import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/widgets/bark_logo.dart';

void main() {
  group('BarkLogo', () {
    testWidgets('renders with paw icon and Bark text', (tester) async {
      await tester.pumpWidget(
        const Directionality(
          textDirection: TextDirection.ltr,
          child: UnconstrainedBox(child: BarkLogo(height: 200)),
        ),
      );

      expect(find.byType(BarkLogo), findsOneWidget);
      expect(find.text('Bark'), findsOneWidget);
      expect(find.byIcon(Icons.pets), findsOneWidget);
    });

    test('default height is 40', () {
      const logo = BarkLogo();
      expect(logo.height, 40);
    });

    test('custom height is preserved', () {
      const logo = BarkLogo(height: 120);
      expect(logo.height, 120);
    });
  });
}
