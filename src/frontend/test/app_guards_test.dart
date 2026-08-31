import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/app_guards.dart';
import 'package:klangk_frontend/auth/pending_redirect.dart';

/// The full public-route set as the router builds it (publicRoutes
/// constant plus a couple of feature paths used in the tests below).
Set<String> _routesWithFeatures(Set<String> featurePaths) =>
    {...publicRoutes, ...featurePaths};

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
    final featurePaths = {'/celebrate'};
    final routes = _routesWithFeatures(featurePaths);

    test('bounces logged-in users on public routes to pendingRedirect', () {
      pendingRedirect = '/workspace/abc';
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: true,
          loc: '/login',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
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
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        '/workspaces',
      );
    });

    test('does not bounce for feature routes (public but legitimate)', () {
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: true,
          loc: '/celebrate',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
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
          featurePaths: featurePaths,
          canAccessAdmin: false,
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
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        isNull,
      );
    });

    test('guard is idempotent across repeated evaluations (#2670)', () {
      // GoRouter re-parses the committed location on every refreshListenable
      // notification, and login() notifies twice in quick succession — the
      // guard must give both evaluations the same answer. It therefore must
      // NOT consume the stash; see the guard's doc comment.
      pendingRedirect = '/workspace/abc';
      final first = guardLoggedInPublicRoute(
        isLoggedIn: true,
        loc: '/login',
        publicRoutes: routes,
        featurePaths: featurePaths,
        canAccessAdmin: false,
      );
      final second = guardLoggedInPublicRoute(
        isLoggedIn: true,
        loc: '/login',
        publicRoutes: routes,
        featurePaths: featurePaths,
        canAccessAdmin: false,
      );
      expect(first, '/workspace/abc');
      expect(second, '/workspace/abc');
    });

    test('admin-target rejection is stable across evaluations (#2670)', () {
      // A rejected target must yield the same fallback on every
      // evaluation — clearing it here would re-introduce the double-
      // notify race (the second evaluation would fall back while the
      // first had already bounced the user).
      pendingRedirect = '/admin/users';
      for (var i = 0; i < 2; i++) {
        expect(
          guardLoggedInPublicRoute(
            isLoggedIn: true,
            loc: '/login',
            publicRoutes: routes,
            featurePaths: featurePaths,
            canAccessAdmin: false,
          ),
          '/workspaces',
        );
      }
    });

    test('rejects an admin target for a non-admin session (#2670)', () {
      // Stashed by a previous (admin) session's logout or expiry.
      pendingRedirect = '/admin/users';
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: true,
          loc: '/login',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        '/workspaces',
      );
    });

    test('allows an admin target for an admin session', () {
      pendingRedirect = '/admin/users';
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: true,
          loc: '/login',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: true,
        ),
        '/admin/users',
      );
    });

    test('allows an admin target for a delegated events auditor (#2923)', () {
      // The auditor's my-permissions carries container-events on
      // /admin/container-events, so the stashed admin URL is honored.
      pendingRedirect = '/admin/users';
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: true,
          loc: '/login',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: true,
        ),
        '/admin/users',
      );
    });

    test('allows non-admin targets for a non-admin session', () {
      pendingRedirect = '/workspace/abc?file=main.dart';
      expect(
        guardLoggedInPublicRoute(
          isLoggedIn: true,
          loc: '/login',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        '/workspace/abc?file=main.dart',
      );
    });
  });

  group('guardAdminRoute', () {
    test('logged-in non-admin on /admin/users -> /workspaces (#2669)', () {
      expect(
        guardAdminRoute(
            isLoggedIn: true, canAccessAdmin: false, loc: '/admin/users'),
        '/workspaces',
      );
    });

    test('logged-in admin on /admin/users -> allowed (null)', () {
      expect(
        guardAdminRoute(
            isLoggedIn: true, canAccessAdmin: true, loc: '/admin/users'),
        isNull,
      );
    });

    test('delegated events auditor on /admin/users -> allowed (null)', () {
      // #2923: a non-wildcard principal holding only `container-events`
      // on /admin/container-events can still enter the admin section —
      // the Events tab is their only section there.
      expect(
        guardAdminRoute(
            isLoggedIn: true, canAccessAdmin: true, loc: '/admin/users'),
        isNull,
      );
    });

    test('logged-out non-admin on /admin/users -> allowed (null)', () {
      // Must not fire for logged-out visitors: guardAuth owns that case
      // (stash + /login), and firing here would strand them on the
      // workspace list without ever seeing the login form.
      expect(
        guardAdminRoute(
            isLoggedIn: false, canAccessAdmin: false, loc: '/admin/users'),
        isNull,
      );
    });

    test('non-admin on non-admin route -> allowed (null)', () {
      expect(
        guardAdminRoute(
            isLoggedIn: true, canAccessAdmin: false, loc: '/workspaces'),
        isNull,
      );
    });

    test('idempotent across repeated evaluations of the same location', () {
      // GoRouter re-parses the committed location on every
      // refreshListenable notification; the guard must answer every
      // evaluation identically (the #2670 lesson).
      final first = guardAdminRoute(
          isLoggedIn: true, canAccessAdmin: false, loc: '/admin/users');
      final second = guardAdminRoute(
          isLoggedIn: true, canAccessAdmin: false, loc: '/admin/users');
      expect(first, second);
      expect(first, '/workspaces');
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
    final featurePaths = {'/celebrate'};
    final routes = _routesWithFeatures(featurePaths);

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
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        '/consent',
      );
      expect(pendingRedirect, isNull);
    });

    test('banner gate is terminal: logged-in user stays on /consent', () {
      // Regression: with a persisted token and bannerRequired true
      // (login_banner_every_visit on a return visit), the logged-in-on-
      // public guard used to bounce /consent -> /workspaces -> /consent
      // in an infinite loop. The banner gate must decide alone here.
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: true,
          loc: '/consent',
          currentUri: '/consent',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        isNull,
      );
    });

    test('banner gate forces /consent even for logged-in users', () {
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: true,
          loc: '/workspaces',
          currentUri: '/workspaces',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        '/consent',
      );
    });

    test('logged-out protected route -> /login with pendingRedirect', () {
      expect(
        evaluateGuards(
          isLoggedIn: false,
          bannerRequired: false,
          loc: '/workspace/abc',
          currentUri: '/workspace/abc?x=1',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
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
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        '/workspace/zzz',
      );
    });

    test(
        'logged-in non-admin on /login with stale admin target -> '
        '/workspaces (#2670)', () {
      // The repro from #2670: an admin session stashed /admin/users (via
      // logout or expiry), and a non-admin logs in on the same browser.
      pendingRedirect = '/admin/users';
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: false,
          loc: '/login',
          currentUri: '/login',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        '/workspaces',
      );
      // The rejected stash is not consumed here — clearing happens on
      // session end (_clearToken). Idempotence is what makes the
      // double-notify race harmless.
      expect(pendingRedirect, '/admin/users');
    });

    test('logged-in admin on /login with admin target -> target', () {
      pendingRedirect = '/admin/users';
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: false,
          loc: '/login',
          currentUri: '/login',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: true,
        ),
        '/admin/users',
      );
    });

    test('logged-in non-admin on /admin/users -> /workspaces (#2669)', () {
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: false,
          loc: '/admin/users',
          currentUri: '/admin/users',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        '/workspaces',
      );
    });

    test('logged-in admin on /admin/users -> allowed (null) (#2669)', () {
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: false,
          loc: '/admin/users',
          currentUri: '/admin/users',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: true,
        ),
        isNull,
      );
    });

    test('logged-out on /admin/users -> /login with stash, not /workspaces',
        () {
      // The auth gate owns logged-out visitors; the admin gate must not
      // preempt it (see guardAdminRoute 'logged-out' test).
      expect(
        evaluateGuards(
          isLoggedIn: false,
          bannerRequired: false,
          loc: '/admin/users',
          currentUri: '/admin/users',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        '/login',
      );
      expect(pendingRedirect, '/admin/users');
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
          featurePaths: featurePaths,
          canAccessAdmin: false,
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
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        isNull,
      );
    });

    test('logged-in on feature route -> allowed (null)', () {
      expect(
        evaluateGuards(
          isLoggedIn: true,
          bannerRequired: false,
          loc: '/celebrate',
          currentUri: '/celebrate',
          publicRoutes: routes,
          featurePaths: featurePaths,
          canAccessAdmin: false,
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
          featurePaths: featurePaths,
          canAccessAdmin: false,
        ),
        '/login',
      );
    });
  });
}
