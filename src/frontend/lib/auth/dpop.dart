/// DPoP (RFC 9449) facade — the binding backend for the web client
/// (#3218).
///
/// See `dpop_core.dart` for the contract and the threat model, and
/// `AuthService._saveToken` for the bind-after-mint flow that uses it.
library;

export 'dpop_core.dart';

import 'dpop_core.dart';
import 'dpop_backend_stub.dart'
    if (dart.library.js_interop) 'dpop_backend_web.dart' as impl;

/// The platform DPoP backend (overridable in tests via
/// [testDpopBackendOverride]).
DpopBackend get dpopBackend =>
    testDpopBackendOverride ?? impl.createDpopBackend();

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
    uri: url,
    accessToken: token,
  );
  if (proof != null) headers['DPoP'] = proof;
  return headers;
}
