import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/auth/consent_page.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

http.Client _mockClient({
  String bannerTitle = '',
  String bannerText = '',
}) {
  return MockClient((request) async {
    if (request.url.path.contains('/api/config')) {
      return http.Response(
        jsonEncode({
          'registration_enabled': true,
          'login_banner_title': bannerTitle,
          'login_banner': bannerText,
        }),
        200,
      );
    }
    return http.Response('Not found', 404);
  });
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
    testAuthHttpClientOverride = null;
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  Widget buildConsentPage(AuthService auth) {
    return ChangeNotifierProvider.value(
      value: auth,
      child: const MaterialApp(home: ConsentPage()),
    );
  }

  group('ConsentPage', () {
    testWidgets('shows banner title and text', (tester) async {
      testAuthHttpClientOverride = _mockClient(
        bannerTitle: 'Terms of Use',
        bannerText: 'You must accept these terms.',
      );

      final auth = AuthService();
      await tester.pumpWidget(buildConsentPage(auth));
      await tester.pumpAndSettle();

      expect(find.text('Terms of Use'), findsOneWidget);
      expect(find.text('Sign in to continue'), findsOneWidget);
      expect(find.text('You must accept these terms.'), findsOneWidget);
      expect(find.text('I Accept'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
    });

    testWidgets('omits title section when no title', (tester) async {
      testAuthHttpClientOverride = _mockClient(
        bannerText: 'Accept this.',
      );

      final auth = AuthService();
      await tester.pumpWidget(buildConsentPage(auth));
      await tester.pumpAndSettle();

      expect(find.text('Accept this.'), findsOneWidget);
      expect(find.text('Sign in to continue'), findsNothing);
      expect(find.text('I Accept'), findsOneWidget);
    });

    testWidgets('I Accept calls acceptBanner', (tester) async {
      testAuthHttpClientOverride = _mockClient(
        bannerText: 'Please accept.',
      );

      final auth = AuthService();
      await tester.pumpWidget(buildConsentPage(auth));
      await tester.pumpAndSettle();

      expect(auth.bannerRequired, isTrue);

      await tester.tap(find.text('I Accept'));
      await tester.pumpAndSettle();

      expect(auth.bannerAccepted, isTrue);
      expect(auth.bannerRequired, isFalse);
    });
  });
}
