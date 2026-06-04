import 'package:flutter/foundation.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Backend origin used on native (desktop) builds, where there is no page
/// origin to resolve relative URLs against. Override at build/run time with
/// `--dart-define=KLANGK_BACKEND_URL=http://host:port`.
const String _backendOrigin = String.fromEnvironment(
  'KLANGK_BACKEND_URL',
  defaultValue: 'http://localhost:18997',
);

/// HTTP base URL prefix for backend API calls.
///
///   web    → the path prefix from `<base href>` (`''` or `/klangk`); the
///            browser resolves it against the page origin.
///   native → a full origin (from `KLANGK_BACKEND_URL`) plus that path prefix,
///            since there is no page origin on desktop.
///
/// VM tests inject a full override URL via [testBaseUrlOverride]; that path is
/// honored verbatim (it already encodes the desired origin) and the native
/// origin is not prepended — otherwise tests, which run with `kIsWeb == false`,
/// would target a doubled-up URL.
String get apiBaseUrl {
  if (testBaseUrlOverride != null) return baseUrl;
  return kIsWeb ? baseUrl : '$_backendOrigin$baseUrl';
}

/// WebSocket base URL for the backend, including the path prefix and the
/// trailing `/ws`.
///
/// On web this is derived from the page location (preserving the historical
/// behavior); on native it is derived from `KLANGK_BACKEND_URL`.
String get wsBaseUrl {
  final origin = kIsWeb ? Uri.base : Uri.parse(_backendOrigin);
  final wsScheme = origin.scheme == 'https' ? 'wss' : 'ws';
  return '$wsScheme://${origin.host}:${origin.port}$baseUrl/ws';
}
