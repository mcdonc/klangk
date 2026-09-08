import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/app_guards.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/auth/oidc_complete_page.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

http.Client _mockClient({
  int exchangeStatus = 400,
  String? accessToken,
  String bannerText = '',
}) {
  return MockClient((request) async {
    if (request.url.path.contains('/api/v1/config')) {
      return http.Response(
        jsonEncode({
          'registration_enabled': true,
          'login_banner': bannerText,
        }),
        200,
      );
    }
    if (request.url.path.contains('/api/v1/auth/oidc/exchange')) {
      if (accessToken != null) {
        return http.Response(
          jsonEncode({'access_token': accessToken}),
          200,
        );
      }
      return http.Response(
        jsonEncode({'detail': 'Login code is invalid or expired.'}),
        exchangeStatus,
      );
    }
    if (request.url.path.contains('/api/v1/my-permissions')) {
      return http.Response(
        jsonEncode({'permissions': {}, 'groups': [], 'is_admin': false}),
        200,
      );
    }
    return http.Response('Not found', 404);
  });
}

/// A JWT-shaped token whose claims are exactly [claims] (no `exp` by
/// default, so no token-refresh timer is scheduled in tests).
String _jwt(Map<String, dynamic> claims) {
  String enc(Map<String, dynamic> m) =>
      base64Url.encode(utf8.encode(jsonEncode(m))).replaceAll('=', '');
  return '${enc({'alg': 'HS256', 'typ': 'JWT'})}.${enc(claims)}.sig';
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

  /// The app's real redirect wiring (evaluateGuards, see app.dart), so
  /// the page is exercised under the actual guard precedence.
  Widget buildApp(AuthService auth, {required String code}) {
    final router = GoRouter(
      initialLocation: '/oidc-complete',
      refreshListenable: auth,
      redirect: (context, state) => evaluateGuards(
        isLoggedIn: auth.isLoggedIn,
        bannerRequired: auth.bannerRequired,
        mustChangePassword: auth.mustChangePassword,
        loc: state.matchedLocation,
        currentUri: state.uri.toString(),
        publicRoutes: publicRoutes,
        featurePaths: const {},
        canAccessAdmin: auth.canAdminSection,
      ),
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
        GoRoute(
          path: '/consent',
          builder: (_, __) => const Scaffold(body: Text('consent page')),
        ),
        GoRoute(
          path: '/workspaces',
          builder: (_, __) => const Scaffold(body: Text('workspace list')),
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

    testWidgets('malformed 200 body maps to the stable error (#3223)',
        (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({'registration_enabled': true}),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/auth/oidc/exchange')) {
          // 200 with a non-JSON body: jsonDecode throws inside the
          // try, exercising the catch's stable-message path
          return http.Response('<html>gateway oops</html>', 200);
        }
        return http.Response('Not found', 404);
      });

      final auth = AuthService();
      await tester.pumpWidget(buildApp(auth, code: 'fresh-code'));
      await tester.pumpAndSettle();

      expect(find.text('Login code exchange failed.'), findsOneWidget);
      expect(find.text('Go to Login'), findsOneWidget);
      expect(find.byType(CircularProgressIndicator), findsNothing);
    });

    testWidgets(
        'redeems the code under a pending banner, then the '
        'guards send the session to /consent (#3371)', (tester) async {
      final token = _jwt({'sub': 'user-1'});
      testAuthHttpClientOverride =
          _mockClient(accessToken: token, bannerText: 'Accept these terms.');

      final auth = AuthService();
      // let the constructor's config fetch land before the router is
      // built (app.dart gates router creation on auth.initialized) —
      // driven with pumps: a real Future.delayed never fires inside a
      // testWidgets FakeAsync zone
      await tester.pumpWidget(const SizedBox());
      await tester.pump();
      expect(auth.bannerRequired, isTrue);

      await tester.pumpWidget(buildApp(auth, code: 'fresh-code'));
      // the banner-pending exemption let the page mount and redeem…
      await tester.pumpAndSettle();

      expect(auth.isLoggedIn, isTrue);
      expect(auth.token, token);
      // …and the re-run guards then hand the session to /consent before
      // any app route loads — not /workspaces, not /login
      expect(find.text('consent page'), findsOneWidget);
      expect(find.text('workspace list'), findsNothing);
      expect(find.text('Go to Login'), findsNothing);
    });

    testWidgets('redeems the code and lands on /workspaces with no banner',
        (tester) async {
      final token = _jwt({'sub': 'user-1'});
      testAuthHttpClientOverride = _mockClient(accessToken: token);

      final auth = AuthService();
      await tester.pumpWidget(const SizedBox());
      await tester.pump();
      expect(auth.bannerRequired, isFalse);

      await tester.pumpWidget(buildApp(auth, code: 'fresh-code'));
      await tester.pumpAndSettle();

      expect(auth.isLoggedIn, isTrue);
      // logged in on the public /oidc-complete route -> public-route
      // guard bounces to /workspaces
      expect(find.text('workspace list'), findsOneWidget);
    });
  });
}
