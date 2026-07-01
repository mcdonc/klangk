import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/branding.dart';
import 'package:klangk_frontend/widgets/klangk_logo.dart';
import 'package:klangk_frontend/theme/colors.dart';

void main() {
  Widget buildLogo({double height = 200}) {
    return Directionality(
      textDirection: TextDirection.ltr,
      child: UnconstrainedBox(child: KlangkLogo(height: height)),
    );
  }

  // Branding.logoUrl is process-global; isolate every test so an override
  // set in one can't leak into another. See #1152.
  setUp(Branding.reset);
  tearDown(Branding.reset);

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

  group('KlangkLogo logo override', () {
    testWidgets('renders the default widget when no override', (tester) async {
      await tester.pumpWidget(buildLogo());
      // Default branch: robot icon + wordmark, no Image widget.
      expect(find.byIcon(Icons.smart_toy_outlined), findsOneWidget);
      expect(find.byType(Image), findsNothing);
    });

    testWidgets('renders an Image when Branding.logoUrl is set',
        (tester) async {
      Branding.logoUrl = 'https://no.such.invalid/logo.png';
      // The override branch builds an Image (with a NetworkImage). We assert
      // the branch was taken, not that the image loaded, and stop before the
      // (offline) load can fail, so the default content is not rendered.
      await tester.pumpWidget(buildLogo());
      expect(find.byType(Image), findsOneWidget);
    });

    testWidgets('falls back to default when the image fails to load',
        (tester) async {
      // `.invalid` is reserved (RFC 2606) and never resolves, so the
      // NetworkImage errors fast and offline. Mirror the image_renderer
      // error-test convention: settle, drain the expected exception, settle
      // again, then assert the errorBuilder fallback rendered the default.
      Branding.logoUrl = 'https://no.such.invalid/logo.png';
      await tester.pumpWidget(buildLogo());
      await tester.pumpAndSettle();
      tester.takeException();
      await tester.pumpAndSettle();
      expect(find.byIcon(Icons.smart_toy_outlined), findsOneWidget);
    });
  });
}
