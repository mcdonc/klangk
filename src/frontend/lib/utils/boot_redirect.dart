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
/// authorization result: a code (approval) or an error (denial), each
/// with the binding state — else null (a normal boot).
String? gitAuthCallbackLocation(Map<String, String> capturedQuery) {
  final state = capturedQuery['state'];
  if (state == null || state.isEmpty) return null;
  final code = capturedQuery['code'];
  final error = capturedQuery['error'];
  final hasCode = code != null && code.isNotEmpty;
  final hasError = error != null && error.isNotEmpty;
  if (!hasCode && !hasError) return null;
  return gitAuthCallbackRoute;
}
