import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/app_guards.dart';
import 'package:klangk_frontend/auth/pending_redirect.dart';

/// The full public-route set as the router builds it (publicRoutes
/// constant plus a couple of plugin paths used in the tests below).
Set<String> _routesWithPlugins(Set<String> pluginPaths) =>
    {...publicRoutes, ...pluginPaths};

void main() {
  // guardAuth mutates the top-level pendingRedirect global; reset it
  // between tests so order doesn't matter.
  setUp(() => pendingRedirect = null);
  tearDown(() => pendingRedirect = null);

  group('guardBanner', () {
    test('forces /consent when a banner is required', () {
      expect(
        guardBanner(bannerRequired: true, loc: '/workspaces'),
        '/consent',
      );
      expect(
        guardBanner(bannerRequired: true, loc: '/workspace/x'),
        '/consent',
      );
    });

    test('allows /consent itself when a banner is required', () {
      expect(guardBanner(bannerRequired: true, loc: '/consent'), isNull);
    });

    test('bounces /consent to /login when no banner is pending', () {
      expect(guardBanner(bannerRequired: false, loc: '/consent'), '/login');
    });

    test('allows other routes when no banner is pending', () {
      expect(
        guardBanner(bannerRequired: false, loc: '/workspaces'),
        isNull,
      );
      expect(guardBanner(bannerRequired: false, loc: '/login'), isNull);
    });
  });

  group('guardAuth', () {
    test('sends logged-out users on non-public routes to /login', () {
      expect(
        guardAuth(
          isLoggedIn: false,
          loc: '/workspaces',
          publicRoutes: publicRoutes,
          currentUri: '/workspaces',
        ),
        '/login',
      );
    });

    test('remembers the intended destination in pendingRedirect', () {
      expect(
        guardAuth(
          isLoggedIn: false,
          loc: '/workspace/abc',
          publicRoutes: publicRoutes,
          currentUri: '/workspace/abc?file=main.dart',
        ),
        '/login',
      );
      expect(pendingRedirect, '/workspace/abc?file=main.dart');
    });

    test('does not remember / or /workspaces as a return target', () {
      guardAuth(
        isLoggedIn: false,
        loc: '/',
        publicRoutes: publicRoutes,
        currentUri: '/',
      );
      expect(pendingRedirect, isNull);
      guardAuth(
        isLoggedIn: false,
        loc: '/workspaces',
        publicRoutes: publicRoutes,
        currentUri: '/workspaces',
      );
      expect(pendingRedirect, isNull);
    });

    test('allows logged-out users on public routes', () {
      expect(
        guardAuth(
          isLoggedIn: false,
          loc: '/login',
          publicRoutes: publicRoutes,
          currentUri: '/login',
        ),
        isNull,
      );
      expect(pendingRedirect, isNull);
    });

    test('allows logged-in users (no opinion)', () {
      expect(
        guardAuth(
          isLoggedIn: true,
          loc: '/workspaces',
          publicRoutes: publicRoutes,
          currentUri: '/workspaces',
        ),
        isNull,
      );
    });
  });

  group('guardLoggedInPublicRoute', () {
    final pluginPaths = {'/celebrate'};
    final routes = _routesWithPlugins(pluginPaths);

    test('bounces logged-in users on public routes to pendingRedirect', () {
      pendingRedirect = '/workspace/abc';
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: true,
          loc: '/login',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        '/workspace/abc',
      );
    });

    test('falls back to /workspaces with no pending redirect', () {
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: true,
          loc: '/login',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        '/workspaces',
      );
    });

    test('does not bounce for plugin routes (public but legitimate)', () {
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: true,
          loc: '/celebrate',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        isNull,
      );
    });

    test('does not bounce for non-public routes', () {
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: true,
          loc: '/workspaces',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        isNull,
      );
    });

    test('does not bounce for logged-out users', () {
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: false,
          loc: '/login',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        isNull,
      );
    });
  });

  group('guardRoot', () {
    test('sends logged-in users at / to /workspaces', () {
      expect(guardRoot(isLoggedIn: true, loc: '/'), '/workspaces');
    });

    test('allows logged-out users at /', () {
      expect(guardRoot(isLoggedIn: false, loc: '/'), isNull);
    });

    test('allows non-root locations', () {
      expect(guardRoot(isLoggedIn: true, loc: '/workspaces'), isNull);
    });
  });

  group('evaluateGuards precedence', () {
    final pluginPaths = {'/celebrate'};
    final routes = _routesWithPlugins(pluginPaths);

    test('banner takes precedence over everything', () {
      // Logged-out user on a protected route, but banner required ->
      // sent to /consent, not /login, and pendingRedirect untouched.
      expect(
        evaluateGuards(
          isLoggedIn: false,
          bannerRequired: true,
          loc: '/workspaces',
          currentUri: '/workspaces',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        '/consent',
      );
      expect(pendingRedirect, isNull);
    });

    test('logged-out protected route -> /login with pendingRedirect', () {
      expect(
        evaluateGuards(
          isLoggedIn: false,
          bannerRequired: false,
          loc: '/workspace/abc',
          currentUri: '/workspace/abc?x=1',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        '/login',
      );
      expect(pendingRedirect, '/workspace/abc?x=1');
    });

    test('logged-in on /login -> pendingRedirect', () {
      pendingRedirect = '/workspace/zzz';
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: false,
          loc: '/login',
          currentUri: '/login',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        '/workspace/zzz',
      );
    });

    test('logged-in on / -> /workspaces (root, not public-route guard)', () {
      // '/' is not in publicRoutes, so the public-route guard skips;
      // the root guard then redirects.
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: false,
          loc: '/',
          currentUri: '/',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        '/workspaces',
      );
    });

    test('logged-in on /workspaces -> allowed (null)', () {
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: false,
          loc: '/workspaces',
          currentUri: '/workspaces',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        isNull,
      );
    });

    test('logged-in on plugin route -> allowed (null)', () {
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: false,
          loc: '/celebrate',
          currentUri: '/celebrate',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        isNull,
      );
    });

    test('logged-out on /consent with no banner -> /login', () {
      expect(
        evaluateGuards(
          isLoggedIn: false,
          bannerRequired: false,
          loc: '/consent',
          currentUri: '/consent',
          publicRoutes: routes,
          pluginPaths: pluginPaths,
        ),
        '/login',
      );
    });
  });
}
