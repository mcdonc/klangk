import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/file_viewer/file_viewer_panel.dart';
import 'package:klangk_frontend/workspace/live_auth_token.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _QuietWsClient extends WsClient {
  @override
  Stream<Map<String, dynamic>> get customEvents => const Stream.empty();
}

String jwt(String jti) {
  String seg(Map<String, dynamic> m) =>
      base64Url.encode(utf8.encode(jsonEncode(m))).replaceAll('=', '');
  final exp = DateTime.now().millisecondsSinceEpoch ~/ 1000 + 600;
  return '${seg({'alg': 'HS256', 'typ': 'JWT'})}.'
      '${seg({'sub': 'u', 'email': 'u@example.com', 'jti': jti, 'exp': exp})}.'
      'sig';
}

void main() {
  final oldToken = jwt('old');
  final newToken = jwt('new');

  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    clearFileListCacheForTest();
    SharedPreferences.setMockInitialValues({'klangk_jwt': oldToken});
    testAuthHttpClientOverride = MockClient((request) async {
      final path = request.url.path;
      if (path.endsWith('/api/v1/config')) {
        return http.Response(jsonEncode({}), 200);
      }
      if (path.endsWith('/api/v1/my-permissions')) {
        return http.Response(
          jsonEncode({
            'user_id': 'u',
            'email': 'u@example.com',
            'permissions': {},
            'groups': [],
          }),
          200,
        );
      }
      if (path.endsWith('/api/v1/auth/refresh')) {
        return http.Response(jsonEncode({'access_token': newToken}), 200);
      }
      return http.Response('Not found', 404);
    });
  });

  tearDown(() {
    testHttpClientOverride = null;
    testAuthHttpClientOverride = null;
  });

  testWidgets('file listing after a token refresh sends the new token',
      (tester) async {
    final listingAuth = <String?>[];
    testHttpClientOverride = MockClient((request) async {
      listingAuth.add(request.headers['Authorization']);
      return http.Response(jsonEncode([]), 200);
    });

    final auth = AuthService();
    await tester.runAsync(() => Future<void>.delayed(Duration.zero));
    expect(auth.token, oldToken);

    final panelKey = GlobalKey<FileViewerPanelState>();
    await tester.pumpWidget(
      ChangeNotifierProvider<AuthService>.value(
        value: auth,
        child: MaterialApp(
          home: Scaffold(
            body: LiveAuthToken(
              builder: (context, token) => FileViewerPanel(
                key: panelKey,
                wsClient: _QuietWsClient(),
                workspaceId: 'ws-1',
                authToken: token,
                userHome: '/home/tester',
                canDownload: true,
                canWrite: true,
              ),
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(listingAuth.last, 'Bearer $oldToken');

    await tester.runAsync(auth.testRefreshToken);
    expect(auth.token, newToken);
    await tester.pump();

    panelKey.currentState!.refresh();
    await tester.pumpAndSettle();
    expect(listingAuth.last, 'Bearer $newToken');

    auth.dispose();
  });
}
