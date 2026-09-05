import 'package:shared_preferences/shared_preferences.dart';

/// Default (non-web) token store: the JWT persists in SharedPreferences.
/// Swapped for a sessionStorage-backed implementation on the web via the
/// conditional export in `token_store.dart` (#3193).

const tokenKey = 'klangk_jwt';

Future<String?> readToken() async {
  final prefs = await SharedPreferences.getInstance();
  return prefs.getString(tokenKey);
}

Future<void> writeToken(String token) async {
  final prefs = await SharedPreferences.getInstance();
  await prefs.setString(tokenKey, token);
}

Future<void> clearToken() async {
  final prefs = await SharedPreferences.getInstance();
  await prefs.remove(tokenKey);
}
