import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/auth/dpop.dart';

import 'dpop_test_helpers.dart';

void main() {
  tearDown(() {
    testDpopBackendOverride = null;
  });

  group('tokenIsBound (#3218)', () {
    test('true for a token with cnf.jkt', () {
      expect(tokenIsBound(boundToken()), isTrue);
    });

    test('false for a token without cnf', () {
      final payload = base64Url
          .encode(utf8.encode(jsonEncode({'sub': 'u1'})))
          .replaceAll('=', '');
      expect(tokenIsBound('h.$payload.s'), isFalse);
    });

    test('false for a cnf without jkt', () {
      final payload = base64Url
          .encode(utf8.encode(jsonEncode({'cnf': {}})))
          .replaceAll('=', '');
      expect(tokenIsBound('h.$payload.s'), isFalse);
    });

    test('false for wrong part count', () {
      expect(tokenIsBound('not-a-jwt'), isFalse);
    });

    test('false for an undecodable payload', () {
      expect(tokenIsBound('h.????.s'), isFalse);
    });

    test('false for a non-object payload', () {
      final payload =
          base64Url.encode(utf8.encode(jsonEncode([1, 2]))).replaceAll('=', '');
      expect(tokenIsBound('h.$payload.s'), isFalse);
    });
  });

  group('dpopBackend', () {
    test('override replaces the platform backend', () {
      final fake = FakeDpopBackend();
      testDpopBackendOverride = fake;
      expect(identical(dpopBackend, fake), isTrue);
    });

    test('falls back to the stub without an override', () {
      expect(dpopBackend.ensureKey(), completion(isFalse));
    });
  });

  group('dpopHeadersFor', () {
    test('null token yields empty headers', () async {
      expect(await dpopHeadersFor('GET', 'https://h/x', null), isEmpty);
    });

    test('bound token carries Bearer plus DPoP proof', () async {
      testDpopBackendOverride = FakeDpopBackend(proof: 'the-proof');
      final token = boundToken();
      final headers = await dpopHeadersFor('GET', 'https://h/x', token);
      expect(headers['Authorization'], 'Bearer $token');
      expect(headers['DPoP'], 'the-proof');
    });

    test('unbound token carries Bearer only', () async {
      testDpopBackendOverride = FakeDpopBackend(proof: 'the-proof');
      final headers = await dpopHeadersFor('GET', 'https://h/x', 'plain');
      expect(headers['Authorization'], 'Bearer plain');
      expect(headers.containsKey('DPoP'), isFalse);
    });

    test('no key means no proof even for a bound token', () async {
      testDpopBackendOverride = FakeDpopBackend(hasKey: false);
      final headers = await dpopHeadersFor(
        'GET',
        'https://h/x',
        boundToken(),
      );
      expect(headers['Authorization'], isNotNull);
      expect(headers.containsKey('DPoP'), isFalse);
    });
  });
}
