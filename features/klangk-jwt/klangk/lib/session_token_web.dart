import 'package:web/web.dart' as web;

/// Web implementation: the session JWT lives in sessionStorage under
/// `klangk_jwt` (token_store_web.dart) — per-tab, dies with the tab.
Future<String?> readSessionToken() async {
  try {
    return web.window.sessionStorage.getItem('klangk_jwt');
  } catch (_) {
    // Storage access throws when the browser blocks site data — degrade
    // to "no token", the same shape as being logged out.
    return null;
  }
}
