import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Override for testing — set to intercept all HTTP calls in AuthService.
http.Client? testAuthHttpClientOverride;

class AuthService extends ChangeNotifier {
  static const _tokenKey = 'klangk_jwt';
  String get _baseUrl => baseUrl;

  http.Client get _client => testAuthHttpClientOverride ?? http.Client();

  String? _token;
  bool _loading = false;
  bool _initialized = false;
  String _bannerTitle = '';
  String _bannerText = '';
  bool _bannerAccepted = false;
  Timer? _permissionTimer;
  Timer? _refreshTimer;

  String? get token => _token;
  bool get isLoggedIn => _token != null;
  bool get loading => _loading;
  bool get initialized => _initialized;
  String get bannerTitle => _bannerTitle;
  String get bannerText => _bannerText;
  bool get bannerAccepted => _bannerAccepted;
  bool get bannerRequired => _bannerText.isNotEmpty && !_bannerAccepted;

  /// Decode the JWT payload.
  Map<String, dynamic>? get _payload {
    if (_token == null) return null;
    try {
      final parts = _token!.split('.');
      if (parts.length != 3) return null;
      final payload = parts[1];
      final padded = payload.padRight(
        payload.length + (4 - payload.length % 4) % 4,
        '=',
      );
      final decoded = utf8.decode(base64Url.decode(padded));
      return jsonDecode(decoded) as Map<String, dynamic>;
    } catch (e) {
      debugPrint(
          '[AuthService] decode token failed: $e'); // coverage:ignore-line
      return null;
    }
  }

  String? get userId => _payload?['sub'] as String?;
  String? get email => _payload?['email'] as String?;

  /// Permissions fetched from /api/my-permissions.
  Map<String, List<String>> _permissions = {};
  List<Map<String, dynamic>> _groups = [];

  Map<String, List<String>> get permissions => _permissions;
  List<Map<String, dynamic>> get groups => _groups;

  bool get isAdmin => hasPermission('/admin', '*');

  /// Check if the user has a specific permission on a resource.
  bool hasPermission(String resource, String permission) {
    final perms = _permissions[resource];
    if (perms == null) return false;
    return perms.contains(permission) || perms.contains('*');
  }

  AuthService() {
    _loadToken();
  }

  Future<void> _loadToken() async {
    final prefs = await SharedPreferences.getInstance();
    _token = prefs.getString(_tokenKey);

    try {
      final client = testAuthHttpClientOverride ?? http.Client();
      final resp = await client.get(Uri.parse('$_baseUrl/api/config'));
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body);
        _bannerTitle = (data['login_banner_title'] as String?) ?? '';
        _bannerText = (data['login_banner'] as String?) ?? '';
      }
    } catch (e) {
      debugPrint('[AuthService] load config failed: $e');
    } // coverage:ignore-line

    if (_bannerText.isNotEmpty) {
      final acceptedHash = prefs.getString('klangk_banner_accepted');
      _bannerAccepted = acceptedHash == _bannerText.hashCode.toString();
    }

    if (_token != null) {
      await _fetchPermissions();
      _scheduleTokenRefresh();
    }

    _initialized = true;
    notifyListeners();
  }

  /// Fetch permissions from the server.
  Future<void> _fetchPermissions() async {
    debugPrint('[AuthService] fetching /api/my-permissions');
    try {
      final resp = await _client.get(
        Uri.parse('$_baseUrl/api/my-permissions'),
        headers: _authHeaders,
      );
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as Map<String, dynamic>;
        final permsRaw = data['permissions'] as Map<String, dynamic>? ?? {};
        _permissions = permsRaw.map(
          (k, v) => MapEntry(k, List<String>.from(v as List)),
        );
        _groups = List<Map<String, dynamic>>.from(
          data['groups'] as List? ?? [],
        );
      } else if (resp.statusCode == 401) {
        await _clearToken();
      }
    } catch (e) {
      debugPrint('[AuthService] fetch permissions failed: $e');
    } // coverage:ignore-line
  }

  /// Refresh permissions from the server (call after group changes).
  Future<void> refreshPermissions() async {
    await _fetchPermissions();
    notifyListeners();
  }

  void _stopPermissionRefresh() {
    _permissionTimer?.cancel();
    _permissionTimer = null;
  }

  Future<void> acceptBanner() async {
    if (_bannerText.isEmpty) return;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(
        'klangk_banner_accepted', _bannerText.hashCode.toString());
    _bannerAccepted = true;
    notifyListeners();
  }

  Future<void> _saveToken(String token) async {
    _token = token;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_tokenKey, token);
    await _fetchPermissions();
    _scheduleTokenRefresh();
    notifyListeners();
  }

  /// Save a token from email verification (public for VerifyPage).
  Future<void> saveTokenFromVerification(String token) async {
    await _saveToken(token);
  }

  Future<void> _clearToken() async {
    _refreshTimer?.cancel();
    _refreshTimer = null;
    _token = null;
    _permissions = {};
    _groups = [];
    _stopPermissionRefresh();
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_tokenKey);
    notifyListeners();
  }

  Map<String, String> get _authHeaders => {
        'Content-Type': 'application/json',
        if (_token != null) 'Authorization': 'Bearer $_token',
      };

  Future<String?> register(String email, String password) async {
    _loading = true;
    notifyListeners();
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/auth/register'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'email': email, 'password': password}),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        if (data['access_token'] != null) {
          // Test mode: auto-verified, log in immediately
          await _saveToken(data['access_token']);
          return null;
        }
        // Production: verification email sent
        return 'Check your email to verify your account.';
      }
      final error = jsonDecode(response.body);
      return error['detail'] ?? 'Registration failed';
    } catch (e) {
      return 'Connection error: $e';
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  Future<String?> login(String email, String password) async {
    _loading = true;
    notifyListeners();
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/auth/login'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'email': email, 'password': password}),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        await _saveToken(data['access_token']);
        return null;
      }
      final error = jsonDecode(response.body);
      return error['detail'] ?? 'Login failed';
    } catch (e) {
      return 'Connection error: $e';
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  Future<String?> resendVerification(String email, String password) async {
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/auth/resend-verification'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'email': email, 'password': password}),
      );
      if (response.statusCode == 200) {
        return null;
      }
      final error = jsonDecode(response.body);
      return error['detail'] ?? 'Failed to resend';
    } catch (e) {
      return 'Connection error: $e';
    }
  }

  /// Make an authenticated HTTP request. If the response is 401,
  /// clear the token (router will redirect to login).
  Future<http.Response> authGet(String path) async {
    final response = await _client.get(
      Uri.parse('$_baseUrl$path'),
      headers: _authHeaders,
    );
    if (response.statusCode == 401) await _clearToken();
    return response;
  }

  Future<http.Response> authPost(String path, {String? body}) async {
    final response = await _client.post(
      Uri.parse('$_baseUrl$path'),
      headers: _authHeaders,
      body: body,
    );
    if (response.statusCode == 401) await _clearToken();
    return response;
  }

  Future<http.Response> authPatch(String path, {String? body}) async {
    final response = await _client.patch(
      Uri.parse('$_baseUrl$path'),
      headers: _authHeaders,
      body: body,
    );
    if (response.statusCode == 401) await _clearToken();
    return response;
  }

  Future<http.Response> authPut(String path, {String? body}) async {
    final response = await _client.put(
      Uri.parse('$_baseUrl$path'),
      headers: {
        ..._authHeaders,
        if (body != null) 'Content-Type': 'application/json',
      },
      body: body,
    );
    if (response.statusCode == 401) await _clearToken();
    return response;
  }

  Future<http.Response> authDelete(String path) async {
    final response = await _client.delete(
      Uri.parse('$_baseUrl$path'),
      headers: _authHeaders,
    );
    if (response.statusCode == 401) await _clearToken();
    return response;
  }

  /// Schedule a token refresh at 80% of the token's remaining lifetime.
  void _scheduleTokenRefresh() {
    _refreshTimer?.cancel();
    _refreshTimer = null;
    final exp = _payload?['exp'] as int?;
    if (exp == null) return;
    final expiryMs = exp * 1000;
    final nowMs = DateTime.now().millisecondsSinceEpoch;
    final remainingMs = expiryMs - nowMs;
    if (remainingMs <= 0) return;
    final refreshInMs = (remainingMs * 0.8).round();
    debugPrint(
      '[AuthService] scheduling token refresh in ${refreshInMs ~/ 1000}s',
    );
    _refreshTimer = Timer(
      Duration(milliseconds: refreshInMs),
      _refreshToken,
    );
  }

  /// Call POST /auth/refresh to get a new token.
  Future<void> _refreshToken() async {
    if (_token == null) return;
    debugPrint('[AuthService] refreshing token');
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/auth/refresh'),
        headers: _authHeaders,
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final newToken = data['access_token'] as String?;
        if (newToken != null) {
          await _saveToken(newToken);
        }
      } else if (response.statusCode == 401) {
        await _clearToken();
      }
    } catch (e) {
      // Network error — retry in 60 seconds
      debugPrint('[AuthService] refresh token failed: $e, retrying in 60s');
      _refreshTimer = Timer(const Duration(seconds: 60), _refreshToken);
    }
  }

  /// Expose refresh for testing.
  @visibleForTesting
  Future<void> testRefreshToken() => _refreshToken();

  /// Log out. Returns the IdP logout URL if the provider requires
  /// a redirect, or null for local-only logout.
  Future<String?> logout() async {
    String? oidcLogoutUrl;
    try {
      final resp = await _client.post(
        Uri.parse('$_baseUrl/auth/logout'),
        headers: _authHeaders,
      );
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body);
        oidcLogoutUrl = data['oidc_logout_url'] as String?;
      }
    } catch (e) {
      debugPrint('[AuthService] logout request failed: $e');
    }
    await _clearToken();
    return oidcLogoutUrl;
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    _stopPermissionRefresh();
    super.dispose();
  }
}
