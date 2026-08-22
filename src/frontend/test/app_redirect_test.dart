import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:klangk_frontend/app.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/auth/login_page.dart';
import 'package:klangk_frontend/workspace/host_services.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// App-level redirect tests that drive the real GoRouter (built by
/// KlangkApp._createRouter) through the real AuthService login flow.
///
/// These pin the interaction between the router guards and AuthService's
/// notifyListeners calls — something the pure guard unit tests cannot see,
/// because GoRouter re-parses the *committed* location on every
/// refreshListenable notification, and login() fires two notifications in
/// quick succession (saveToken's, then the finally-block's).
class _FakeWebSocketChannel extends Fake implements WebSocketChannel {
  final _incoming = StreamController<dynamic>.broadcast();
  final _sink = _FakeSink();

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _sink;

  @override
  Future<void> get ready => Future.value();
}

class _FakeSink extends Fake implements WebSocketSink {
  @override
  void add(dynamic data) {}

  @override
  Future close([int? closeCode, String? closeReason]) async {}
}

void main() {
  String makeJwt(Map<String, dynamic> payload) {
    final header = base64Url
        .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
        .replaceAll('=', '');
    final body =
        base64Url.encode(utf8.encode(jsonEncode(payload))).replaceAll('=', '');
    return '$header.$body.fakesig';
  }

  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
    testAuthHttpClientOverride = null;
    testConfigHttpClientOverride = null;
    // The workspace page mounts a WorkspaceConnector on arrival; without a
    // channel factory it would parse a ws:// URL derived from Uri.base,
    // which is meaningless in a widget test.
    WsClient.testChannelFactory = (_) => _FakeWebSocketChannel();
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
    testConfigHttpClientOverride = null;
    WsClient.testChannelFactory = null;
  });

  // All HTTP the app issues during this flow: config (login page +
  // AuthService), login, my-permissions, and the workspace lookups the
  // workspace page performs after landing (answered with 404/empty so the
  // page settles into its error state instead of connecting anything).
  void installMocks(String token) {
    testConfigHttpClientOverride = MockClient((request) async {
      return http.Response(
        jsonEncode({
          'registration_enabled': true,
          'login_banner_title': '',
          'login_banner': '',
          'oidc_providers': [],
          'auth_modes': 'password',
        }),
        200,
      );
    });
    testAuthHttpClientOverride = MockClient((request) async {
      final path = request.url.path;
      if (path.contains('/api/v1/auth/login')) {
        return http.Response(
          jsonEncode({'access_token': token}),
          200,
        );
      }
      if (path.contains('/api/v1/my-permissions')) {
        return http.Response(
          jsonEncode({
            'user_id': 'u1',
            'email': 'user@example.com',
            'permissions': <String, List<String>>{},
            'groups': <Map<String, dynamic>>[],
          }),
          200,
        );
      }
      if (path.contains('/api/v1/config')) {
        return http.Response(
          jsonEncode({'login_banner_title': '', 'login_banner': ''}),
          200,
        );
      }
      // Workspace list / shared list / per-resource permissions lookups.
      if (path.contains('/api/v1/workspaces')) {
        return http.Response(jsonEncode([]), 200);
      }
      return http.Response('Not found', 404);
    });
  }

  Widget buildApp() {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProxyProvider<AuthService, WsClient>(
          create: (_) => WsClient(),
          update: (_, auth, client) => client!..updateAuth(auth),
        ),
        Provider<WorkspaceServices>(
          create: (ctx) => HostWorkspaceServices(
            ctx.read<WsClient>(),
            ctx.read<AuthService>(),
          ),
        ),
      ],
      child: const KlangkApp(initialLocation: '/workspace/test-ws-123'),
    );
  }

  Future<String> currentLocation(WidgetTester tester) async {
    // The Router's internal Navigator sits below InheritedGoRouter, so its
    // context can resolve GoRouter.of (the builder-wrapped StaleBuildBanner
    // is above the Router and cannot).
    final ctx = tester.element(find.byType(Navigator).first);
    final router = GoRouter.of(ctx);
    return router.routerDelegate.currentConfiguration.uri.toString();
  }

  testWidgets(
      'deep-link login lands on the stashed workspace, not /workspaces (#2670)',
      (tester) async {
    // No exp claim -> no token-refresh timer for the test to fight with.
    installMocks(makeJwt({'sub': 'user-1', 'email': 'user@example.com'}));

    await tester.pumpWidget(buildApp());
    await tester.pumpAndSettle();
    expect(find.text('Log In'), findsWidgets);
    expect(await currentLocation(tester), '/login');

    // Log in through the real form — this exercises the real login()
    // double notifyListeners (saveToken + finally), which is exactly what
    // the E2E deep-link test drives.
    final fields = find.byType(TextField);
    await tester.enterText(fields.first, 'user@example.com');
    await tester.enterText(fields.last, 'password');
    await tester.tap(find.widgetWithText(FilledButton, 'Log In'));

    // Bounded pumps: process the notify -> re-parse -> redirect chain
    // without pumpAndSettle (the workspace page's connector retries on
    // timers that never quiesce in a test).
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 10));
    }

    expect(await currentLocation(tester), '/workspace/test-ws-123');
  });
}
