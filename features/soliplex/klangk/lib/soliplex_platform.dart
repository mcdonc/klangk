/// Platform boundary for the Soliplex feature's two browser-only concerns:
/// token storage and interactive login. Mirrors klangk's own
/// `web_helpers_stub`/`web_helpers_web` and soliplex's `auth_flow_native`/`_web`
/// split — the native (stub) implementation is the default; web swaps in when
/// `dart.library.js_interop` is available.
///
/// Both implementations export the same surface:
///   - `SoliplexTokenStore` — async token persistence (5 keys).
///   - `String soliplexBackendBase()` — Klangk backend base URL.
///   - `Future<SoliplexAuthResult> soliplexInteractiveLogin(...)` — login.
///
/// Rule (Phase 4 guardrail): feature.dart and soliplex_tools.dart must NOT
/// import dart:js_interop / dart:html / package:web directly — only this file.
export 'soliplex_platform_native.dart'
    if (dart.library.js_interop) 'soliplex_platform_web.dart';
