/// DPoP (RFC 9449) facade — the binding backend for the web client
/// (#3218).
///
/// See `dpop_core.dart` for the contract and the threat model, and
/// `AuthService._saveToken` for the bind-after-mint flow that uses it.
library;

export 'dpop_core.dart';

import 'package:klangk_plugin_api/klangk_plugin_api.dart' show baseUrl;

import 'dpop_core.dart';
import 'dpop_backend_stub.dart'
    if (dart.library.js_interop) 'dpop_backend_web.dart' as impl;

/// The platform DPoP backend (overridable in tests via
/// [testDpopBackendOverride]).
DpopBackend get dpopBackend =>
    testDpopBackendOverride ?? impl.createDpopBackend();

/// The htu a proof must name: the request path as the backend sees it
/// (#3287).
///
/// On a subpath deployment the backend sits behind an outer proxy that
/// strips the base prefix before forwarding, while the browser — minting
/// from `<base href>` — carries it. A proof whose htu included the prefix
/// failed the server's uri comparison, so the htu is minted over the
/// backend-visible path: the [url]'s path component with [baseUrl]'s
/// prefix removed. Scheme, host, and query never participate in the
/// server's comparison, so a bare path is a valid htu (the server also
/// tolerates the prefixed form, keeping pre-fix clients working).
String backendVisiblePath(String url) {
  final path = Uri.tryParse(url)?.path ?? url;
  if (baseUrl.isEmpty) return path;
  final base = baseUrl.startsWith('/') ? baseUrl : '/$baseUrl';
  final trimmed =
      base.endsWith('/') ? base.substring(0, base.length - 1) : base;
  if (trimmed.isEmpty) return path;
  if (path == trimmed) return '';
  return path.startsWith('$trimmed/') ? path.substring(trimmed.length) : path;
}

/// Bearer headers for [token], plus a DPoP proof when it is bound
/// (#3218). For callers that only hold the token string (file viewer,
/// uploads, WS clients) — `AuthService.authHeadersFor` is the same
/// thing for callers holding the service. A null [token] yields empty
/// headers (an unauthenticated request), matching the old conditional
/// Bearer assembly at each call site.
Future<Map<String, String>> dpopHeadersFor(
  String method,
  String url,
  String? token,
) async {
  final headers = <String, String>{};
  if (token == null) return headers;
  headers['Authorization'] = 'Bearer $token';
  final proof = await dpopBackend.createProof(
    method: method,
    uri: backendVisiblePath(url),
    accessToken: token,
  );
  if (proof != null) headers['DPoP'] = proof;
  return headers;
}
