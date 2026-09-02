/// Mount policy for the Terminal tab (#3023).
///
/// Pure predicate, kept outside [WorkspacePage] so the gate is
/// unit-tested directly (importing the page from a test would pull its
/// whole widget tree into the coverage denominator) — the same pattern
/// as consent_surface.dart (#2883). Both delegate the permission check
/// to permGranted (permission_gate.dart), the single source of the
/// wildcard semantics.

import 'permission_gate.dart';

/// Whether the Terminal tab may mount for this member: they must hold
/// `terminal` (or the `*` wildcard). Since the workspace_connect gate
/// moved to `join-workspace` (#2975), `terminal` is the Terminal-tab
/// visibility signal — a files-only member (`join-workspace` +
/// `files-view`) gets the workspace page with no Terminal tab. The same
/// predicate also gates the spectator auto-join
/// (`_onClientUpdate` in workspace_page.dart): without `terminal` there
/// is no Terminal surface at all, so nothing subscribes to shared PTY
/// frames. The inner gates (`code-in-isolation`,
/// `spectate-on-shared-terminals`) apply inside the mounted tab and are
/// unaffected — do not "simplify" one call site away from the other.
bool terminalTabAllowed({required List<String> permissions}) {
  return permGranted(permissions: permissions, permission: 'terminal');
}
