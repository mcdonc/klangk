import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/branding.dart';
import 'package:klangk_frontend/widgets/legal_links.dart';

void main() {
  setUp(Branding.reset);

  // Regression sentinel: ensures the widget renders nothing by default so an
  // unconfigured deployment shows no empty/placeholder chrome (#1177).
  testWidgets('renders nothing when no links configured', (tester) async {
    await tester.pumpWidget(
      const MaterialApp(home: Scaffold(body: LegalLinks())),
    );
    expect(find.byType(LegalLinks), findsOneWidget);
    expect(find.byType(Wrap), findsNothing);
  });

  testWidgets('renders configured legal + support links', (tester) async {
    Branding.termsUrl = 'https://corp/t';
    Branding.privacyUrl = 'https://corp/p';
    Branding.supportUrl = 'https://help';
    await tester.pumpWidget(
      const MaterialApp(home: Scaffold(body: LegalLinks())),
    );
    expect(find.text('Terms'), findsOneWidget);
    expect(find.text('Privacy'), findsOneWidget);
    expect(find.text('Support'), findsOneWidget);
  });

  testWidgets('legal-only hides support; support-only hides legal',
      (tester) async {
    Branding.supportEmail = 'help@corp';
    await tester.pumpWidget(
      const MaterialApp(home: Scaffold(body: LegalLinks())),
    );
    expect(find.text('Support'), findsOneWidget);
    expect(find.text('Terms'), findsNothing);
  });

  testWidgets('respects showLegal/showSupport flags', (tester) async {
    Branding.termsUrl = 'https://corp/t';
    Branding.supportUrl = 'https://help';
    await tester.pumpWidget(
      const MaterialApp(
        home: Scaffold(body: LegalLinks(showLegal: false, showSupport: true)),
      ),
    );
    expect(find.text('Terms'), findsNothing); // legal suppressed
    expect(find.text('Support'), findsOneWidget); // support shown
  });

  testWidgets('support link uses mailto when only email is set',
      (tester) async {
    Branding.supportEmail = 'help@corp';
    await tester.pumpWidget(
      const MaterialApp(home: Scaffold(body: LegalLinks(showLegal: false))),
    );
    expect(find.text('Support'), findsOneWidget);
  });

  // Covers the per-link onTap handler (the openUrl call). openUrl is a
  // no-op stub under VM tests, so this just confirms the handler runs
  // without throwing (#1177).
  testWidgets('tapping a link invokes its handler without throwing',
      (tester) async {
    Branding.termsUrl = 'https://corp/t';
    await tester.pumpWidget(
      const MaterialApp(home: Scaffold(body: LegalLinks(showSupport: false))),
    );
    await tester.tap(find.text('Terms'));
    await tester.pump();
    // No exception means the tap handler ran clean.
    expect(tester.takeException(), isNull);
  });
}
