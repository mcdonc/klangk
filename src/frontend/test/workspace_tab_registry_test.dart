import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace_tab_filter.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Minimal tab plugin for filter tests — identity matters (the registry keeps
/// the same instances main() registered), so each test builds distinct ones.
class _FakeTab extends WorkspaceTabPlugin {
  _FakeTab(this.title);
  final String title;
  @override
  IconData get icon => Icons.tab;
  @override
  Widget build(BuildContext context) => Text(title);
}

void main() {
  // The registry is a process-global singleton shared between main()
  // (register) and the workspace page (read). Reset it around each test so
  // ordering and cross-test leakage can't matter.
  setUp(WorkspaceTabRegistry().disposeAll);
  tearDown(WorkspaceTabRegistry().disposeAll);

  group('registerActiveWorkspaceTabs (active-set filter, #1975)', () {
    test('registers only tabs whose feature is in the active set', () {
      final a = _FakeTab('a');
      final b = _FakeTab('b');
      final c = _FakeTab('c');

      registerActiveWorkspaceTabs(
        [
          (name: 'alpha', tab: a),
          (name: 'beta', tab: b),
          (name: 'gamma', tab: c),
        ],
        {'alpha', 'gamma'},
      );

      // Only active features' tabs land in the singleton registry, in order.
      expect(WorkspaceTabRegistry().tabs, [a, c]);
    });

    test('an empty active set registers nothing', () {
      registerActiveWorkspaceTabs(
        [(name: 'alpha', tab: _FakeTab('a'))],
        <String>{},
      );
      expect(WorkspaceTabRegistry().tabs, isEmpty);
    });

    test('exact-name match — "git" does not activate "git-credential"', () {
      // activeFeatureNames is a Set<String>, so .contains() is exact-name
      // equality (not substring) — mirrors the tool-plugin comment in main().
      final git = _FakeTab('git');
      final gitCred = _FakeTab('git-credential');

      registerActiveWorkspaceTabs(
        [
          (name: 'git', tab: git),
          (name: 'git-credential', tab: gitCred),
        ],
        {'git'},
      );

      expect(WorkspaceTabRegistry().tabs, [git]);
    });

    test(
        'main() register is visible to the workspace-page read '
        '(singleton wiring invariant)', () {
      // workspace_page.dart reads `WorkspaceTabRegistry().tabs` from a
      // different constructor call than main()'s register — that only works
      // because the registry is a singleton. Register via the helper and read
      // via a fresh instance to prove the two share state.
      final tab = _FakeTab('wired');
      registerActiveWorkspaceTabs(
        [(name: 'wired', tab: tab)],
        {'wired'},
      );
      expect(identical(WorkspaceTabRegistry(), WorkspaceTabRegistry()), isTrue);
      expect(WorkspaceTabRegistry().tabs.last, tab);
    });
  });
}
