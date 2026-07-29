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
  });
}
