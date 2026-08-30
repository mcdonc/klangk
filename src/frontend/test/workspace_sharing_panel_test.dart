import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/workspace/workspace_sharing_panel.dart';

/// Default JWT for a logged-in user.
String get _token {
  final header = base64Url
      .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
      .replaceAll('=', '');
  final body = base64Url
      .encode(utf8.encode(jsonEncode({
        'sub': 'user-1',
        'email': 'user@example.com',
      })))
      .replaceAll('=', '');
  return '$header.$body.fakesig';
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({'klangk_jwt': _token});
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  Widget buildPanel({required bool canEditAcl}) {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
      ],
      child: MaterialApp(
        home: Scaffold(
          body: WorkspaceSharingPanel(
            workspaceId: 'ws1',
            canEditAcl: canEditAcl,
          ),
        ),
      ),
    );
  }

  void stubHttp() {
    testAuthHttpClientOverride = MockClient((request) async {
      final path = request.url.path;
      if (path == '/api/v1/workspaces/ws1/roles') {
        return http.Response(jsonEncode([]), 200);
      }
      if (path == '/api/v1/workspaces/ws1/acl') {
        return http.Response(
          jsonEncode([
            {
              'id': 1,
              'action': 1,
              'principal_type': 1,
              'permission': '*',
              'user_id': 'u1',
              'principal': 'user@example.com',
            },
          ]),
          200,
        );
      }
      return http.Response('Not found', 404);
    });
  }

  testWidgets('hides the advanced ACL editor without change-acls (#2764)',
      (tester) async {
    stubHttp();
    await tester.pumpWidget(buildPanel(canEditAcl: false));
    await tester.pumpAndSettle();
    expect(find.text('Advanced: Access Control'), findsNothing);
  });

  testWidgets('offers the advanced ACL editor with change-acls (#2764)',
      (tester) async {
    stubHttp();
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();
    expect(find.text('Advanced: Access Control'), findsOneWidget);

    // Expanding mounts the raw ACE editor (its header row loads the
    // stubbed entry).
    await tester.tap(find.text('Advanced: Access Control'));
    await tester.pumpAndSettle();
    expect(find.text('Access Control Entries'), findsOneWidget);
    expect(find.text('user@example.com'), findsOneWidget);
  });
}
