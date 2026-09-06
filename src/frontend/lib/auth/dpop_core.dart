/// DPoP (RFC 9449) token binding — shared contract (#3218).
///
/// Browser sessions bind their JWT to a non-extractable WebCrypto ECDSA
/// P-256 key so an XSS cannot exfiltrate a usable credential: the token
/// carries `cnf.jkt` (the public key's RFC 7638 thumbprint) and every
/// authenticated request must present a fresh proof signed by the private
/// half. The platform backend lives in `dpop_backend_web.dart` (WebCrypto
/// + IndexedDB) or `dpop_backend_stub.dart` (non-web: no binding); pick it
/// via the conditional export in `dpop.dart`.
library;

import 'dart:convert';

/// The platform DPoP backend. Tests replace it wholesale via
/// [testDpopBackendOverride] (same pattern as
/// `testAuthHttpClientOverride`).
abstract class DpopBackend {
  /// Ensure a binding keypair exists (creating one when none does).
  ///
  /// Returns false when the platform cannot hold a key (non-web, or a
  /// browser without a secure context — `crypto.subtle` is undefined
  /// over plain HTTP to a remote host). Callers then skip binding.
  Future<bool> ensureKey();

  /// The public JWK to register at `POST /auth/bind`, or null when no
  /// key is available. Always `{kty, crv, x, y}` — the private half is
  /// never handed to anything outside WebCrypto.
  Future<Map<String, dynamic>?> publicJwk();

  /// A DPoP proof (compact JWS, ES256, raw P1363 signature) for
  /// [method] + [uri] + [accessToken], or null when [accessToken] is
  /// not bound or no key is available.
  Future<String?> createProof({
    required String method,
    required String uri,
    required String accessToken,
  });
}

/// Test seam: when set, [dpopBackend] returns it instead of the
/// platform implementation.
DpopBackend? testDpopBackendOverride;

/// True when the JWT carries a DPoP binding (`cnf.jkt`, #3218).
///
/// Decodes the payload locally — the same defensive parse
/// `AuthService._payload` does, kept here so call sites that only hold
/// the token string (WS clients, file widgets) can decide whether to
/// attach a proof without reaching into AuthService.
bool tokenIsBound(String token) {
  final parts = token.split('.');
  if (parts.length != 3) return false;
  final payload = parts[1];
  final normalized = payload.padRight(
    payload.length + (4 - payload.length % 4) % 4,
    '=',
  );
  try {
    final decoded = jsonDecode(utf8.decode(base64Url.decode(normalized)));
    if (decoded is! Map<String, dynamic>) return false;
    final cnf = decoded['cnf'];
    return cnf is Map && cnf['jkt'] is String;
  } catch (_) {
    return false;
  }
}
