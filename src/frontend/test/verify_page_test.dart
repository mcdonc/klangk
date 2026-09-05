import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/auth/verify_page.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
    testAuthHttpClientOverride = null;
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  GoRouter buildRouter({String token = 'some-token'}) => GoRouter(
        initialLocation: '/verify?token=$token',
        routes: [
          GoRoute(
            path: '/verify',
            builder: (context, state) =>
                VerifyPage(token: state.uri.queryParameters['token'] ?? ''),
          ),
          GoRoute(
            path: '/login',
            builder: (context, state) =>
                const Scaffold(body: Center(child: Text('login-marker'))),
          ),
        ],
      );

  Widget buildVerifyPage({String token = 'some-token'}) {
    return ChangeNotifierProvider(
      create: (_) => AuthService(),
      child: MaterialApp.router(routerConfig: buildRouter(token: token)),
    );
  }

  /// Answers config/permissions so AuthService post-login bookkeeping
  /// completes; `verify` responses are supplied per test.
  http.Client clientFor(http.Response Function() verify) {
    return MockClient((request) async {
      if (request.url.path.contains('/api/v1/auth/verify')) {
        return verify();
      }
      if (request.url.path.contains('/api/v1/config')) {
        return http.Response('{}', 200);
      }
      if (request.url.path.contains('/api/v1/my-permissions')) {
        return http.Response(
          jsonEncode({
            'user_id': 'u',
            'email': 'u',
            'permissions': {},
            'groups': [],
          }),
          200,
        );
      }
      return http.Response('Not found', 404);
    });
  }

  testWidgets('empty token shows a stable message', (tester) async {
    await tester.pumpWidget(buildVerifyPage(token: ''));
    await tester.pumpAndSettle();

    expect(find.text('Missing verification token.'), findsOneWidget);
  });

  testWidgets('200 with token logs the user in', (tester) async {
    testAuthHttpClientOverride = clientFor(
      () => http.Response(jsonEncode({'access_token': 'verified-token'}), 200),
    );

    final auth = AuthService();
    await tester.pumpWidget(
      ChangeNotifierProvider<AuthService>.value(
        value: auth,
        child: MaterialApp.router(routerConfig: buildRouter()),
      ),
    );
    // Fixed pumps, not pumpAndSettle: the page keeps its progress spinner
    // until the router redirect fires, and the spinner animates forever.
    await tester.pump();
    await tester.pump();

    expect(auth.token, 'verified-token');
    expect(auth.isLoggedIn, isTrue);
  });

  testWidgets('200 without token shows the success message', (tester) async {
    testAuthHttpClientOverride = clientFor(
      () => http.Response(jsonEncode({}), 200),
    );

    await tester.pumpWidget(buildVerifyPage());
    await tester.pumpAndSettle();

    expect(
      find.text('Your email has been verified. You can now log in.'),
      findsOneWidget,
    );
  });

  testWidgets('non-200 shows the server detail', (tester) async {
    testAuthHttpClientOverride = clientFor(
      () => http.Response(
        jsonEncode({'detail': 'Invalid or expired token'}),
        403,
      ),
    );

    await tester.pumpWidget(buildVerifyPage());
    await tester.pumpAndSettle();

    expect(find.text('Invalid or expired token'), findsOneWidget);
  });

  testWidgets('Go to Login navigates to /login', (tester) async {
    testAuthHttpClientOverride = clientFor(
      () => http.Response(
        jsonEncode({'detail': 'Invalid or expired token'}),
        403,
      ),
    );

    await tester.pumpWidget(buildVerifyPage());
    await tester.pumpAndSettle();

    await tester.tap(find.text('Go to Login'));
    await tester.pumpAndSettle();

    expect(find.text('login-marker'), findsOneWidget);
  });

  testWidgets(
    'network failure shows a stable message, not the raw exception (#3203)',
    (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        throw Exception(
          'Network unreachable: GET http://localhost:8997/api/v1/auth/verify',
        );
      });

      await tester.pumpWidget(buildVerifyPage());
      await tester.pumpAndSettle();

      expect(find.text('Network error. Please try again.'), findsOneWidget);
      expect(find.textContaining('Network unreachable'), findsNothing);
      expect(find.textContaining('Connection error'), findsNothing);
      expect(find.textContaining('localhost:8997'), findsNothing);
    },
  );

  // The authGet await can straddle an unmount (the router redirects an
  // already-tokened user off the public /verify route mid-request); every arm
  // below the await must bail instead of calling setState on a disposed
  // state (#3203 review finding).
  testWidgets('token response after unmount does not touch state',
      (tester) async {
    final completer = Completer<http.Response>();
    // Config resolves immediately so AuthService init finishes while
    // still mounted; only the verification request parks on the
    // completer.
    testAuthHttpClientOverride = MockClient((request) async {
      if (request.url.path.contains('/api/v1/config')) {
        return http.Response('{}', 200);
      }
      return completer.future;
    });

    await tester.pumpWidget(buildVerifyPage());
    await tester.pump();
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    completer.complete(
      http.Response(jsonEncode({'access_token': 't'}), 200),
    );
    await tester.pumpAndSettle();
  });

  testWidgets('network failure after unmount does not touch state',
      (tester) async {
    final completer = Completer<http.Response>();
    testAuthHttpClientOverride = MockClient((request) async {
      if (request.url.path.contains('/api/v1/config')) {
        return http.Response('{}', 200);
      }
      return completer.future;
    });

    await tester.pumpWidget(buildVerifyPage());
    await tester.pump();
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    completer.completeError(Exception('Network unreachable'));
    await tester.pumpAndSettle();
  });
}
