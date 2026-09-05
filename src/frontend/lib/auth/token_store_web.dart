import 'dart:convert';

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

/// Undo shared_preferences' JSON encoding (it stores strings via
/// `json.encode`, so the legacy value arrives double-quoted). Falls back
/// to the raw value for anything that isn't a JSON string.
String? decodeLegacyToken(String? raw) {
  if (raw == null) return null;
  try {
    final decoded = jsonDecode(raw);
    if (decoded is String) return decoded;
  } catch (_) {
    // Not JSON — treat as a raw token.
  }
  return raw;
}

/// Scrub a token left in localStorage by an older build. Safe to call on
/// every write/clear: a fresh login supersedes any legacy token, and the
/// key simply may not exist.
void scrubLegacyToken() {
  web.window.localStorage.removeItem(legacyLocalStorageKey);
}

/// Storage access throws when the browser blocks site data (cookies
/// policy, privacy modes). A failure here must degrade to "logged out",
/// not brick startup — `_loadToken` runs fire-and-forget from the
/// AuthService constructor, so an exception would leave the splash
/// spinner up forever.
Future<String?> readToken() async {
  try {
    final token = web.window.sessionStorage.getItem(tokenKey);
    if (token != null) return token;
    final legacy = decodeLegacyToken(
      web.window.localStorage.getItem(legacyLocalStorageKey),
    );
    if (legacy == null) return null;
    // Write the replacement BEFORE removing the original, so a storage
    // failure can't destroy the only copy.
    web.window.sessionStorage.setItem(tokenKey, legacy);
    web.window.localStorage.removeItem(legacyLocalStorageKey);
    return legacy;
  } catch (_) {
    return null;
  }
}

Future<void> writeToken(String token) async {
  web.window.sessionStorage.setItem(tokenKey, token);
  // #3193 review: a fresh login supersedes any legacy token — don't let
  // one linger in localStorage until the next logout.
  scrubLegacyToken();
}

Future<void> clearToken() async {
  web.window.sessionStorage.removeItem(tokenKey);
  scrubLegacyToken();
}
