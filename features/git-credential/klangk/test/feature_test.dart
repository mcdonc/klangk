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

      final gh = jsonDecode(
        await feature.handlers['git_credential']!({
          'operation': 'get',
          'protocol': 'https',
          'host': 'github.com',
        }),
      );
      expect(gh['username'], 'gh-user');

      final gl = jsonDecode(
        await feature.handlers['git_credential']!({
          'operation': 'get',
          'protocol': 'https',
          'host': 'gitlab.com',
        }),
      );
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

      final result = jsonDecode(
        await feature.handlers['git_credential']!({
          'operation': 'get',
          'protocol': 'https',
          'host': 'github.com',
        }),
      );
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

      final result = jsonDecode(
        await feature.handlers['git_credential']!({
          'operation': 'peek',
          'protocol': 'https',
          'host': 'github.com',
        }),
      );
      expect(result['username'], 'x-access-token');
      expect(result['password'], 'gho_abc123');
    });

    test('returns miss immediately on empty cache', () async {
      final result = jsonDecode(
        await feature.handlers['git_credential']!({
          'operation': 'peek',
          'protocol': 'https',
          'host': 'github.com',
        }),
      );
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

      final result = jsonDecode(
        await feature.handlers['git_credential']!({
          'operation': 'peek',
          'protocol': 'https',
          'host': 'github.com',
        }),
      );
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
      expect(
        completed,
        isTrue,
        reason: 'peek must resolve without waiting for a dialog',
      );
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

  group('device flow dialog', () {
    Widget overlayHost(GitCredentialFeature feature) => MaterialApp(
          home: Scaffold(
            body: Builder(
              builder: (context) =>
                  Stack(children: [feature.buildOverlay(context)!]),
            ),
          ),
        );

    Future<void> pumpWithDeviceFlow(
      WidgetTester tester,
      GitCredentialFeature feature,
      Map<String, dynamic> payload,
    ) async {
      await feature.handlers['git_credential']!({
        'operation': 'device_flow_show',
        'protocol': 'https',
        ...payload,
      });
      await tester.pumpWidget(overlayHost(feature));
      await tester.pump();
    }

    testWidgets('names the provider host from the helper', (tester) async {
      await pumpWithDeviceFlow(tester, feature, {
        'host': 'gitlab.com',
        'user_code': 'ABCD-1234',
        'verification_uri': 'https://gitlab.com/oauth/authorize_device',
      });
      expect(find.text('Sign in to gitlab.com'), findsOneWidget);
      expect(find.text('Enter this code at gitlab.com:'), findsOneWidget);
      expect(find.byType(SelectableText), findsOneWidget);
    });

    testWidgets('falls back to github.com when host is absent', (tester) async {
      // An older container helper that doesn't send the provider host —
      // the dialog must still render a sensible title.
      await pumpWithDeviceFlow(tester, feature, {
        'user_code': 'ABCD-1234',
        'verification_uri': 'https://github.com/login/device',
      });
      expect(find.text('Sign in to github.com'), findsOneWidget);
      expect(find.text('Enter this code at github.com:'), findsOneWidget);
    });

    testWidgets('shows the error message from device_flow_error', (
      tester,
    ) async {
      await feature.handlers['git_credential']!({
        'operation': 'device_flow_error',
        'protocol': 'https',
        'host': 'gitlab.com',
        'error': 'Code expired. Please try again.',
      });
      await tester.pumpWidget(overlayHost(feature));
      await tester.pump();
      expect(find.text('Code expired. Please try again.'), findsOneWidget);
      expect(find.text('Falling back to manual auth...'), findsOneWidget);
      // The error state keeps the provider host — a failed GitLab flow
      // must not relabel the dialog as GitHub.
      expect(find.text('Sign in to gitlab.com'), findsOneWidget);
    });
  });

  group('verification URI auto-open gate', () {
    test('https URIs are auto-open candidates', () {
      expect(
        shouldAutoOpenVerificationUri('https://gitlab.com/oauth/device'),
        isTrue,
      );
    });

    test('non-https URIs are never auto-opened', () {
      // The provider map is ad-hoc settable from a workspace shell; a
      // hostile entry must not be able to pop arbitrary pages.
      expect(
        shouldAutoOpenVerificationUri('http://gitlab.com/oauth/device'),
        isFalse,
      );
      expect(shouldAutoOpenVerificationUri('javascript:alert(1)'), isFalse);
      expect(shouldAutoOpenVerificationUri('data:text/html,x'), isFalse);
      expect(shouldAutoOpenVerificationUri(''), isFalse);
    });
  });

  group('credential dialog hints', () {
    Widget overlayHost(GitCredentialFeature feature) => MaterialApp(
          home: Scaffold(
            body: Builder(
              builder: (context) =>
                  Stack(children: [feature.buildOverlay(context)!]),
            ),
          ),
        );

    Future<void> pumpWithPendingGet(
      WidgetTester tester,
      GitCredentialFeature feature,
      String host,
    ) async {
      // Cache-miss get blocks on the dialog completer; do not await it.
      unawaited(
        feature.handlers['git_credential']!({
          'operation': 'get',
          'protocol': 'https',
          'host': host,
        }),
      );
      await tester.pumpWidget(overlayHost(feature));
      await tester.pump();
    }

    String? hintOf(WidgetTester tester, int textFieldIndex) {
      final field = tester.widget<TextField>(
        find.byType(TextField).at(textFieldIndex),
      );
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

    testWidgets('uppercase GitHub.com host keeps the GitHub hints', (
      tester,
    ) async {
      await pumpWithPendingGet(tester, feature, 'GitHub.com');
      expect(hintOf(tester, 0), 'GitHub username');
      expect(hintOf(tester, 1), 'ghp_... or github_pat_...');
    });

    testWidgets('github.com with explicit port keeps the GitHub hints', (
      tester,
    ) async {
      await pumpWithPendingGet(tester, feature, 'github.com:443');
      expect(hintOf(tester, 0), 'GitHub username');
      expect(hintOf(tester, 1), 'ghp_... or github_pat_...');
    });

    testWidgets('github.com with trailing dot keeps the GitHub hints', (
      tester,
    ) async {
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

  group('auth flow operations', () {
    Widget overlayHost(GitCredentialFeature feature) => MaterialApp(
          home: Scaffold(
            body: Builder(
              builder: (context) =>
                  Stack(children: [feature.buildOverlay(context)!]),
            ),
          ),
        );

    late StreamController<Map<String, String>> messages;

    setUp(() {
      messages = StreamController<Map<String, String>>.broadcast();
      feature = GitCredentialFeature(authMessages: messages.stream);
    });

    tearDown(() async {
      // The outer tearDown disposes `feature`; close the stream only.
      await messages.close();
    });

    Future<String> startFlow() {
      return feature.handlers['git_credential']!({
        'operation': 'auth_flow_start',
        'protocol': 'https',
        'host': 'git.example.com',
        'authorize_url': 'https://git.example.com/login/oauth/authorize',
        'state': 'state-1',
      });
    }

    test('delivers the code when the popup message matches state', () async {
      final pending = startFlow();
      await Future<void>.delayed(Duration.zero);
      messages.add({'code': 'auth-code-1', 'state': 'state-1'});
      expect(jsonDecode(await pending), {
        'code': 'auth-code-1',
        'state': 'state-1',
      });
    });

    test('ignores a message with a mismatched state', () async {
      final pending = startFlow();
      await Future<void>.delayed(Duration.zero);
      messages.add({'code': 'evil', 'state': 'other-state'});
      messages.add({'code': 'auth-code-2', 'state': 'state-1'});
      expect(jsonDecode(await pending)['code'], 'auth-code-2');
    });

    test('ignores messages when no flow is pending', () async {
      messages.add({'code': 'unsolicited', 'state': 'state-1'});
      await Future<void>.delayed(Duration.zero);
      final result = await feature.handlers['git_credential']!({
        'operation': 'peek',
        'protocol': 'https',
        'host': 'git.example.com',
      });
      expect(jsonDecode(result), {'error': 'miss'});
    });

    test('a denial message (error + state) cancels the flow', () async {
      final pending = startFlow();
      await Future<void>.delayed(Duration.zero);
      messages.add({'error': 'access_denied', 'state': 'state-1'});
      expect(jsonDecode(await pending), {'error': 'cancelled'});
    });

    test('a second flow displaces the first with a cancellation', () async {
      final first = startFlow();
      await Future<void>.delayed(Duration.zero);
      final second = startFlow();
      await Future<void>.delayed(Duration.zero);
      messages.add({'code': 'second-code', 'state': 'state-1'});
      expect(jsonDecode(await first), {'error': 'cancelled'});
      expect(jsonDecode(await second)['code'], 'second-code');
    });

    test('a duplicate delivery does not double-complete', () async {
      final pending = startFlow();
      await Future<void>.delayed(Duration.zero);
      messages.add({'code': 'once', 'state': 'state-1'});
      await Future<void>.delayed(Duration.zero);
      messages.add({'code': 'again', 'state': 'state-1'});
      expect(jsonDecode(await pending)['code'], 'once');
    });

    testWidgets('cancel answers with an error and clears the dialog', (
      tester,
    ) async {
      final pending = startFlow();
      await tester.pumpWidget(overlayHost(feature));
      await tester.pump();
      expect(find.textContaining('Waiting for authorization'), findsOneWidget);
      expect(feature.routes.map((r) => r.path), contains('/git-auth-callback'));
      // The overlay dialog shows the authorize host.
      expect(find.textContaining('Sign in to git.example.com'), findsOneWidget);
      // Cancel via the dialog button.
      await tester.tap(find.text('Cancel'));
      await tester.pump();
      expect(jsonDecode(await pending), {'error': 'cancelled'});
      await tester.pump();
      expect(find.textContaining('Waiting for authorization'), findsNothing);
    });
  });

  group('OAuth credential cache (#3385)', () {
    test('peek returns refresh fields for an OAuth credential', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'git.example.com',
        'username': 'oauth2',
        'password': 'tok',
        'refresh_token': 'rt-1',
        'expires_at': 4102444800,
      });
      final result = await feature.handlers['git_credential']!({
        'operation': 'peek',
        'protocol': 'https',
        'host': 'git.example.com',
      });
      final decoded = jsonDecode(result) as Map<String, dynamic>;
      expect(decoded['refresh_token'], 'rt-1');
      expect(decoded['expires_at'], 4102444800);
    });

    test('git store without refresh fields keeps the cached ones', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'git.example.com',
        'username': 'oauth2',
        'password': 'tok',
        'refresh_token': 'rt-1',
        'expires_at': 4102444800,
      });
      // git's own store call after a successful push carries u/p only.
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'git.example.com',
        'username': 'oauth2',
        'password': 'tok',
      });
      final result = await feature.handlers['git_credential']!({
        'operation': 'peek',
        'protocol': 'https',
        'host': 'git.example.com',
      });
      final decoded = jsonDecode(result) as Map<String, dynamic>;
      expect(decoded['refresh_token'], 'rt-1');
    });

    test('a plain PAT credential peeks without refresh fields', () async {
      await feature.handlers['git_credential']!({
        'operation': 'store',
        'protocol': 'https',
        'host': 'github.com',
        'username': 'octocat',
        'password': 'ghp_x',
      });
      final result = await feature.handlers['git_credential']!({
        'operation': 'peek',
        'protocol': 'https',
        'host': 'github.com',
      });
      expect(jsonDecode(result).containsKey('refresh_token'), isFalse);
    });
  });

  group('callback route', () {
    test('registers the popup callback page', () {
      expect(feature.routes.map((r) => r.path), contains('/git-auth-callback'));
    });
  });
}
