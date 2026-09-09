/// Boot-time redirect for OAuth authorize popups (#3385).
///
/// The git-credential authorization-code flow registers the klangk
/// origin root as its OAuth redirect URI, so the authorize popup lands
/// at `/?code=..&state=..` with no hash. The app captures those query
/// params before GoRouter navigation clears them (main.dart); this
/// helper decides whether that boot is a callback that should open the
/// git-credential callback route instead of the default landing page.
library;

const gitAuthCallbackRoute = '/git-auth-callback';

/// The callback route when the captured page query carries an OAuth
/// authorization code, else null (a normal boot).
String? gitAuthCallbackLocation(Map<String, String> capturedQuery) {
  final code = capturedQuery['code'];
  final state = capturedQuery['state'];
  if (code == null || code.isEmpty) return null;
  if (state == null || state.isEmpty) return null;
  return gitAuthCallbackRoute;
}
