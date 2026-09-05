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
import 'package:klangk_frontend/auth/login_page.dart';
import 'package:klangk_frontend/auth/pending_redirect.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// App-level redirect tests that drive a real GoRouter through the real
/// AuthService login flow.
///
/// The router here mirrors KlangkApp._createRouter's redirect wiring
/// (evaluateGuards over publicRoutes + featurePaths, refreshListenable on
/// auth) but mounts dummy builders — deliberately NOT the real KlangkApp:
/// importing app.dart would transitively load every page module into the
/// test isolate, and dart coverage reports all *loaded* files, so the
/// pages that have no dedicated unit tests would surface as uncovered and
/// collapse the 100% gate.
///
/// What these pin that the pure guard unit tests cannot: GoRouter
/// re-parses the *committed* location on every refreshListenable
/// notification, and login() fires two notifications back-to-back
/// (saveToken's notifyListeners, then the finally-block's microtasks
/// later). A guard that answers those evaluations differently (the
/// consume-once bug, #2670 review) sends the deep-link login to
/// /workspaces.
void main() {
  String makeJwt(Map<String, dynamic> payload) {
    final header = base64Url
        .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
        .replaceAll('=', '');
    final body =
        base64Url.encode(utf8.encode(jsonEncode(payload))).replaceAll('=', '');
    return '$header.$body.fakesig';
  }

  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
    testAuthHttpClientOverride = null;
    testConfigHttpClientOverride = null;
    pendingRedirect = null;
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
    testConfigHttpClientOverride = null;
    pendingRedirect = null;
  });

  // All HTTP the app issues during this flow: config (login page +
  // AuthService), login, and my-permissions. No token exp claim -> no
  // refresh timer for the test to fight with.
  String installMocks({
    Map<String, List<String>> permissions = const {},
    bool isAdmin = false,
  }) {
    final token = makeJwt({'sub': 'user-1', 'email': 'user@example.com'});
    testConfigHttpClientOverride = MockClient((request) async {
      return http.Response(
        jsonEncode({
          'registration_enabled': true,
          'login_banner_title': '',
          'login_banner': '',
          'oidc_providers': [],
          'auth_modes': 'password',
        }),
        200,
      );
    });
    testAuthHttpClientOverride = MockClient((request) async {
      final path = request.url.path;
      if (path.contains('/api/v1/auth/login')) {
        return http.Response(jsonEncode({'access_token': token}), 200);
      }
      if (path.contains('/api/v1/my-permissions')) {
        return http.Response(
          jsonEncode({
            'user_id': 'u1',
            'email': 'user@example.com',
            'is_admin': isAdmin,
            'permissions': permissions,
            'groups': <Map<String, dynamic>>[],
          }),
          200,
        );
      }
      if (path.contains('/api/v1/config')) {
        return http.Response(
          jsonEncode({'login_banner_title': '', 'login_banner': ''}),
          200,
        );
      }
      return http.Response('Not found', 404);
    });
    return token;
  }

  GoRouter buildRouter(AuthService auth, String initialLocation) {
    const featurePaths = <String>{};
    return GoRouter(
      initialLocation: initialLocation,
      refreshListenable: auth,
      redirect: (context, state) {
        final loc = state.matchedLocation;
        final routes = {...publicRoutes, ...featurePaths};
        return evaluateGuards(
          isLoggedIn: auth.isLoggedIn,
          bannerRequired: auth.bannerRequired,
          mustChangePassword: auth.mustChangePassword,
          loc: loc,
          currentUri: state.uri.toString(),
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: auth.isAdmin,
        );
      },
      routes: [
        GoRoute(path: '/login', builder: (context, state) => const LoginPage()),
        GoRoute(
          path: '/workspaces',
          builder: (context, state) =>
              const Scaffold(body: Center(child: Text('workspaces-list'))),
        ),
        GoRoute(
          path: '/workspace/:id',
          builder: (context, state) => Scaffold(
            body: Center(
              child: Text('workspace-${state.pathParameters['id']}'),
            ),
          ),
        ),
        GoRoute(
          path: '/admin/users',
          builder: (context, state) =>
              const Scaffold(body: Center(child: Text('admin-users'))),
        ),
      ],
    );
  }

  testWidgets(
    'deep-link login lands on the stashed workspace, not /workspaces (#2670)',
    (tester) async {
      installMocks();

      final auth = AuthService();
      final router = buildRouter(auth, '/workspace/test-ws-123');
      await tester.pumpWidget(
        ChangeNotifierProvider.value(
          value: auth,
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('Log In'), findsWidgets);
      expect(pendingRedirect, '/workspace/test-ws-123');

      // Log in through the real form — this exercises the real login()
      // double notifyListeners (saveToken + finally), which is exactly what
      // the E2E deep-link test drives.
      final fields = find.byType(TextField);
      await tester.enterText(fields.first, 'user@example.com');
      await tester.enterText(fields.last, 'password');
      await tester.tap(find.widgetWithText(FilledButton, 'Log In'));
      await tester.pumpAndSettle();

      expect(
        router.routerDelegate.currentConfiguration.uri.toString(),
        '/workspace/test-ws-123',
      );
      expect(find.text('workspace-test-ws-123'), findsOneWidget);
    },
  );

  testWidgets('plain login (no stash) lands on /workspaces', (tester) async {
    installMocks();

    final auth = AuthService();
    final router = buildRouter(auth, '/login');
    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: auth,
        child: MaterialApp.router(routerConfig: router),
      ),
    );
    await tester.pumpAndSettle();
    expect(pendingRedirect, isNull);

    final fields = find.byType(TextField);
    await tester.enterText(fields.first, 'user@example.com');
    await tester.enterText(fields.last, 'password');
    await tester.tap(find.widgetWithText(FilledButton, 'Log In'));
    await tester.pumpAndSettle();

    expect(
      router.routerDelegate.currentConfiguration.uri.toString(),
      '/workspaces',
    );
    expect(find.text('workspaces-list'), findsOneWidget);
  });

  // Restored-session tests for the admin-route guard (#2669). KlangkApp
  // builds its router only after auth initializes, so the guard's first
  // evaluation of the initial location sees the *real* session state.
  // Mirror that here: let the persisted token restore (permissions fetch
  // included) settle before mounting the router at /admin/users.
  Future<AuthService> restoreSession(
    WidgetTester tester,
    Map<String, List<String>> permissions, {
    bool isAdmin = false,
  }) async {
    final token = makeJwt({'sub': 'user-1', 'email': 'user@example.com'});
    // ignore: invalid_use_of_visible_for_testing
    SharedPreferences.setMockInitialValues({'klangk_jwt': token});
    installMocks(permissions: permissions, isAdmin: isAdmin);
    final auth = AuthService();
    // A quiet host widget: pump until _loadToken (config + permissions)
    // completes and initialized flips true.
    await tester.pumpWidget(const MaterialApp(home: SizedBox()));
    await tester.pumpAndSettle();
    expect(auth.initialized, isTrue);
    return auth;
  }

  testWidgets(
    'restored non-admin session at /admin/users lands on /workspaces, '
    'no redirect loop (#2669)',
    (tester) async {
      final auth = await restoreSession(tester, const {});
      expect(auth.isAdmin, isFalse);

      final router = buildRouter(auth, '/admin/users');
      await tester.pumpWidget(
        ChangeNotifierProvider.value(
          value: auth,
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      // The dead-end admin page must not be showing.
      expect(find.text('admin-users'), findsNothing);
      expect(
        router.routerDelegate.currentConfiguration.uri.toString(),
        '/workspaces',
      );
      expect(find.text('workspaces-list'), findsOneWidget);
    },
  );

  testWidgets('restored admin session at /admin/users stays put (#2669)', (
    tester,
  ) async {
    final auth = await restoreSession(
        tester,
        const {
          '/users': ['manage-users'],
        },
        isAdmin: true);
    expect(auth.isAdmin, isTrue);

    final router = buildRouter(auth, '/admin/users');
    await tester.pumpWidget(
      ChangeNotifierProvider.value(
        value: auth,
        child: MaterialApp.router(routerConfig: router),
      ),
    );
    await tester.pumpAndSettle();

    expect(
      router.routerDelegate.currentConfiguration.uri.toString(),
      '/admin/users',
    );
    expect(find.text('admin-users'), findsOneWidget);
  });
}
