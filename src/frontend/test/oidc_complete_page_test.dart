import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/auth/oidc_complete_page.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

http.Client _mockClient({int exchangeStatus = 400}) {
  return MockClient((request) async {
    if (request.url.path.contains('/api/v1/config')) {
      return http.Response(
        jsonEncode({'registration_enabled': true}),
        200,
      );
    }
    if (request.url.path.contains('/api/v1/auth/oidc/exchange')) {
      return http.Response(
        jsonEncode({'detail': 'Login code is invalid or expired.'}),
        exchangeStatus,
      );
    }
    return http.Response('Not found', 404);
  });
}

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

  /// Minimal router so the error card's "Go to Login" button can be
  /// exercised for real (it navigates via context.go).
  Widget buildApp(AuthService auth, {required String code}) {
    final router = GoRouter(
      initialLocation: '/oidc-complete',
      routes: [
        GoRoute(
          path: '/oidc-complete',
          builder: (_, __) => ChangeNotifierProvider.value(
            value: auth,
            child: OidcCompletePage(code: code),
          ),
        ),
        GoRoute(
          path: '/login',
          builder: (_, __) => const Scaffold(body: Text('login page')),
        ),
      ],
    );
    return MaterialApp.router(routerConfig: router);
  }

  group('OidcCompletePage', () {
    testWidgets(
        'failed exchange shows the error with a recovery '
        'button that reaches /login (#3371)', (tester) async {
      testAuthHttpClientOverride = _mockClient();

      final auth = AuthService();
      await tester.pumpWidget(buildApp(auth, code: 'stale-code'));
      await tester.pumpAndSettle();

      expect(find.text('Login code exchange failed.'), findsOneWidget);
      expect(find.text('Go to Login'), findsOneWidget);

      // the recovery path: the button navigates away instead of
      // stranding the browser on the callback page (a banner-pending
      // deploy bounces /login to /consent in the real router)
      await tester.tap(find.text('Go to Login'));
      await tester.pumpAndSettle();
      expect(find.text('login page'), findsOneWidget);
    });

    testWidgets('missing code shows the error with the recovery button',
        (tester) async {
      testAuthHttpClientOverride = _mockClient();

      final auth = AuthService();
      await tester.pumpWidget(buildApp(auth, code: ''));
      await tester.pumpAndSettle();

      expect(find.text('Missing login code.'), findsOneWidget);
      expect(find.text('Go to Login'), findsOneWidget);
      expect(find.text('Login code exchange failed.'), findsNothing);
    });
  });
}
