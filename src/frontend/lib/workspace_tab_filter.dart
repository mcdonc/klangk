import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Registers a feature's workspace tab into the [WorkspaceTabRegistry] only
/// when the feature is in [activeFeatureNames] — the tab analogue of the
/// tool-plugin active-set filter inlined in `main()` (#1975).
///
/// Lives in its own module (not `main.dart`) so it is unit-testable in
/// isolation: importing `main.dart` from a test pulls the entire app graph
/// (app shell, workspace page, auth/debug/sharing panels, …) into the
/// coverage instrument set, and those aren't unit-tested — which would break
/// the frontend's 100% coverage gate. This module imports only the plugin
/// API, so its transitive closure is tiny and fully coverable. `main()`
/// passes the generated `createAllNamedWorkspaceTabs()` aggregator output.
///
/// `activeFeatureNames` is a [Set], so [Set.contains] is exact-name equality
/// (not substring) — "git" does not activate "git-credential", same as tools.
void registerActiveWorkspaceTabs(
  Iterable<({String name, WorkspaceTabPlugin tab})> allTabs,
  Set<String> activeFeatureNames,
) {
  final tabRegistry = WorkspaceTabRegistry();
  for (final entry in allTabs) {
    if (activeFeatureNames.contains(entry.name)) {
      tabRegistry.register(entry.tab);
    }
  }
}
