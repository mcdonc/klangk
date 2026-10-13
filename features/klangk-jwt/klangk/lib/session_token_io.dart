/// Reads the browser tab's klangk session JWT (the `klangk_jwt`
/// sessionStorage key written by the auth service, #3193).
///
/// Web: the live sessionStorage read (`session_token_web.dart`). Non-web:
/// always null (`session_token_stub.dart`) — the feature serves browsers,
/// and a native build has no tab to read from.
export 'session_token_stub.dart'
    if (dart.library.js_interop) 'session_token_web.dart';
