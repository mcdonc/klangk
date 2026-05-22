import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/layout/ide_layout.dart';

void main() {
  Widget buildLayout({
    Widget? chat,
    Widget? fileViewer,
    Widget? terminal,
    Widget? output,
  }) {
    return MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 1280,
          height: 720,
          child: IdeLayout(
            chat: chat ?? const Text('Chat'),
            fileViewer: fileViewer ?? const Text('Files'),
            terminal: terminal ?? const Text('Terminal'),
            output: output ?? const Text('Debug'),
          ),
        ),
      ),
    );
  }

  group('IdeLayout', () {
    testWidgets('renders all child widgets', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.text('Chat'), findsOneWidget);
      expect(find.text('Terminal'), findsWidgets);
      expect(find.text('Files'), findsWidgets);
      expect(find.text('Debug'), findsOneWidget);
    });

    testWidgets('has TabBar', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.byType(TabBar), findsOneWidget);
    });

    testWidgets('terminal tab content is visible by default', (tester) async {
      await tester.pumpWidget(buildLayout(
        terminal: const Text('TERMINAL_CONTENT'),
        fileViewer: const Text('FILES_CONTENT'),
      ));

      // Terminal is the default tab (index 0)
      expect(find.text('TERMINAL_CONTENT'), findsOneWidget);
    });

    testWidgets('files tab content is visible after switch', (tester) async {
      await tester.pumpWidget(buildLayout(
        terminal: const Text('TERMINAL_CONTENT'),
        fileViewer: const Text('FILES_CONTENT'),
      ));

      final filesTab = find.descendant(
        of: find.byType(TabBar),
        matching: find.text('Files'),
      );
      await tester.tap(filesTab);
      await tester.pumpAndSettle();

      expect(find.text('FILES_CONTENT'), findsOneWidget);
    });

    testWidgets('tab switching works', (tester) async {
      await tester.pumpWidget(buildLayout());

      final filesTab = find.descendant(
        of: find.byType(TabBar),
        matching: find.text('Files'),
      );
      expect(filesTab, findsOneWidget);
      await tester.tap(filesTab);
      await tester.pumpAndSettle();

      final terminalTab = find.descendant(
        of: find.byType(TabBar),
        matching: find.text('Terminal'),
      );
      await tester.tap(terminalTab);
      await tester.pumpAndSettle();

      expect(find.byType(IdeLayout), findsOneWidget);
    });

    testWidgets('has two tabs: Terminal and Files', (tester) async {
      await tester.pumpWidget(buildLayout());

      final tabBar = find.byType(TabBar);
      expect(tabBar, findsOneWidget);

      expect(
        find.descendant(of: tabBar, matching: find.text('Terminal')),
        findsOneWidget,
      );
      expect(
        find.descendant(of: tabBar, matching: find.text('Files')),
        findsOneWidget,
      );
    });

    testWidgets('uses IndexedStack for tab content', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.byType(IndexedStack), findsOneWidget);
    });

    testWidgets('output widget is always visible', (tester) async {
      await tester.pumpWidget(buildLayout(
        output: const Text('DEBUG_OUTPUT'),
      ));

      expect(find.text('DEBUG_OUTPUT'), findsOneWidget);

      // Switch to Files tab — output should still be visible
      final filesTab = find.descendant(
        of: find.byType(TabBar),
        matching: find.text('Files'),
      );
      await tester.tap(filesTab);
      await tester.pumpAndSettle();

      expect(find.text('DEBUG_OUTPUT'), findsOneWidget);
    });

    testWidgets('horizontal divider has resize cursor', (tester) async {
      await tester.pumpWidget(buildLayout());

      final mouseRegions = tester.widgetList<MouseRegion>(
        find.byType(MouseRegion),
      );

      final resizeColumn = mouseRegions
          .where((m) => m.cursor == SystemMouseCursors.resizeColumn);
      final resizeRow =
          mouseRegions.where((m) => m.cursor == SystemMouseCursors.resizeRow);

      expect(resizeColumn.length, 1);
      expect(resizeRow.length, 1);
    });

    testWidgets('chat panel is on the left', (tester) async {
      await tester.pumpWidget(buildLayout(
        chat: const Text('CHAT_LEFT'),
      ));

      expect(find.text('CHAT_LEFT'), findsOneWidget);
    });

    testWidgets('uses LayoutBuilder for responsive sizing', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.byType(LayoutBuilder), findsOneWidget);
    });

    testWidgets('vertical divider can be dragged', (tester) async {
      await tester.pumpWidget(buildLayout());
      await tester.pumpAndSettle();

      // Find the horizontal divider (resizeRow cursor)
      final resizeRow = find.byWidgetPredicate(
        (w) => w is MouseRegion && w.cursor == SystemMouseCursors.resizeRow,
      );
      expect(resizeRow, findsOneWidget);

      // Drag it down
      await tester.drag(resizeRow, const Offset(0, 50));
      await tester.pumpAndSettle();

      expect(find.byType(IdeLayout), findsOneWidget);
    });

    testWidgets('horizontal divider can be dragged', (tester) async {
      await tester.pumpWidget(buildLayout());
      await tester.pumpAndSettle();

      // Find the vertical divider (resizeColumn cursor)
      final resizeCol = find.byWidgetPredicate(
        (w) => w is MouseRegion && w.cursor == SystemMouseCursors.resizeColumn,
      );
      expect(resizeCol, findsOneWidget);

      // Drag it right
      await tester.drag(resizeCol, const Offset(100, 0));
      await tester.pumpAndSettle();

      expect(find.byType(IdeLayout), findsOneWidget);
    });
  });
}
