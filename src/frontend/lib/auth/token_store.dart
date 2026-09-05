/// Session-token persistence (#3193).
///
/// On the web the token is kept in browser sessionStorage — closing the
/// tab or the browser destroys the session — while other targets use
/// SharedPreferences via the stub implementation.
export 'token_store_stub.dart'
    if (dart.library.js_interop) 'token_store_web.dart';
