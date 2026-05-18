import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/widgets/bark_logo.dart';

void main() {
  Widget buildLogo({double height = 200}) {
    return Directionality(
      textDirection: TextDirection.ltr,
      child: UnconstrainedBox(child: BarkLogo(height: height)),
    );
  }

  group('BarkLogo', () {
    test('default height is 40', () {
      const logo = BarkLogo();
      expect(logo.height, 40);
    });

    test('custom height is preserved', () {
      const logo = BarkLogo(height: 120);
      expect(logo.height, 120);
    });

    testWidgets('renders paw icon', (tester) async {
      await tester.pumpWidget(buildLogo());
      expect(find.byIcon(Icons.pets), findsOneWidget);
    });

    testWidgets('renders Bark text', (tester) async {
      await tester.pumpWidget(buildLogo());
      expect(find.text('Bark'), findsOneWidget);
    });

    testWidgets('icon is white', (tester) async {
      await tester.pumpWidget(buildLogo());
      final icon = tester.widget<Icon>(find.byIcon(Icons.pets));
      expect(icon.color, Colors.white);
    });

    testWidgets('text is white and bold', (tester) async {
      await tester.pumpWidget(buildLogo());
      final text = tester.widget<Text>(find.text('Bark'));
      expect(text.style?.color, Colors.white);
      expect(text.style?.fontWeight, FontWeight.w800);
    });

    testWidgets('icon size scales with height', (tester) async {
      await tester.pumpWidget(buildLogo(height: 100));
      final icon = tester.widget<Icon>(find.byIcon(Icons.pets));
      expect(icon.size, 50); // height * 0.5
    });

    testWidgets('has gradient decoration', (tester) async {
      await tester.pumpWidget(buildLogo());
      final container = tester.widget<Container>(
        find.descendant(
          of: find.byType(BarkLogo),
          matching: find.byType(Container),
        ),
      );
      final decoration = container.decoration as BoxDecoration;
      expect(decoration.gradient, isA<LinearGradient>());
    });

    testWidgets('has rounded corners', (tester) async {
      await tester.pumpWidget(buildLogo(height: 100));
      final container = tester.widget<Container>(
        find.descendant(
          of: find.byType(BarkLogo),
          matching: find.byType(Container),
        ),
      );
      final decoration = container.decoration as BoxDecoration;
      expect(decoration.borderRadius, isNotNull);
    });

    testWidgets('has box shadow', (tester) async {
      await tester.pumpWidget(buildLogo());
      final container = tester.widget<Container>(
        find.descendant(
          of: find.byType(BarkLogo),
          matching: find.byType(Container),
        ),
      );
      final decoration = container.decoration as BoxDecoration;
      expect(decoration.boxShadow, isNotNull);
      expect(decoration.boxShadow!.length, 1);
    });

    testWidgets('uses FittedBox to prevent overflow', (tester) async {
      await tester.pumpWidget(buildLogo());
      expect(
        find.descendant(
          of: find.byType(BarkLogo),
          matching: find.byType(FittedBox),
        ),
        findsOneWidget,
      );
    });

    testWidgets('widget is square', (tester) async {
      await tester.pumpWidget(buildLogo(height: 150));
      final container = tester.widget<Container>(
        find.descendant(
          of: find.byType(BarkLogo),
          matching: find.byType(Container),
        ),
      );
      expect(container.constraints?.maxWidth, 150);
      expect(container.constraints?.maxHeight, 150);
    });
  });
}
