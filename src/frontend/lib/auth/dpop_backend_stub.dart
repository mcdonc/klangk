// coverage:ignore-file
/// Non-web DPoP backend: no binding, no proofs (#3218).
///
/// The web app is the only surface the XSS findings concern; everywhere
/// else (VM tests, potential desktop shells) tokens simply stay
/// unbound — the server accepts unbound tokens unchanged.
library;

import 'dpop_core.dart';

DpopBackend createDpopBackend() => StubDpopBackend();

class StubDpopBackend implements DpopBackend {
  @override
  Future<bool> ensureKey() async => false;

  @override
  Future<Map<String, dynamic>?> publicJwk() async => null;

  @override
  Future<String?> createProof({
    required String method,
    required String uri,
    required String accessToken,
  }) async =>
      null;
}
