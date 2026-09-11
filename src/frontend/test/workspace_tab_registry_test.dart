import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace_tab_filter.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Minimal tab plugin for filter tests — constructible with a title so a
/// registered factory's output is identifiable.
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
  // (register factories) and the workspace page (create instances). Reset
  // it around each test so ordering and cross-test leakage can't matter.
  setUp(WorkspaceTabRegistry().clear);
  tearDown(WorkspaceTabRegistry().clear);

  group('registerActiveWorkspaceTabs (active-set filter, #1975)', () {
    test('registers only factories whose feature is in the active set', () {
      registerActiveWorkspaceTabs(
        [
          (name: 'alpha', create: () => _FakeTab('a')),
          (name: 'beta', create: () => _FakeTab('b')),
          (name: 'gamma', create: () => _FakeTab('c')),
        ],
        {'alpha', 'gamma'},
      );

      // Only active features' factories land in the singleton registry,
      // in registration order — one fresh instance per factory per page.
      final tabs = WorkspaceTabRegistry().createTabs();
      expect(tabs.map((t) => (t as _FakeTab).title), ['a', 'c']);
    });

    test('an empty active set registers nothing', () {
      registerActiveWorkspaceTabs(
        [(name: 'alpha', create: () => _FakeTab('a'))],
        <String>{},
      );
      expect(WorkspaceTabRegistry().createTabs(), isEmpty);
    });

    test('exact-name match — "git" does not activate "git-credential"', () {
      // activeFeatureNames is a Set<String>, so .contains() is exact-name
      // equality (not substring) — mirrors the tool-plugin comment in main().
      registerActiveWorkspaceTabs(
        [
          (name: 'git', create: () => _FakeTab('git')),
          (name: 'git-credential', create: () => _FakeTab('git-credential')),
        ],
        {'git'},
      );

      final tabs = WorkspaceTabRegistry().createTabs();
      expect(tabs.map((t) => (t as _FakeTab).title), ['git']);
    });

    test(
        'main() register is visible to the workspace-page read '
        '(singleton wiring invariant)', () {
      // workspace_page.dart calls `WorkspaceTabRegistry().createTabs()`
      // from a different constructor call than main()'s register — that
      // only works because the registry is a singleton. Register via the
      // helper and instantiate via a fresh instance to prove the two
      // share state.
      registerActiveWorkspaceTabs(
        [(name: 'wired', create: () => _FakeTab('wired'))],
        {'wired'},
      );
      expect(identical(WorkspaceTabRegistry(), WorkspaceTabRegistry()), isTrue);
      expect(
        WorkspaceTabRegistry().createTabs().single,
        isA<_FakeTab>(),
      );
    });
  });

  group('WorkspaceTabRegistry (#3409: per-page tab instances)', () {
    test('createTabs returns FRESH instances on every call', () {
      WorkspaceTabRegistry().register(() => _FakeTab('a'));
      final pageOne = WorkspaceTabRegistry().createTabs();
      final pageTwo = WorkspaceTabRegistry().createTabs();

      // Same class and order, but no shared identity: a workspace page
      // disposing its set can never poison the next page's.
      expect(pageTwo.length, pageOne.length);
      for (var i = 0; i < pageOne.length; i++) {
        expect(identical(pageOne[i], pageTwo[i]), isFalse);
      }
    });

    test('clear drops the registrations and disposes nothing', () {
      // The registry owns factories, not instances — a page still holding
      // tabs it created keeps them alive across a test-reset clear.
      final owned = _FakeTab('owned');
      WorkspaceTabRegistry().register(() => owned);
      WorkspaceTabRegistry().clear();
      expect(WorkspaceTabRegistry().createTabs(), isEmpty);
      expect(owned.title, 'owned'); // untouched, still usable
    });
  });
}
