import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/widgets/klangk_logo.dart';
import 'package:klangk_frontend/theme/colors.dart';

void main() {
  Widget buildLogo({double height = 200}) {
    return Directionality(
      textDirection: TextDirection.ltr,
      child: UnconstrainedBox(child: KlangkLogo(height: height)),
    );
  }

  group('KlangkLogo', () {
    test('default height is 40', () {
      const logo = KlangkLogo();
      expect(logo.height, 40);
    });

    test('custom height is preserved', () {
      const logo = KlangkLogo(height: 120);
      expect(logo.height, 120);
    });

    testWidgets('renders robot icon', (tester) async {
      await tester.pumpWidget(buildLogo());
      expect(find.byIcon(Icons.smart_toy_outlined), findsOneWidget);
    });

    testWidgets('renders klangk text', (tester) async {
      await tester.pumpWidget(buildLogo());
      expect(find.text('klangk'), findsOneWidget);
    });

    testWidgets('icon uses accent cyan color', (tester) async {
      await tester.pumpWidget(buildLogo());
      final icon = tester.widget<Icon>(find.byIcon(Icons.smart_toy_outlined));
      expect(icon.color, KColors.textPrimary);
    });

    testWidgets('text uses primary color and thin weight', (tester) async {
      await tester.pumpWidget(buildLogo());
      final text = tester.widget<Text>(find.text('klangk'));
      expect(text.style?.color, KColors.textPrimary);
      expect(text.style?.fontWeight, FontWeight.w400);
    });

    testWidgets('icon size scales with height', (tester) async {
      await tester.pumpWidget(buildLogo(height: 100));
      final icon = tester.widget<Icon>(find.byIcon(Icons.smart_toy_outlined));
      expect(icon.size, 50); // height * 0.5
    });

    testWidgets('has gradient decoration', (tester) async {
      await tester.pumpWidget(buildLogo());
      final container = tester.widget<Container>(
        find.descendant(
          of: find.byType(KlangkLogo),
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
          of: find.byType(KlangkLogo),
          matching: find.byType(Container),
        ),
      );
      final decoration = container.decoration as BoxDecoration;
      expect(decoration.borderRadius, isNotNull);
    });

    testWidgets('has border', (tester) async {
      await tester.pumpWidget(buildLogo());
      final container = tester.widget<Container>(
        find.descendant(
          of: find.byType(KlangkLogo),
          matching: find.byType(Container),
        ),
      );
      final decoration = container.decoration as BoxDecoration;
      expect(decoration.border, isNotNull);
    });

    testWidgets('uses FittedBox to prevent overflow', (tester) async {
      await tester.pumpWidget(buildLogo());
      expect(
        find.descendant(
          of: find.byType(KlangkLogo),
          matching: find.byType(FittedBox),
        ),
        findsOneWidget,
      );
    });

    testWidgets('widget is square', (tester) async {
      await tester.pumpWidget(buildLogo(height: 150));
      final container = tester.widget<Container>(
        find.descendant(
          of: find.byType(KlangkLogo),
          matching: find.byType(Container),
        ),
      );
      expect(container.constraints?.maxWidth, 150);
      expect(container.constraints?.maxHeight, 150);
    });
  });
}
