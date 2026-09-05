import 'package:web/web.dart' as web;

/// Web token store (#3193): the session JWT lives in sessionStorage, so
/// closing the tab or the browser destroys it. shared_preferences maps to
/// localStorage on the web — which survives a browser close — so this
/// implementation talks to `window.sessionStorage` directly.
///
/// A token left in localStorage by an older build (shared_preferences
/// writes it under the `flutter.` prefix) is migrated into sessionStorage
/// on first read and scrubbed, so an upgraded deployment stops carrying
/// the persistent copy around.

const tokenKey = 'klangk_jwt';
const legacyLocalStorageKey = 'flutter.klangk_jwt';

Future<String?> readToken() async {
  final token = web.window.sessionStorage.getItem(tokenKey);
  if (token != null) return token;
  final legacy = web.window.localStorage.getItem(legacyLocalStorageKey);
  if (legacy == null) return null;
  web.window.localStorage.removeItem(legacyLocalStorageKey);
  web.window.sessionStorage.setItem(tokenKey, legacy);
  return legacy;
}

Future<void> writeToken(String token) async {
  web.window.sessionStorage.setItem(tokenKey, token);
}

Future<void> clearToken() async {
  web.window.sessionStorage.removeItem(tokenKey);
  web.window.localStorage.removeItem(legacyLocalStorageKey);
}
