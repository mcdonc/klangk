/// Popup↔opener messaging for the browser-authorization flow (#3385).
///
/// The authorize popup is a second instance of this SPA at the klangk
/// origin (Gitea redirects to the origin root with `?code=..&state=..`).
/// It delivers the authorization result to the workspace tab that opened
/// it via `postMessage`; the opener side surfaces it as a stream.
library;

export 'window_messaging_stub.dart'
    if (dart.library.js_interop) 'window_messaging_web.dart';
