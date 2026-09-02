/// Mount policy for the Terminal tab (#3023).
///
/// Pure predicate, kept outside [WorkspacePage] so the gate is
/// unit-tested directly (importing the page from a test would pull its
/// whole widget tree into the coverage denominator) — the same pattern
/// as consent_surface.dart (#2883).

/// Whether the Terminal tab may mount for this member: they must hold
/// `terminal` (or the `*` wildcard). Since the workspace_connect gate
/// moved to `join-workspace` (#2975), `terminal` is the Terminal-tab
/// visibility signal — a files-only member (`join-workspace` +
/// `files-view`) gets the workspace page with no Terminal tab. The
/// inner gates (`code-in-isolation`, `spectate-on-shared-terminals`)
/// apply inside the mounted tab and are unaffected.
bool terminalTabAllowed({required List<String> permissions}) {
  return permissions.contains('terminal') || permissions.contains('*');
}
