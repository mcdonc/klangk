import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_feature_klangk_jwt/feature.dart';

http.Client _configClient([Map<String, dynamic> config = const {}]) {
  return MockClient((request) async {
    return http.Response(jsonEncode(config), 200);
  });
}

void main() {
  late KlangkJwtFeature feature;

  setUp(() {
    feature = KlangkJwtFeature(
      tokenReader: () async => 'jwt-abc',
      httpClient: _configClient(),
    );
  });

  tearDown(() {
    feature.dispose();
  });

  group('handler registration', () {
    test('registers exactly the klangk_jwt handler', () {
      expect(feature.handlers.keys, ['klangk_jwt']);
    });
  });

  group('no token', () {
    test('answers with an error when the tab holds no token', () async {
      final bare = KlangkJwtFeature(
        tokenReader: () async => null,
        httpClient: _configClient(
          {'klangk_jwt_require_consent': 'false'},
        ),
      );
      final result =
          await bare.handlers['klangk_jwt']!({'action': 'klangk_jwt'});
      expect(jsonDecode(result), {'error': 'no session token'});
      bare.dispose();
    });

    test('an empty stored token counts as no token', () async {
      final bare = KlangkJwtFeature(
        tokenReader: () async => '',
        httpClient: _configClient(
          {'klangk_jwt_require_consent': 'false'},
        ),
      );
      final result = await bare.handlers['klangk_jwt']!({});
      expect(jsonDecode(result), {'error': 'no session token'});
      bare.dispose();
    });

    test('a denied caller learns nothing about the login state', () async {
      // Consent runs before the token read, so a denial answers 'cancelled'
      // whether or not the tab holds a token — the login-state oracle stays
      // closed (review finding on #3555).
      final oracle = KlangkJwtFeature(
        tokenReader: () async => 'jwt-secret',
        httpClient: _configClient(),
      );
      final pending = oracle.handlers['klangk_jwt']!({});
      await Future<void>.delayed(Duration.zero);
      oracle.pendingConsentForTest!.complete(false);
      expect(jsonDecode(await pending), {'error': 'cancelled'});
      oracle.dispose();
    });
  });

  group('consent disabled by config', () {
    test('returns the raw token without waiting for a dialog', () async {
      final open = KlangkJwtFeature(
        tokenReader: () async => 'jwt-open',
        httpClient: _configClient({'klangk_jwt_require_consent': 'false'}),
      );
      final result = await open.handlers['klangk_jwt']!({});
      expect(jsonDecode(result), {'token': 'jwt-open'});
      open.dispose();
    });
  });

  group('consent required (default)', () {
    test('blocks until the user allows', () async {
      final pending = feature.handlers['klangk_jwt']!({});
      await Future<void>.delayed(Duration.zero);
      bool completed = false;
      unawaited(pending.then((_) => completed = true));
      await Future<void>.delayed(const Duration(milliseconds: 20));
      expect(completed, isFalse, reason: 'must wait for the dialog');

      final consent = feature.pendingConsentForTest;
      expect(consent, isNotNull);
      consent!.complete(true);
      expect(jsonDecode(await pending), {'token': 'jwt-abc'});
    });

    test('deny answers with cancelled', () async {
      final pending = feature.handlers['klangk_jwt']!({});
      await Future<void>.delayed(Duration.zero);
      feature.pendingConsentForTest!.complete(false);
      expect(jsonDecode(await pending), {'error': 'cancelled'});
    });

    test('a second request cancels the first', () async {
      final first = feature.handlers['klangk_jwt']!({});
      await Future<void>.delayed(Duration.zero);
      final second = feature.handlers['klangk_jwt']!({});
      await Future<void>.delayed(Duration.zero);
      expect(jsonDecode(await first), {'error': 'cancelled'});
      feature.pendingConsentForTest!.complete(true);
      expect(jsonDecode(await second), {'token': 'jwt-abc'});
    });

    test('a config fetch failure keeps the prompt on', () async {
      final broken = KlangkJwtFeature(
        tokenReader: () async => 'jwt-x',
        httpClient: MockClient(
          (request) async => throw Exception('network down'),
        ),
      );
      final pending = broken.handlers['klangk_jwt']!({});
      await Future<void>.delayed(Duration.zero);
      expect(broken.pendingConsentForTest, isNotNull);
      broken.pendingConsentForTest!.complete(false);
      expect(jsonDecode(await pending), {'error': 'cancelled'});
      broken.dispose();
    });

    test('config is fetched once across requests', () async {
      var fetches = 0;
      final counting = KlangkJwtFeature(
        tokenReader: () async => 'jwt-count',
        httpClient: MockClient((request) async {
          fetches++;
          return http.Response(
            jsonEncode({'klangk_jwt_require_consent': 'false'}),
            200,
          );
        }),
      );
      await counting.handlers['klangk_jwt']!({});
      await counting.handlers['klangk_jwt']!({});
      expect(fetches, 1);
      counting.dispose();
    });
  });

  group('consent dialog', () {
    Widget overlayHost(KlangkJwtFeature feature) => MaterialApp(
          home: Scaffold(
            body: Builder(
              builder: (context) =>
                  Stack(children: [feature.buildOverlay(context)!]),
            ),
          ),
        );

    testWidgets('renders while pending and resolves on Allow', (
      tester,
    ) async {
      final pending = feature.handlers['klangk_jwt']!({});
      await tester.pumpWidget(overlayHost(feature));
      await tester.pump();
      expect(find.text('Share your klangk session token?'), findsOneWidget);

      await tester.tap(find.text('Allow'));
      await tester.pump();
      expect(jsonDecode(await pending), {'token': 'jwt-abc'});
      await tester.pump();
      expect(
        find.text('Share your klangk session token?'),
        findsNothing,
        reason: 'the dialog clears after the decision',
      );
    });

    testWidgets('Deny cancels the request', (tester) async {
      final pending = feature.handlers['klangk_jwt']!({});
      await tester.pumpWidget(overlayHost(feature));
      await tester.pump();
      await tester.tap(find.text('Deny'));
      await tester.pump();
      expect(jsonDecode(await pending), {'error': 'cancelled'});
    });

    testWidgets('no dialog mounts while nothing is pending', (tester) async {
      await tester.pumpWidget(overlayHost(feature));
      await tester.pump();
      expect(find.text('Share your klangk session token?'), findsNothing);
    });
  });
}
