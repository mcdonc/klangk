import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/auth/dpop.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart'
    show testBaseUrlOverride;

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

  group('backendVisiblePath (#3287)', () {
    tearDown(() {
      testBaseUrlOverride = null;
    });

    test('root deployment: baseUrl empty, path unchanged', () {
      testBaseUrlOverride = '';
      expect(backendVisiblePath('/api/v1/x'), '/api/v1/x');
      expect(
        backendVisiblePath('https://h:9/api/v1/x?q=1'),
        '/api/v1/x',
      );
    });

    test('subpath deployment: prefix stripped from htu', () {
      testBaseUrlOverride = '/klangk';
      expect(backendVisiblePath('/klangk/api/v1/x'), '/api/v1/x');
      expect(backendVisiblePath('/klangk/ws'), '/ws');
      expect(
        backendVisiblePath('ws://h:443/klangk/api/v1/x?q=1'),
        '/api/v1/x',
      );
    });

    test('subpath with trailing-slash baseUrl strips too', () {
      testBaseUrlOverride = '/klangk/';
      expect(backendVisiblePath('/klangk/api/v1/x'), '/api/v1/x');
    });

    test('multi-segment baseUrl strips whole', () {
      testBaseUrlOverride = '/tools/klangk';
      expect(backendVisiblePath('/tools/klangk/ws'), '/ws');
      expect(
        backendVisiblePath('https://h/tools/klangk/api/v1/x'),
        '/api/v1/x',
      );
      expect(backendVisiblePath('/tools/other/x'), '/tools/other/x');
    });

    test('prefix only matches whole segments', () {
      testBaseUrlOverride = '/klangk';
      expect(
          backendVisiblePath('/klangkland/api/v1/x'), '/klangkland/api/v1/x');
    });

    test('already backend-visible path passes through', () {
      testBaseUrlOverride = '/klangk';
      expect(backendVisiblePath('/api/v1/x'), '/api/v1/x');
    });

    test('bare base path collapses to empty path', () {
      testBaseUrlOverride = '/klangk';
      expect(backendVisiblePath('/klangk'), '');
    });

    test('full-URL baseUrl (test shapes) never matches a path', () {
      testBaseUrlOverride = 'http://localhost:8997';
      expect(backendVisiblePath('/api/v1/x'), '/api/v1/x');
      expect(
        backendVisiblePath('http://localhost:8997/api/v1/x'),
        '/api/v1/x',
      );
    });
  });

  group('dpopHeadersFor mints the backend-visible htu (#3287)', () {
    tearDown(() {
      testBaseUrlOverride = null;
      testDpopBackendOverride = null;
    });

    test('subpath deployment strips the prefix before minting', () async {
      testBaseUrlOverride = '/klangk';
      final fake = FakeDpopBackend();
      testDpopBackendOverride = fake;
      await dpopHeadersFor('GET', 'ws://h:443/klangk/ws', boundToken());
      expect(fake.lastUri, '/ws');
    });

    test('root deployment mints the bare path', () async {
      testBaseUrlOverride = '';
      final fake = FakeDpopBackend();
      testDpopBackendOverride = fake;
      await dpopHeadersFor(
        'GET',
        'https://h/api/v1/my-permissions?q=1',
        boundToken(),
      );
      expect(fake.lastUri, '/api/v1/my-permissions');
    });
  });
}
