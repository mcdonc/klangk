/// Shared DPoP test fakes (#3218). Not a `_test.dart` file — imported
/// by the suites that need it.
library;

import 'dart:convert';

import 'package:klangk_frontend/auth/dpop.dart';

/// A JWT-shaped token whose payload carries `cnf.jkt` — what the server
/// returns from `POST /auth/bind`. The signature is irrelevant: the
/// client only base64-decodes the payload.
String boundToken({String jkt = 'jkt-1'}) {
  final payload = jsonEncode({
    'sub': 'u1',
    'email': 'u@example.com',
    'jti': 'jti-1',
    'exp': 9999999999,
    'cnf': {'jkt': jkt},
  });
  final encoded = base64Url.encode(utf8.encode(payload)).replaceAll('=', '');
  return 'header.$encoded.signature';
}

/// A programmable [DpopBackend] mirroring the web backend's semantics:
/// proofs are produced only for bound tokens. Records the last htu it
/// was asked to mint, so tests can assert the backend-visible path
/// (#3287).
class FakeDpopBackend implements DpopBackend {
  FakeDpopBackend({this.hasKey = true, this.proof = 'proof-value'});

  final bool hasKey;
  final String proof;

  /// The `uri` of the most recent createProof call (null before any).
  String? lastUri;
  Map<String, dynamic>? jwk = const {
    'kty': 'EC',
    'crv': 'P-256',
    'x': 'x-coord',
    'y': 'y-coord',
  };

  @override
  Future<bool> ensureKey() async => hasKey;

  @override
  Future<Map<String, dynamic>?> publicJwk() async => hasKey ? jwk : null;

  @override
  Future<String?> createProof({
    required String method,
    required String uri,
    required String accessToken,
  }) async {
    lastUri = uri;
    return hasKey && tokenIsBound(accessToken) ? proof : null;
  }
}
