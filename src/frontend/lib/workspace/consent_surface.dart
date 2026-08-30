/// Mount policy for the consent-decider surface (#2246, #2883).
///
/// Pure predicate, kept outside [WorkspacePage] so the gate is
/// unit-tested directly (importing the page from a test would pull its
/// whole widget tree into the coverage denominator).

/// Whether the consent-decider surface (the consent banner and the
/// Network tab) may mount for this member: the workspace must be in
/// interactive egress mode (#2246) AND the member must hold
/// `egress-consent` (or the `*` wildcard) — spectators are watch-only
/// and never decide egress (#2883).
bool consentSurfaceAllowed({
  required String egressMode,
  required List<String> permissions,
}) {
  if (egressMode != 'interactive') return false;
  return permissions.contains('egress-consent') || permissions.contains('*');
}
