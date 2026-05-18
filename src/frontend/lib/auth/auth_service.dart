import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';

/// Override for testing — set to intercept all HTTP calls in AuthService.
http.Client? testAuthHttpClientOverride;

class AuthService extends ChangeNotifier {
  static const _tokenKey = 'bark_jwt';
  String get _baseUrl => baseUrl;

  http.Client get _client => testAuthHttpClientOverride ?? http.Client();

  String? _token;
  bool _loading = false;
  bool _initialized = false;

  String? get token => _token;
  bool get isLoggedIn => _token != null;
  bool get loading => _loading;
  bool get initialized => _initialized;

  AuthService() {
    _loadToken();
  }

  Future<void> _loadToken() async {
    final prefs = await SharedPreferences.getInstance();
    _token = prefs.getString(_tokenKey);
    _initialized = true;
    notifyListeners();
  }

  Future<void> _saveToken(String token) async {
    _token = token;
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_tokenKey, token);
    notifyListeners();
  }

  Future<void> _clearToken() async {
    _token = null;
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_tokenKey);
    notifyListeners();
  }

  Map<String, String> get _authHeaders => {
        'Content-Type': 'application/json',
        if (_token != null) 'Authorization': 'Bearer $_token',
      };

  Future<String?> register(String username, String password) async {
    _loading = true;
    notifyListeners();
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/auth/register'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'username': username, 'password': password}),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        await _saveToken(data['access_token']);
        return null;
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

  Future<String?> login(String username, String password) async {
    _loading = true;
    notifyListeners();
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/auth/login'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'username': username, 'password': password}),
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

  Future<http.Response> authDelete(String path) async {
    final response = await _client.delete(
      Uri.parse('$_baseUrl$path'),
      headers: _authHeaders,
    );
    if (response.statusCode == 401) await _clearToken();
    return response;
  }

  Future<void> logout() async {
    try {
      await _client.post(
        Uri.parse('$_baseUrl/auth/logout'),
        headers: _authHeaders,
      );
    } catch (_) {
      // Best effort — clear token regardless
    }
    await _clearToken();
  }
}
