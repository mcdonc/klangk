import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/branding.dart';
import 'package:klangk_frontend/widgets/app_bar_actions.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Build a JWT whose body decodes to a permissions shape AuthService
/// recognises. Only the body matters here (the signature is not verified by
/// the client). Mirrors the helper in admin_invitations_page_test.dart.
String _adminToken() {
  final header = base64Url.encode(utf8.encode(jsonEncode({'alg': 'none'})));
  final body = base64Url.encode(utf8.encode(jsonEncode({
    'email': 'admin@example.com',
    'permissions': {
      '/admin': ['*'],
    },
  })));
  return '$header.$body.fakesig';
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({'klangk_jwt': _adminToken()});
  });
  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
    Branding.reset();
  });

  Widget buildWith(Widget child) {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
      ],
      child: MaterialApp(home: Scaffold(body: child)),
    );
  }

  group('AppBarActions support link (#1177)', () {
    testWidgets('shows help icon when support URL configured', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({'support_url': 'https://help.example.com'}),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/my-permissions')) {
          return http.Response(
            jsonEncode({
              'user_id': 'u1',
              'email': 'admin@example.com',
              'permissions': {'/admin': ['*']},
              'groups': [],
            }),
            200,
          );
        }
        return http.Response('', 404);
      });
      await tester.pumpWidget(buildWith(const AppBarActions()));
      await tester.pumpAndSettle();
      // The help icon is present when support URL is configured (sourced
      // from /config -> Branding -> widget).
      final helpIcon = find.byIcon(Icons.help_outline);
      expect(helpIcon, findsOneWidget);
      // Tap it to exercise the openUrl handler (a no-op stub under VM).
      await tester.tap(helpIcon);
      await tester.pump();
      expect(tester.takeException(), isNull);
    });

    testWidgets('help icon renders when only email configured',
        (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(
            jsonEncode({'support_email': 'help@corp.example.com'}),
            200,
          );
        }
        if (request.url.path.contains('/api/v1/my-permissions')) {
          return http.Response(
            jsonEncode({
              'user_id': 'u1',
              'email': 'admin@example.com',
              'permissions': {'/admin': ['*']},
              'groups': [],
            }),
            200,
          );
        }
        return http.Response('', 404);
      });
      await tester.pumpWidget(buildWith(const AppBarActions()));
      await tester.pumpAndSettle();
      // Help icon present (email-only support also shows it).
      expect(find.byIcon(Icons.help_outline), findsOneWidget);
    });

    testWidgets('hides help icon when no support configured', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path.contains('/api/v1/config')) {
          return http.Response(jsonEncode({}), 200);
        }
        if (request.url.path.contains('/api/v1/my-permissions')) {
          return http.Response(
            jsonEncode({
              'user_id': 'u1',
              'email': 'admin@example.com',
              'permissions': {},
              'groups': [],
            }),
            200,
          );
        }
        return http.Response('', 404);
      });
      await tester.pumpWidget(buildWith(const AppBarActions()));
      await tester.pumpAndSettle();
      // No help icon at all when Branding.supportHref is empty.
      expect(find.byIcon(Icons.help_outline), findsNothing);
    });
  });
}
