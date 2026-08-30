import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/widgets/obscure_toggle.dart';

void main() {
  group('KObscureToggle', () {
    // One FocusNode per field so the assertions identify the focused
    // field directly instead of walking the widget tree for keys.
    late FocusNode first, pw, last;

    Widget buildForm({Widget? suffix}) {
      return MaterialApp(
        home: Scaffold(
          body: Column(
            children: [
              TextField(key: const Key('first'), focusNode: first),
              TextFormField(
                key: const Key('pw'),
                focusNode: pw,
                autofocus: true,
                decoration: InputDecoration(suffixIcon: suffix),
              ),
              TextField(key: const Key('last'), focusNode: last),
            ],
          ),
        ),
      );
    }

    setUp(() {
      first = FocusNode();
      pw = FocusNode();
      last = FocusNode();
    });

    tearDown(() {
      first.dispose();
      pw.dispose();
      last.dispose();
    });

    bool focusInEye() {
      final ctx = FocusManager.instance.primaryFocus?.context;
      return ctx?.findAncestorWidgetOfExactType<IconButton>() != null;
    }

    testWidgets('shows the hidden-text eye while obscured, reveal eye after',
        (tester) async {
      var obscured = true;
      await tester.pumpWidget(MaterialApp(
        home: Scaffold(
          body: StatefulBuilder(
            builder: (_, setState) => KObscureToggle(
              obscured: obscured,
              onToggle: () => setState(() => obscured = !obscured),
            ),
          ),
        ),
      ));
      expect(find.byIcon(Icons.visibility_off), findsOneWidget);

      await tester.tap(find.byType(IconButton));
      await tester.pump();
      expect(find.byIcon(Icons.visibility), findsOneWidget);
    });

    testWidgets('Tab out of the password field skips the toggle (#2893)',
        (tester) async {
      await tester.pumpWidget(buildForm(
        suffix: KObscureToggle(obscured: true, onToggle: () {}),
      ));
      await tester.pump();
      expect(pw.hasFocus, isTrue);

      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();

      // Focus jumps straight to the next input — not the eye IconButton.
      expect(last.hasFocus, isTrue);
      expect(focusInEye(), isFalse);
    });

    testWidgets('control: a plain suffix IconButton IS a tab stop',
        (tester) async {
      // Guards the skip test above: proves this harness detects a
      // focusable suffix button, so the skip assertion is not vacuous.
      await tester.pumpWidget(buildForm(
        suffix: IconButton(
          icon: const Icon(Icons.visibility),
          onPressed: () {},
        ),
      ));
      await tester.pump();
      expect(pw.hasFocus, isTrue);

      await tester.sendKeyEvent(LogicalKeyboardKey.tab);
      await tester.pump();

      expect(last.hasFocus, isFalse);
      expect(focusInEye(), isTrue);
    });
  });
}
