import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_feature_git_credential/feature.dart';

void main() {
  late GitCredentialFeature feature;

  setUp(() {
    feature = GitCredentialFeature();
  });

  tearDown(() {
    feature.dispose();
  });

  group('store operation', () {
    test('stores credentials in cache', () async {
      final result = await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_abc123',
      });
      expect(jsonDecode(result), {'status': 'ok'});
    });

    test('ignores empty username', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': '',
        'password': 'ghp_abc123',
      });
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_real',
      });
      final result = await feature.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result)['password'], 'ghp_real');
    });

    test('ignores empty password', () async {
      final result = await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': '',
      });
      expect(jsonDecode(result), {'status': 'ok'});
    });
  });

  group('erase operation', () {
    test('removes cached credentials', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_abc123',
      });

      final result = await feature.handlers['git_credential']!({
        'operation': 'erase',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result), {'status': 'ok'});
    });

    test('erase on empty cache is a no-op', () async {
      final result = await feature.handlers['git_credential']!({
        'operation': 'erase',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result), {'status': 'ok'});
    });
  });

  group('get operation', () {
    test('cache hit returns credentials immediately', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_abc123',
      });

      final result = await feature.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      });
      final creds = jsonDecode(result);
      expect(creds['username'], 'octocat');
      expect(creds['password'], 'ghp_abc123');
    });

    test('cache is keyed by protocol and host', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'gh-user',
        'password': 'gh-token',
      });
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'gitlab.com',
        'username': 'gl-user',
        'password': 'gl-token',
      });

      final gh = jsonDecode(await feature.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      }));
      expect(gh['username'], 'gh-user');

      final gl = jsonDecode(await feature.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'gitlab.com',
      }));
      expect(gl['username'], 'gl-user');
    });

    test('erase then get does not return stale credentials', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_abc123',
      });
      await feature.handlers['git_credential']!({
        'operation': 'erase',
        'protocol': 'https',
        'host': 'github.com',
      });

      bool completed = false;
      feature.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      })
          .then((_) => completed = true);

      await Future.delayed(const Duration(milliseconds: 50));
      expect(completed, isFalse, reason: 'get should block on cache miss');
    });

    test('store overwrites previous credentials', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'old-user',
        'password': 'old-token',
      });
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'new-user',
        'password': 'new-token',
      });

      final result = jsonDecode(await feature.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': 'github.com',
      }));
      expect(result['username'], 'new-user');
      expect(result['password'], 'new-token');
    });
  });

  group('peek operation', () {
    test('returns cached credentials without dialog', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'x-access-token',
        'password': 'gho_abc123',
      });

      final result = jsonDecode(await feature.handlers['git_credential']!({
        'operation': 'peek',
        'protocol': 'https',
        'host': 'github.com',
      }));
      expect(result['username'], 'x-access-token');
      expect(result['password'], 'gho_abc123');
    });

    test('returns miss immediately on empty cache', () async {
      final result = jsonDecode(await feature.handlers['git_credential']!({
        'operation': 'peek',
        'protocol': 'https',
        'host': 'github.com',
      }));
      expect(result['error'], 'miss');
    });

    test('returns miss after erase', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_abc123',
      });
      await feature.handlers['git_credential']!({
        'operation': 'erase',
        'protocol': 'https',
        'host': 'github.com',
      });

      final result = jsonDecode(await feature.handlers['git_credential']!({
        'operation': 'peek',
        'protocol': 'https',
        'host': 'github.com',
      }));
      expect(result['error'], 'miss');
    });

    test('does not block on cache miss', () async {
      bool completed = false;
      await feature.handlers['git_credential']!({
        'operation': 'peek',
        'protocol': 'https',
        'host': 'github.com',
      })
          .then((_) => completed = true);
      expect(completed, isTrue,
          reason: 'peek must resolve without waiting for a dialog');
    });
  });

  group('device flow operations', () {
    test('device_flow_show returns ok and notifies', () async {
      bool notified = false;
      feature.addListener(() => notified = true);

      final result = await feature.handlers['git_credential']!({
        'operation': 'device_flow_show',
        'protocol': 'https',
        'host': 'github.com',
        'user_code': 'ABCD-1234',
        'verification_uri':
            'https://github.com/login/device?user_code=ABCD-1234',
      });
      expect(jsonDecode(result), {'status': 'ok'});
      expect(notified, isTrue);
    });

    test('device_flow_done returns ok and notifies', () async {
      await feature.handlers['git_credential']!({
        'operation': 'device_flow_show',
        'protocol': 'https',
        'host': 'github.com',
        'user_code': 'ABCD-1234',
        'verification_uri': 'https://github.com/login/device',
      });

      bool notified = false;
      feature.addListener(() => notified = true);

      final result = await feature.handlers['git_credential']!({
        'operation': 'device_flow_done',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result), {'status': 'ok'});
      expect(notified, isTrue);
    });

    test('device_flow_error returns ok and notifies', () async {
      bool notified = false;
      feature.addListener(() => notified = true);

      final result = await feature.handlers['git_credential']!({
        'operation': 'device_flow_error',
        'protocol': 'https',
        'host': 'github.com',
        'error': 'Code expired. Please try again.',
      });
      expect(jsonDecode(result), {'status': 'ok'});
      expect(notified, isTrue);
    });
  });

  group('unknown operation', () {
    test('returns error', () async {
      final result = await feature.handlers['git_credential']!({
        'operation': 'bogus',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result)['error'], contains('unknown operation'));
    });
  });

  group('credential dialog hints', () {
    Widget overlayHost(GitCredentialFeature feature) => MaterialApp(
          home: Scaffold(
            body: Builder(
              builder: (context) => Stack(
                children: [feature.buildOverlay(context)!],
              ),
            ),
          ),
        );

    Future<void> pumpWithPendingGet(
      WidgetTester tester,
      GitCredentialFeature feature,
      String host,
    ) async {
      // Cache-miss get blocks on the dialog completer; do not await it.
      unawaited(feature.handlers['git_credential']!({
        'operation': 'get',
        'protocol': 'https',
        'host': host,
      }));
      await tester.pumpWidget(overlayHost(feature));
      await tester.pump();
    }

    String? hintOf(WidgetTester tester, int textFieldIndex) {
      final field =
          tester.widget<TextField>(find.byType(TextField).at(textFieldIndex));
      return field.decoration?.hintText;
    }

    testWidgets('github.com keeps the GitHub hints', (tester) async {
      await pumpWithPendingGet(tester, feature, 'github.com');
      expect(hintOf(tester, 0), 'GitHub username');
      expect(hintOf(tester, 1), 'ghp_... or github_pat_...');
      expect(find.text('Personal access token (PAT):'), findsOneWidget);
      expect(find.text('Token or password:'), findsNothing);
    });

    testWidgets('www.github.com keeps the GitHub hints', (tester) async {
      await pumpWithPendingGet(tester, feature, 'www.github.com');
      expect(hintOf(tester, 0), 'GitHub username');
      expect(hintOf(tester, 1), 'ghp_... or github_pat_...');
    });

    testWidgets('uppercase GitHub.com host keeps the GitHub hints',
        (tester) async {
      await pumpWithPendingGet(tester, feature, 'GitHub.com');
      expect(hintOf(tester, 0), 'GitHub username');
      expect(hintOf(tester, 1), 'ghp_... or github_pat_...');
    });

    testWidgets('github.com with explicit port keeps the GitHub hints',
        (tester) async {
      await pumpWithPendingGet(tester, feature, 'github.com:443');
      expect(hintOf(tester, 0), 'GitHub username');
      expect(hintOf(tester, 1), 'ghp_... or github_pat_...');
    });

    testWidgets('github.com with trailing dot keeps the GitHub hints',
        (tester) async {
      await pumpWithPendingGet(tester, feature, 'github.com.');
      expect(hintOf(tester, 0), 'GitHub username');
      expect(hintOf(tester, 1), 'ghp_... or github_pat_...');
    });

    testWidgets('gitlab.com gets neutral hints', (tester) async {
      await pumpWithPendingGet(tester, feature, 'gitlab.com');
      expect(hintOf(tester, 0), 'Username');
      expect(hintOf(tester, 1), 'Token or password');
      expect(find.text('Token or password:'), findsOneWidget);
      expect(find.text('Personal access token (PAT):'), findsNothing);
    });

    testWidgets('self-hosted host gets neutral hints', (tester) async {
      await pumpWithPendingGet(tester, feature, 'git.example.com');
      expect(hintOf(tester, 0), 'Username');
      expect(hintOf(tester, 1), 'Token or password');
    });
  });

  group('handler registration', () {
    test('registers git_credential handler', () {
      expect(feature.handlers, contains('git_credential'));
      expect(feature.handlers.length, 1);
    });
  });
}
