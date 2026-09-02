/// Single source of the workspace-permission check (#3023 review).
///
/// Pure predicate, kept outside [WorkspacePage] so every gate built on it
/// is unit-testable directly (importing the page from a test would pull
/// its whole widget tree into the coverage denominator). Both extracted
/// gates (consent_surface.dart, terminal_tab_gate.dart) and the page's
/// own `_hasPerm` delegate here, so the wildcard semantics have exactly
/// one definition.

/// Whether [permissions] — the my-permissions answer for one workspace
/// resource — grants [permission]. The `*` wildcard (owners) satisfies
/// any permission; anything else requires the literal name. Fail-closed:
/// an empty list grants nothing.
bool permGranted({
  required List<String> permissions,
  required String permission,
}) {
  return permissions.contains(permission) || permissions.contains('*');
}
