import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/widgets/option_button.dart';

void main() {
  // The option-button group styling (#2502): active renders as a
  // FilledButton (amber), inactive as an OutlinedButton, with the key on
  // the Material button itself so tests can type-assert via the key —
  // the contract the pause-window and duration-selector groups rely on.
  testWidgets('active renders FilledButton, inactive OutlinedButton, by key',
      (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: Row(
          children: [
            KOptionButton(
              buttonKey: const ValueKey('opt-a'),
              label: 'Choice A',
              active: true,
              onPressed: () {},
            ),
            KOptionButton(
              buttonKey: const ValueKey('opt-b'),
              label: 'Choice B',
              active: false,
              onPressed: () {},
            ),
          ],
        ),
      ),
    ));
    expect(find.text('Choice A'), findsOneWidget);
    expect(find.text('Choice B'), findsOneWidget);
    expect(tester.widget(find.byKey(const ValueKey('opt-a'))),
        isA<FilledButton>());
    expect(tester.widget(find.byKey(const ValueKey('opt-b'))),
        isA<OutlinedButton>());
  });

  testWidgets('icon + tooltip render; onPressed fires on tap', (tester) async {
    var taps = 0;
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: KOptionButton(
          buttonKey: const ValueKey('opt-x'),
          label: 'Pause 15m',
          active: false,
          icon: Icons.pause_outlined,
          tooltip: 'Silence all consent prompts for 15 minutes',
          onPressed: () => taps++,
        ),
      ),
    ));
    expect(find.byIcon(Icons.pause_outlined), findsOneWidget);
    expect(find.byTooltip('Silence all consent prompts for 15 minutes'),
        findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('opt-x')));
    expect(taps, 1);
  });

  testWidgets('without a tooltip no Tooltip widget is added', (tester) async {
    await tester.pumpWidget(MaterialApp(
      home: Scaffold(
        body: KOptionButton(
          buttonKey: const ValueKey('opt-plain'),
          label: 'plain',
          active: false,
          onPressed: () {},
        ),
      ),
    ));
    expect(find.byType(Tooltip), findsNothing);
  });
}
