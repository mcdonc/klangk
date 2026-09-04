// Redirect guards extracted from KlangkApp's GoRouter. See #955.
//
// Each guard takes the inputs it needs and returns the redirect target
// (a path) or null to allow the navigation. They are top-level functions
// (not closures inside _createRouter) so they can be unit-tested in
// isolation; _createRouter just wires them up in precedence order.
//
// The order matters: the first guard to return non-null wins. The
// precedence matches the original inline redirect callback:
//   banner -> auth -> logged-in-on-public -> admin -> root.

import 'auth/pending_redirect.dart';

/// Set of routes reachable without being logged in.
///
/// Feature paths are appended by the caller, since they depend on the
/// installed features and are not known at compile time.
const Set<String> publicRoutes = {
  '/login',
  '/verify',
  '/forgot-password',
  '/reset-password',
  '/accept-invite',
  '/oidc-complete',
  '/consent',
};

/// Banner gate.
///
/// When a banner must be accepted, force every route to `/consent`
/// (allowing `/consent` itself). When no banner is pending, a visit to
/// `/consent` bounces to `/login` — the consent page is only meaningful
/// while a banner is required.
///
/// Returns the redirect target, or null to allow.
String? guardBanner({required bool bannerRequired, required String loc}) {
  if (bannerRequired) {
    return loc == '/consent' ? null : '/consent';
  }
  if (loc == '/consent') {
    return '/login';
  }
  return null;
}

/// Auth gate.
///
/// Logged-out users hitting a non-public route are sent to `/login`.
/// Their intended destination is stashed in [pendingRedirect] (unless
/// it was `/` or `/workspaces`, which have no value as a return target)
/// so login can send them back. This guard has a side effect on the
/// [pendingRedirect] global, matching the legacy inline behavior.
///
/// Returns the redirect target, or null to allow.
String? guardAuth({
  required bool isLoggedIn,
  required String loc,
  required Set<String> publicRoutes,
  required String currentUri,
}) {
  if (!isLoggedIn && !publicRoutes.contains(loc)) {
    if (loc != '/' && loc != '/workspaces') {
      pendingRedirect = currentUri;
    }
    return '/login';
  }
  return null;
}

/// Logged-in-on-public gate.
///
/// A logged-in user landing on a public route (e.g. `/login` after a
/// refresh) is bounced to their pending redirect, or `/workspaces`.
/// Feature routes are excluded: they are public but a logged-in user may
/// legitimately navigate to them.
///
/// The target is permission-checked against the *current* session: an
/// `/admin`-prefixed target (e.g. stashed by an admin's logout or expiry,
/// then inherited by whoever logs in next on this browser) falls back to
/// `/workspaces` unless [canAccessAdmin] (#2670).
///
/// The stash is deliberately NOT consumed here. GoRouter re-parses the
/// *committed* location on every refreshListenable notification, and
/// AuthService.login() fires two notifications in quick succession
/// (saveToken's, then its finally-block's). Consuming on the first
/// evaluation made the second — still re-parsing `/login` — see a null
/// stash and win the navigation with the `/workspaces` fallback (#2670).
/// The cross-session leak is instead cut where it originates:
/// `_clearToken()` clears the stash on logout / any session-ending 401.
///
/// Returns the redirect target, or null to allow.
String? guardLoggedInPublicRoute({
  required bool isLoggedIn,
  required String loc,
  required Set<String> publicRoutes,
  required Set<String> featurePaths,
  required bool canAccessAdmin,
}) {
  if (isLoggedIn && publicRoutes.contains(loc) && !featurePaths.contains(loc)) {
    final target = pendingRedirect;
    if (target == null) return '/workspaces';
    if (target.startsWith('/admin') && !canAccessAdmin) {
      return '/workspaces';
    }
    return target;
  }
  return null;
}

/// Admin-route gate (#2669).
///
/// A logged-in user who cannot enter the admin section at all (see
/// [AuthService.canAdminSection]) on an `/admin`-prefixed route is
/// bounced to `/workspaces` — the route is reachable by URL or a stale
/// redirect even though the app-bar admin icon is gated, and its page is
/// a dead end ("No admin sections available") for them.
///
/// Fires only for *logged-in* users: a logged-out visitor must keep the
/// `guardAuth` flow (stash + `/login`), and `guardLoggedInPublicRoute`
/// already rejects `/admin`-prefixed stashes for non-admins on login
/// (#2670), so the two checks meet in the middle.
///
/// Loop safety: the target `/workspaces` is not `/admin`-refixed and no
/// guard redirects away from it for a logged-in user, so this can never
/// re-enter itself; the guard is pure w.r.t. its inputs, so repeated
/// evaluations of the same committed location (GoRouter re-parses on
/// every refreshListenable notification) all agree.
String? guardAdminRoute({
  required bool isLoggedIn,
  required bool canAccessAdmin,
  required String loc,
}) {
  if (isLoggedIn && !canAccessAdmin && loc.startsWith('/admin')) {
    return '/workspaces';
  }
  return null;
}

/// Forced password change gate (#3172).
///
/// A logged-in user whose session carries `must_change_password` is
/// forced to `/change-password` on every route except `/change-password`
/// itself. Like the banner gate, this is terminal — no other guard runs
/// while the flag is set.
String? guardForcedPasswordChange({
  required bool isLoggedIn,
  required bool mustChangePassword,
  required String loc,
}) {
  if (!isLoggedIn || !mustChangePassword) return null;
  return loc == '/change-password' ? null : '/change-password';
}

/// Root shortcut: a logged-in user at `/` goes to `/workspaces`.
///
/// Returns the redirect target, or null to allow.
String? guardRoot({required bool isLoggedIn, required String loc}) {
  if (isLoggedIn && loc == '/') return '/workspaces';
  return null;
}

/// Run the redirect guards in precedence order and return the first
/// non-null redirect target, or null if all guards allow.
///
/// [publicRoutes] should already include the feature paths; [featurePaths]
/// is passed separately so [guardLoggedInPublicRoute] can exclude them.
///
/// While a banner is pending, the banner gate is **terminal**: `/consent`
/// is the only legal location, for logged-out and logged-in users alike.
/// Falling through to the guards below would let [guardLoggedInPublicRoute]
/// bounce a logged-in user off `/consent` (a public route) straight back
/// into the banner gate — the `/consent => /workspaces => /consent`
/// redirect loop hit whenever `bannerRequired` is true with a persisted
/// token (`login_banner_every_visit`, or a changed banner text).
String? evaluateGuards({
  required bool isLoggedIn,
  required bool bannerRequired,
  required bool mustChangePassword,
  required String loc,
  required String currentUri,
  required Set<String> publicRoutes,
  required Set<String> featurePaths,
  required bool canAccessAdmin,
}) {
  if (bannerRequired) {
    return guardBanner(bannerRequired: true, loc: loc);
  }
  return guardBanner(bannerRequired: false, loc: loc) ??
      guardAuth(
        isLoggedIn: isLoggedIn,
        loc: loc,
        publicRoutes: publicRoutes,
        currentUri: currentUri,
      ) ??
      guardForcedPasswordChange(
        isLoggedIn: isLoggedIn,
        mustChangePassword: mustChangePassword,
        loc: loc,
      ) ??
      guardLoggedInPublicRoute(
        isLoggedIn: isLoggedIn,
        loc: loc,
        publicRoutes: publicRoutes,
        featurePaths: featurePaths,
        canAccessAdmin: canAccessAdmin,
      ) ??
      guardAdminRoute(
        isLoggedIn: isLoggedIn,
        canAccessAdmin: canAccessAdmin,
        loc: loc,
      ) ??
      guardRoot(isLoggedIn: isLoggedIn, loc: loc);
}
