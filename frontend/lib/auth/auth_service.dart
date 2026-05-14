import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';

class AuthService extends ChangeNotifier {
  static const _tokenKey = 'bark_jwt';
  static const _baseUrl = 'http://localhost:8996';

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
      final response = await http.post(
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
      final response = await http.post(
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

  Future<void> logout() async {
    try {
      await http.post(
        Uri.parse('$_baseUrl/auth/logout'),
        headers: _authHeaders,
      );
    } catch (_) {
      // Best effort — clear token regardless
    }
    await _clearToken();
  }
}
