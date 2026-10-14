import 'package:web/web.dart' as web;

import 'popup_handle.dart';

/// The popup this platform returns: wraps the [web.Window] handle.
class WebAuthFetchPopup implements AuthFetchPopup {
  final web.Window _window;

  WebAuthFetchPopup(this._window);

  @override
  void close() => _window.close();
}

/// Open a URL in a sized popup window (falls back to tab if blocked).
/// Returns a closable handle — the opener may close a window it opened
/// even after the popup navigated cross-origin.
AuthFetchPopup? openUrl(String url) {
  final window = web.window.open(
    url,
    'klangk_auth_fetch',
    'width=600,height=700,menubar=no,toolbar=no,location=yes,status=no',
  );
  return window == null ? null : WebAuthFetchPopup(window);
}
