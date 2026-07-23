// Redirect guards extracted from KlangkApp's GoRouter. See #955.
//
// Each guard takes the inputs it needs and returns the redirect target
// (a path) or null to allow the navigation. They are top-level functions
// (not closures inside _createRouter) so they can be unit-tested in
// isolation; _createRouter just wires them up in precedence order.
//
// The order matters: the first guard to return non-null wins. The
// precedence matches the original inline redirect callback:
//   banner -> auth -> logged-in-on-public -> root.

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
/// Returns the redirect target, or null to allow.
String? guardLoggedInPublicRoute({
  required bool isLoggedIn,
  required String loc,
  required Set<String> publicRoutes,
  required Set<String> featurePaths,
}) {
  if (isLoggedIn && publicRoutes.contains(loc) && !featurePaths.contains(loc)) {
    return pendingRedirect ?? '/workspaces';
  }
  return null;
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
String? evaluateGuards({
  required bool isLoggedIn,
  required bool bannerRequired,
  required String loc,
  required String currentUri,
  required Set<String> publicRoutes,
  required Set<String> featurePaths,
}) {
  return guardBanner(bannerRequired: bannerRequired, loc: loc) ??
      guardAuth(
        isLoggedIn: isLoggedIn,
        loc: loc,
        publicRoutes: publicRoutes,
        currentUri: currentUri,
      ) ??
      guardLoggedInPublicRoute(
        isLoggedIn: isLoggedIn,
        loc: loc,
        publicRoutes: publicRoutes,
        featurePaths: featurePaths,
      ) ??
      guardRoot(isLoggedIn: isLoggedIn, loc: loc);
}
