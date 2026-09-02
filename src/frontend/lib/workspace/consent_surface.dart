/// Mount policy for the consent-decider surface (#2246, #2883).
///
/// Pure predicate, kept outside [WorkspacePage] so the gate is
/// unit-tested directly (importing the page from a test would pull its
/// whole widget tree into the coverage denominator).

import 'permission_gate.dart';

/// Whether the consent-decider surface (the consent banner and the
/// Network tab) may mount for this member: the workspace must be in
/// interactive egress mode (#2246) AND the member must hold
/// `egress-consent` (or the `*` wildcard) — spectators are watch-only
/// and never decide egress (#2883). The permission check delegates to
/// permGranted (permission_gate.dart), the single source of the
/// wildcard semantics.
bool consentSurfaceAllowed({
  required String egressMode,
  required List<String> permissions,
}) {
  if (egressMode != 'interactive') return false;
  return permGranted(permissions: permissions, permission: 'egress-consent');
}
