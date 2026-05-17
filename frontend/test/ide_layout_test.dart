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
  });
}
