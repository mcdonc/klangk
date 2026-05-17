import 'backend_url_stub.dart'
    if (dart.library.js_interop) 'backend_url_web.dart';

/// Override for testing — set to bypass DOM access in VM tests.
String? testBaseUrlOverride;

/// Get the base URL path for API calls.
/// Returns '' for root, '/bark' for subpath (no trailing slash).
String get baseUrl {
  if (testBaseUrlOverride != null) return testBaseUrlOverride!;
  return getBaseUrlFromDom();
}
