import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/debug/system_info_tab.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

http.Client _mockClient({int versionStatus = 200}) {
  return MockClient((request) async {
    if (request.url.path == '/api/v1/version') {
      if (versionStatus != 200) {
        return http.Response('', versionStatus);
      }
      return http.Response(
        jsonEncode({
          'version': '1.2.3',
          'commit': 'abc1234',
          'built_at': '2026-01-01T00:00:00Z',
          'plugins': [
            {
              'name': 'celebrate',
              'version': '0.1.0',
              'description': 'Confetti',
            },
          ],
        }),
        200,
      );
    }
    if (request.url.path.contains('/api/config')) {
      return http.Response(
        jsonEncode({'login_banner_title': '', 'login_banner': ''}),
        200,
      );
    }
    if (request.url.path.contains('/api/my-permissions')) {
      return http.Response(
        jsonEncode({
          'user_id': 'test',
          'email': 'test@example.com',
          'permissions': <String, dynamic>{},
          'groups': <dynamic>[],
        }),
        200,
      );
    }
    return http.Response('{}', 200);
  });
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    testAuthHttpClientOverride = null;
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  testWidgets('shows version info and plugins', (tester) async {
    testAuthHttpClientOverride = _mockClient();
    SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
    final auth = AuthService();

    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: SystemInfoTab(auth: auth)),
      ),
    );
    // Pump a few frames to let async init + fetch complete.
    await tester.pump();
    await tester.pump();
    await tester.pump();

    expect(find.text('1.2.3'), findsOneWidget);
    expect(find.text('abc1234'), findsOneWidget);
    expect(find.text('2026-01-01T00:00:00Z'), findsOneWidget);
    expect(find.textContaining('celebrate'), findsOneWidget);
    expect(find.textContaining('Confetti'), findsOneWidget);
  });

  testWidgets('shows error on failure', (tester) async {
    testAuthHttpClientOverride = _mockClient(versionStatus: 500);
    SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
    final auth = AuthService();

    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: SystemInfoTab(auth: auth)),
      ),
    );
    await tester.pump();
    await tester.pump();
    await tester.pump();

    expect(find.text('HTTP 500'), findsOneWidget);
  });

  testWidgets('shows connection error on exception', (tester) async {
    testAuthHttpClientOverride = MockClient((request) async {
      if (request.url.path == '/api/v1/version') {
        throw Exception('connection refused');
      }
      if (request.url.path.contains('/api/config')) {
        return http.Response(
          jsonEncode({'login_banner_title': '', 'login_banner': ''}),
          200,
        );
      }
      if (request.url.path.contains('/api/my-permissions')) {
        return http.Response(
          jsonEncode({
            'user_id': 'test',
            'email': 'test@example.com',
            'permissions': <String, dynamic>{},
            'groups': <dynamic>[],
          }),
          200,
        );
      }
      return http.Response('{}', 200);
    });

    SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
    final auth = AuthService();

    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: SystemInfoTab(auth: auth)),
      ),
    );
    await tester.pump();
    await tester.pump();
    await tester.pump();

    expect(find.text('Failed to connect'), findsOneWidget);
  });

  testWidgets('shows no plugins loaded when list is empty', (tester) async {
    testAuthHttpClientOverride = MockClient((request) async {
      if (request.url.path == '/api/v1/version') {
        return http.Response(
          jsonEncode({
            'version': 'dev',
            'commit': 'unknown',
            'built_at': null,
            'plugins': <dynamic>[],
          }),
          200,
        );
      }
      if (request.url.path.contains('/api/config')) {
        return http.Response(
          jsonEncode({'login_banner_title': '', 'login_banner': ''}),
          200,
        );
      }
      if (request.url.path.contains('/api/my-permissions')) {
        return http.Response(
          jsonEncode({
            'user_id': 'test',
            'email': 'test@example.com',
            'permissions': <String, dynamic>{},
            'groups': <dynamic>[],
          }),
          200,
        );
      }
      return http.Response('{}', 200);
    });

    SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
    final auth = AuthService();

    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(body: SystemInfoTab(auth: auth)),
      ),
    );
    await tester.pump();
    await tester.pump();
    await tester.pump();

    expect(find.text('No plugins loaded'), findsOneWidget);
  });
}
