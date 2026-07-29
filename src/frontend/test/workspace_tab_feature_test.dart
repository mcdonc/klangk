import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/layout/ide_layout.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// A feature that contributes ONLY a workspace tab (no ToolPlugin) — the
/// shape #1975 adds framework support for. Mirrors how the eventual `chat`
/// feature will declare a tab alongside (or instead of) tool handlers.
class _TabOnlyFeature extends WorkspaceTabPlugin {
  @override
  String get title => 'Notes';

  @override
  IconData get icon => Icons.note_outlined;

  @override
  Widget build(BuildContext context) =>
      const Center(child: Text('notes panel'));
}

Widget _harness({List<WorkspaceTabPlugin> tabs = const []}) {
  return MaterialApp(
    home: Scaffold(
      body: SizedBox(
        width: 1000,
        height: 700,
        child: IdeLayout(
          fileViewer: const SizedBox(),
          terminal: const SizedBox(),
          featureTabs: tabs,
        ),
      ),
    ),
  );
}

/// A ValueNotifier that exposes hasListeners for tests (the base getter is
/// @protected, so only subclasses can read it).
class _ExposedValueNotifier<T> extends ValueNotifier<T> {
  _ExposedValueNotifier(super.value);
  bool get hasListenersPublic => hasListeners;
}

/// A feature tab that exposes a live badge and records visibility changes
/// (#1976) — the shape the chat feature uses (unread badge + mark-read-on-view).
class _BadgedTab extends WorkspaceTabPlugin {
  _BadgedTab(this.title, {TabBadge? badge})
      : _badge = _ExposedValueNotifier<TabBadge?>(badge);
  final _ExposedValueNotifier<TabBadge?> _badge;
  final List<bool> visibility = [];

  @override
  final String title;

  @override
  IconData get icon => Icons.chat_bubble_outline;

  @override
  Widget build(BuildContext context) => Center(child: Text('$title panel'));

  @override
  ValueListenable<TabBadge?>? get badge => _badge;

  @override
  void setVisible(bool visible) => visibility.add(visible);

  @override
  void dispose() => _badge.dispose();
}

/// A stateful harness that rebuilds IdeLayout with a different feature-tab
/// set — used to verify badge subscriptions are dropped when a tab is removed.
class _TabsHarness extends StatefulWidget {
  const _TabsHarness({required this.tabs});
  final List<WorkspaceTabPlugin> tabs;
  @override
  State<_TabsHarness> createState() => _TabsHarnessState();
}

class _TabsHarnessState extends State<_TabsHarness> {
  late List<WorkspaceTabPlugin> _tabs;
  @override
  void initState() {
    super.initState();
    _tabs = widget.tabs;
  }

  void setTabs(List<WorkspaceTabPlugin> tabs) => setState(() => _tabs = tabs);

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 1000,
          height: 700,
          child: IdeLayout(
            fileViewer: const SizedBox(),
            terminal: const SizedBox(),
            featureTabs: _tabs,
          ),
        ),
      ),
    );
  }
}

void main() {
  // The active-set filter lives in main.dart: it resolves the deploy's active
  // features and registers only those tabs into WorkspaceTabRegistry, which
  // the workspace page passes to IdeLayout. So "registered into the registry"
  // IS "active". These tests assert IdeLayout mounts a tab that reaches it
  // (active) and omits one that does not (inactive).

  group('feature-contributed workspace tabs', () {
    testWidgets('mounts an active feature tab in the workspace shell',
        (tester) async {
      await tester.pumpWidget(_harness(tabs: [_TabOnlyFeature()]));
      await tester.pumpAndSettle();

      // The feature's tab title is rendered in the tab strip.
      expect(find.text('Notes'), findsOneWidget);

      // Selecting the tab mounts its content widget.
      await tester.tap(find.text('Notes'));
      await tester.pumpAndSettle();
      expect(find.text('notes panel'), findsOneWidget);
    });

    testWidgets('an inactive feature tab is absent from the workspace shell',
        (tester) async {
      // No tabs passed in — the feature is inactive (main() did not register
      // it), so its tab must not appear anywhere.
      await tester.pumpWidget(_harness());
      await tester.pumpAndSettle();

      expect(find.text('Notes'), findsNothing);
      expect(find.text('notes panel'), findsNothing);
    });

    testWidgets('a tab-only feature needs no ToolPlugin', (tester) async {
      // Smoke-check the framework shape: a feature whose only component is a
      // WorkspaceTabPlugin is a valid, registrable tab (no tool handlers
      // required). This is what lets a tab-only feature ship.
      final tab = _TabOnlyFeature();
      expect(tab, isA<WorkspaceTabPlugin>());
      // It is NOT a ToolPlugin — the two are deliberately independent.
      expect(tab is ToolPlugin, isFalse);
      await tester.pumpWidget(_harness(tabs: [tab]));
      await tester.pumpAndSettle();
      expect(find.text('Notes'), findsOneWidget);
    });

    testWidgets('a feature tab badge renders and setVisible toggles on select',
        (tester) async {
      final tab = _BadgedTab('Chat', badge: const TabBadge(count: 3));
      await tester.pumpWidget(_harness(tabs: [tab]));
      await tester.pumpAndSettle();
      // The badge count renders in the strip (SkeuoTab shows the raw count).
      expect(find.text('3'), findsOneWidget);

      // Selecting the chat tab notifies it of visibility.
      await tester.tap(find.text('Chat'));
      await tester.pumpAndSettle();
      expect(tab.visibility, [true]);

      // Switching back to Terminal hides it.
      await tester.tap(find.text('Terminal'));
      await tester.pumpAndSettle();
      expect(tab.visibility, [true, false]);
    });

    testWidgets('a highlighted badge renders as @count', (tester) async {
      final tab =
          _BadgedTab('Chat', badge: const TabBadge(count: 5, highlight: true));
      await tester.pumpWidget(_harness(tabs: [tab]));
      await tester.pumpAndSettle();
      expect(find.text('@5'), findsOneWidget);
    });

    testWidgets('badge updates re-render the strip', (tester) async {
      final tab = _BadgedTab('Chat');
      await tester.pumpWidget(_harness(tabs: [tab]));
      await tester.pumpAndSettle();
      expect(find.text('7'), findsNothing);
      tab._badge.value = const TabBadge(count: 7);
      await tester.pumpAndSettle();
      expect(find.text('7'), findsOneWidget);
    });

    testWidgets('drops the badge subscription when a feature tab is removed',
        (tester) async {
      final tab = _BadgedTab('Chat', badge: const TabBadge(count: 1));
      await tester.pumpWidget(_TabsHarness(tabs: [tab]));
      await tester.pumpAndSettle();
      expect(tab._badge.hasListenersPublic, isTrue); // IdeLayout subscribed.

      final state =
          tester.state(find.byType(_TabsHarness)) as _TabsHarnessState;
      state.setTabs(const []);
      await tester.pumpAndSettle();

      // Subscription dropped — no listeners remain on the tab's badge notifier.
      expect(tab._badge.hasListenersPublic, isFalse);
    });
  });
}
