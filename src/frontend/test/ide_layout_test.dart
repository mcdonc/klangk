import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/layout/ide_layout.dart';

void main() {
  Widget buildLayout({
    Widget? fileViewer,
    Widget? terminal,
    Widget? chat,
    Widget? debug,
  }) {
    return MaterialApp(
      home: Scaffold(
        body: SizedBox(
          width: 1280,
          height: 720,
          child: IdeLayout(
            fileViewer: fileViewer ?? const Text('Files'),
            terminal: terminal ?? const Text('Terminal'),
            chat: chat ?? const Text('Chat'),
            debug: debug ?? const Text('Debug'),
          ),
        ),
      ),
    );
  }

  group('IdeLayout', () {
    testWidgets('renders all child widgets', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.text('Terminal'), findsWidgets);
      expect(find.text('Files'), findsWidgets);
    });

    testWidgets('has Terminal and Files tabs', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.text('Terminal'), findsWidgets);
      expect(find.text('Files'), findsWidgets);
    });

    testWidgets('terminal tab content is visible by default', (tester) async {
      await tester.pumpWidget(buildLayout(
        terminal: const Text('TERMINAL_CONTENT'),
        fileViewer: const Text('FILES_CONTENT'),
      ));
      expect(find.text('TERMINAL_CONTENT'), findsOneWidget);
    });

    testWidgets('files tab content is visible after switch', (tester) async {
      await tester.pumpWidget(buildLayout(
        terminal: const Text('TERMINAL_CONTENT'),
        fileViewer: const Text('FILES_CONTENT'),
      ));

      await tester.tap(find.text('Files'));
      await tester.pumpAndSettle();

      expect(find.text('FILES_CONTENT'), findsOneWidget);
    });

    testWidgets('tab switching works', (tester) async {
      await tester.pumpWidget(buildLayout());

      await tester.tap(find.text('Files'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Terminal'));
      await tester.pumpAndSettle();

      expect(find.byType(IdeLayout), findsOneWidget);
    });

    testWidgets('uses IndexedStack for tab content', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.byType(IndexedStack), findsOneWidget);
    });

    testWidgets('debug divider has resize cursor', (tester) async {
      await tester.pumpWidget(buildLayout());

      final mouseRegions = tester.widgetList<MouseRegion>(
        find.byType(MouseRegion),
      );

      final resizeRow =
          mouseRegions.where((m) => m.cursor == SystemMouseCursors.resizeRow);
      expect(resizeRow.length, 1);
    });

    testWidgets('debug divider can be dragged', (tester) async {
      await tester.pumpWidget(buildLayout());
      await tester.pumpAndSettle();

      final resizeRow = find.byWidgetPredicate(
        (w) => w is MouseRegion && w.cursor == SystemMouseCursors.resizeRow,
      );
      expect(resizeRow, findsOneWidget);

      await tester.drag(resizeRow, const Offset(0, -50));
      await tester.pumpAndSettle();

      expect(find.byType(IdeLayout), findsOneWidget);
    });

    testWidgets('debug divider double tap toggles debug pane', (tester) async {
      await tester.pumpWidget(buildLayout(
        debug: const Text('DEBUG_OUTPUT'),
      ));
      await tester.pumpAndSettle();

      final gestureDetector = find.byWidgetPredicate(
        (w) =>
            w is GestureDetector &&
            w.onDoubleTap != null &&
            w.onVerticalDragUpdate != null,
      );

      // Double tap to expand from 0 to 200
      await tester.tap(gestureDetector);
      await tester.pump(const Duration(milliseconds: 50));
      await tester.tap(gestureDetector);
      await tester.pumpAndSettle();

      expect(find.text('DEBUG_OUTPUT'), findsOneWidget);

      // Double tap again to collapse back to 0
      await tester.tap(gestureDetector);
      await tester.pump(const Duration(milliseconds: 50));
      await tester.tap(gestureDetector);
      await tester.pumpAndSettle();

      expect(find.byType(IdeLayout), findsOneWidget);
    });

    testWidgets('no debug pane when debug is null', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 1280,
              height: 720,
              child: IdeLayout(
                fileViewer: const Text('Files'),
                terminal: const Text('Terminal'),
              ),
            ),
          ),
        ),
      );

      final mouseRegions = tester.widgetList<MouseRegion>(
        find.byType(MouseRegion),
      );
      final resizeRow =
          mouseRegions.where((m) => m.cursor == SystemMouseCursors.resizeRow);
      expect(resizeRow.length, 0);
    });

    testWidgets('has Chat tab', (tester) async {
      await tester.pumpWidget(buildLayout());
      expect(find.text('Chat'), findsWidgets);
    });

    testWidgets('chat tab content is visible after switch', (tester) async {
      await tester.pumpWidget(buildLayout(
        terminal: const Text('TERMINAL_CONTENT'),
        fileViewer: const Text('FILES_CONTENT'),
        chat: const Text('CHAT_CONTENT'),
      ));

      await tester.tap(find.text('Chat'));
      await tester.pumpAndSettle();

      expect(find.text('CHAT_CONTENT'), findsOneWidget);
    });

    testWidgets('no chat tab when chat is null', (tester) async {
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SizedBox(
              width: 1280,
              height: 720,
              child: IdeLayout(
                fileViewer: const Text('Files'),
                terminal: const Text('Terminal'),
              ),
            ),
          ),
        ),
      );

      // Chat tab label should NOT be present (only Terminal and Files)
      final chatTabs = find.text('Chat');
      expect(chatTabs, findsNothing);
    });

    testWidgets('selecting same tab does not rebuild', (tester) async {
      await tester.pumpWidget(buildLayout());
      // Terminal tab label appears in both the tab bar and content;
      // tap only the one inside the GestureDetector (the tab).
      final terminalTab = find.descendant(
        of: find.byType(GestureDetector),
        matching: find.text('Terminal'),
      );
      await tester.tap(terminalTab.first);
      await tester.pumpAndSettle();
      expect(find.byType(IdeLayout), findsOneWidget);
    });
  });
}
