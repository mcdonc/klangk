/// Tests for the classification marking banner (#2768).
///
/// The banner renders the effective marking (workspace override, else the
/// deploy default) as a persistent top/bottom strip, color-coded by
/// marking convention — and renders NOTHING (no reserved space) when no
/// marking is configured.

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/marking_banner.dart';

void main() {
  group('markingColor', () {
    test('maps the common US marking words', () {
      expect(markingColor('TOP SECRET'), const Color(0xFFE0A800));
      // Case-insensitive; TOP SECRET wins over SECRET.
      expect(markingColor('top secret // sci'), const Color(0xFFE0A800));
      expect(markingColor('SECRET'), const Color(0xFFC01818));
      expect(markingColor('CONFIDENTIAL'), const Color(0xFF005EB8));
      expect(markingColor('CUI//SP-ABC'), const Color(0xFF0076CE));
      expect(markingColor('unclassified'), const Color(0xFF007A33));
    });

    test('falls back to neutral amber for free-text labels', () {
      expect(markingColor('Internal use only'), const Color(0xFF8A6D00));
    });
  });

  group('effectiveMarking', () {
    test('workspace override wins', () {
      expect(effectiveMarking('SECRET', 'CUI'), 'SECRET');
    });

    test('falls back to the deploy default', () {
      expect(effectiveMarking(null, 'CUI'), 'CUI');
      expect(effectiveMarking('', ' CUI '), 'CUI');
    });

    test('whitespace-only values count as unset', () {
      expect(effectiveMarking('   ', '  '), '');
    });
  });

  group('MarkingBanner widget', () {
    Widget _wrap(Widget child) => MaterialApp(home: Scaffold(body: child));

    testWidgets('renders the marking centered on a colored strip',
        (tester) async {
      await tester.pumpWidget(_wrap(MarkingBanner(text: 'SECRET')));
      final text = tester.widget<Text>(find.text('SECRET'));
      expect(text.textAlign, TextAlign.center);
      expect(text.style?.color, Colors.white);
      expect(text.style?.fontWeight, FontWeight.w700);
      final material = tester.widget<Material>(
        find
            .ancestor(
              of: find.text('SECRET'),
              matching: find.byType(Material),
            )
            .first,
      );
      expect(material.color, const Color(0xFFC01818));
    });

    testWidgets('renders nothing when no marking is configured',
        (tester) async {
      await tester.pumpWidget(_wrap(MarkingBanner(text: '')));
      expect(find.byType(MarkingBanner), findsOneWidget);
      // Zero-size: no banner strip, no reserved screen space (#2768).
      final size = tester.getSize(find.byType(MarkingBanner));
      expect(size, Size.zero);
      expect(find.text('SECRET'), findsNothing);
    });

    testWidgets('trims surrounding whitespace before rendering',
        (tester) async {
      await tester.pumpWidget(_wrap(MarkingBanner(text: '  CUI  ')));
      expect(find.text('CUI'), findsOneWidget);
    });
  });
}
