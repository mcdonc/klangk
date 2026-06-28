import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/chat/chat_input_bar.dart';

void main() {
  group('ChatInputBar', () {
    late List<String> sentMessages;

    setUp(() {
      sentMessages = [];
    });

    Widget buildBar({
      bool agentThinking = false,
      VoidCallback? onAbort,
      List<Map<String, dynamic>> members = const [],
      GlobalKey<ChatInputBarState>? barKey,
    }) {
      return MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: 800,
            height: 100,
            child: ChatInputBar(
              key: barKey,
              onSendText: (text) => sentMessages.add(text),
              agentThinking: agentThinking,
              onAbort: onAbort,
              members: members,
            ),
          ),
        ),
      );
    }

    testWidgets('sends message on Enter', (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.enterText(find.byType(TextField), 'hello');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      expect(sentMessages, ['hello']);
      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, isEmpty);
    });

    testWidgets('sends message on send button tap', (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.enterText(find.byType(TextField), 'button msg');
      await tester.tap(find.byIcon(Icons.send));
      await tester.pump();

      expect(sentMessages, ['button msg']);
    });

    testWidgets('does not send empty message', (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      expect(sentMessages, isEmpty);
    });

    testWidgets('does not send whitespace-only message', (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.enterText(find.byType(TextField), '   ');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      expect(sentMessages, isEmpty);
    });

    testWidgets('shows stop button when agent is thinking', (tester) async {
      var aborted = false;
      await tester.pumpWidget(buildBar(
        agentThinking: true,
        onAbort: () => aborted = true,
      ));

      expect(find.byIcon(Icons.stop_circle_outlined), findsOneWidget);

      await tester.tap(find.byIcon(Icons.stop_circle_outlined));
      await tester.pump();

      expect(aborted, isTrue);
    });

    testWidgets('no stop button when agent is not thinking', (tester) async {
      await tester.pumpWidget(buildBar());
      expect(find.byIcon(Icons.stop_circle_outlined), findsNothing);
    });

    testWidgets('Ctrl+A moves cursor to beginning of line', (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.enterText(find.byType(TextField), 'hello world');
      await tester.pump();

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.selection.baseOffset, 0);
    });

    testWidgets('Ctrl+E moves cursor to end of line', (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.enterText(find.byType(TextField), 'hello world');
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      field.controller!.selection = const TextSelection.collapsed(offset: 0);
      await tester.pump();

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyE);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pump();

      expect(field.controller!.selection.baseOffset, 11);
    });

    testWidgets('Ctrl+K kills to end of line', (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.enterText(find.byType(TextField), 'hello world');
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      field.controller!.selection = const TextSelection.collapsed(offset: 5);
      await tester.pump();

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyK);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pump();

      expect(field.controller!.text, 'hello');
    });

    testWidgets('Ctrl+K at newline joins lines', (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.enterText(find.byType(TextField), 'line1\nline2');
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      field.controller!.selection = const TextSelection.collapsed(offset: 5);
      await tester.pump();

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyK);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pump();

      expect(field.controller!.text, 'line1line2');
    });

    testWidgets('Shift+Ctrl+A selects all text', (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.enterText(find.byType(TextField), 'hello world');
      await tester.pump();

      await tester.sendKeyDownEvent(LogicalKeyboardKey.controlLeft);
      await tester.sendKeyDownEvent(LogicalKeyboardKey.shiftLeft);
      await tester.sendKeyEvent(LogicalKeyboardKey.keyA);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.shiftLeft);
      await tester.sendKeyUpEvent(LogicalKeyboardKey.controlLeft);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.selection.baseOffset, 0);
      expect(field.controller!.selection.extentOffset, 11);
    });

    testWidgets('Up arrow recalls previous sent message', (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.enterText(find.byType(TextField), 'first');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      await tester.enterText(find.byType(TextField), 'second');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, 'second');

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();
      expect(field.controller!.text, 'first');
    });

    testWidgets('Down arrow restores draft after history recall',
        (tester) async {
      await tester.pumpWidget(buildBar());

      await tester.enterText(find.byType(TextField), 'sent msg');
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      await tester.enterText(find.byType(TextField), 'my draft');
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowUp);
      await tester.pump();
      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, 'sent msg');

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      expect(field.controller!.text, 'my draft');
    });

    testWidgets('multiline text field grows', (tester) async {
      await tester.pumpWidget(buildBar());

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.maxLines, isNull);
    });

    testWidgets('requestFocus focuses input', (tester) async {
      final barKey = GlobalKey<ChatInputBarState>();
      await tester.pumpWidget(buildBar(barKey: barKey));

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.focusNode!.hasFocus, isFalse);

      barKey.currentState!.requestFocus();
      await tester.pump();

      expect(field.focusNode!.hasFocus, isTrue);
    });

    testWidgets('@autocomplete shows members on @ input', (tester) async {
      await tester.pumpWidget(buildBar(members: [
        {'id': 'u1', 'email': 'alice@test.com', 'handle': ''},
        {'id': 'u2', 'email': 'bob@test.com', 'handle': ''},
      ]));

      await tester.enterText(find.byType(TextField), '@');
      await tester.pump();

      expect(find.text('alice@test.com'), findsWidgets);
      expect(find.text('bob@test.com'), findsWidgets);
    });

    testWidgets('@autocomplete filters by query', (tester) async {
      await tester.pumpWidget(buildBar(members: [
        {'id': 'u1', 'email': 'alice@test.com', 'handle': ''},
        {'id': 'u2', 'email': 'bob@test.com', 'handle': ''},
      ]));

      await tester.enterText(find.byType(TextField), '@ali');
      await tester.pump();

      expect(find.text('alice@test.com'), findsWidgets);
      expect(find.text('bob@test.com'), findsNothing);
    });

    testWidgets('Tab key accepts autocomplete suggestion', (tester) async {
      await tester.pumpWidget(buildBar(members: [
        {'id': 'u1', 'email': 'alice@test.com', 'handle': ''},
      ]));

      await tester.enterText(find.byType(TextField), '@al');
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '@alice@test.com ');
    });

    testWidgets('Escape dismisses autocomplete', (tester) async {
      await tester.pumpWidget(buildBar(members: [
        {'id': 'u1', 'email': 'alice@test.com', 'handle': ''},
      ]));

      await tester.enterText(find.byType(TextField), '@al');
      await tester.pump();
      expect(find.text('alice@test.com'), findsWidgets);

      await tester.sendKeyEvent(LogicalKeyboardKey.escape);
      await tester.pump();

      expect(find.text('alice@test.com'), findsNothing);
    });

    testWidgets('Enter accepts autocomplete when overlay is visible',
        (tester) async {
      await tester.pumpWidget(buildBar(members: [
        {'id': 'u1', 'email': 'alice@test.com', 'handle': ''},
      ]));

      await tester.enterText(find.byType(TextField), '@al');
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '@alice@test.com ');
      // Should not have sent a message
      expect(sentMessages, isEmpty);
    });

    testWidgets('@autocomplete hides when @ followed by space', (tester) async {
      await tester.pumpWidget(buildBar(members: [
        {'id': 'u1', 'email': 'alice@test.com', 'handle': ''},
      ]));

      await tester.enterText(find.byType(TextField), '@ ');
      await tester.pump();

      expect(find.text('alice@test.com'), findsNothing);
    });

    testWidgets('Arrow keys navigate autocomplete', (tester) async {
      await tester.pumpWidget(buildBar(members: [
        {'id': 'u1', 'email': 'alice@test.com', 'handle': ''},
        {'id': 'u2', 'email': 'bob@test.com', 'handle': ''},
      ]));

      await tester.enterText(find.byType(TextField), '@');
      await tester.pump();

      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '@bob@test.com ');
    });

    testWidgets('prefers handle over email in autocomplete', (tester) async {
      await tester.pumpWidget(buildBar(members: [
        {'id': 'u1', 'email': 'alice@test.com', 'handle': 'alice'},
      ]));

      await tester.enterText(find.byType(TextField), '@al');
      await tester.pump();

      expect(find.text('alice'), findsWidgets);

      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();

      final field = tester.widget<TextField>(find.byType(TextField));
      expect(field.controller!.text, '@alice ');
    });
  });
}
