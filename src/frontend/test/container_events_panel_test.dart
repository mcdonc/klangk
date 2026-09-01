import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/admin/admin_users_page.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// A paged events envelope, matching the backend
/// `GET /admin/container-events` response.
String _eventsEnvelope(
  List<Map<String, dynamic>> items, {
  int total = 0,
  int limit = 50,
  int offset = 0,
}) =>
    jsonEncode({
      'items': items,
      'total': total,
      'limit': limit,
      'offset': offset,
    });

Map<String, dynamic> _event(
  String workspaceId, {
  String? workspaceName,
  String event = 'start',
  String actorType = 'user',
  String? actorId,
  String? actorEmail,
  String cause = 'api',
  String? containerId,
  String? networkNamespace,
  double createdAt = 1767225600.0,
}) =>
    {
      'id': 1,
      'workspace_id': workspaceId,
      'workspace_name': workspaceName,
      'event': event,
      'actor_type': actorType,
      'actor_id': actorId,
      'actor_email': actorEmail,
      'cause': cause,
      'container_id': containerId,
      'container_role': 'workspace',
      'network_namespace': networkNamespace,
      'created_at': createdAt,
    };

/// Default JWT for a logged-in admin user.
String get _adminToken {
  final header = base64Url
      .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
      .replaceAll('=', '');
  final body = base64Url
      .encode(utf8.encode(jsonEncode({
        'sub': 'admin-user',
        'email': 'admin@example.com',
      })))
      .replaceAll('=', '');
  return '$header.$body.fakesig';
}

/// Build a mock client serving config + my-permissions ([permissions])
/// plus a custom handler for everything else.
http.Client _mockClient(
  Map<String, List<String>> permissions,
  Future<http.Response> Function(http.Request) handler,
) {
  return MockClient((request) async {
    if (request.url.path.contains('/api/v1/config')) {
      return http.Response(
        jsonEncode({'login_banner_title': '', 'login_banner': ''}),
        200,
      );
    }
    if (request.url.path.contains('/api/v1/my-permissions')) {
      return http.Response(
        jsonEncode({
          'user_id': 'admin-user',
          'email': 'admin@example.com',
          'permissions': permissions,
          'groups': [
            {'id': 'g1', 'name': 'admin'}
          ],
        }),
        200,
      );
    }
    return handler(request);
  });
}

/// The admin permission set as the server reports it: the `/admin` `*`
/// wildcard expands to every permission on every `/admin/*` resource in
/// `/my-permissions`, so the Events resource is listed alongside it.
Map<String, List<String>> get _adminPermissions => {
      '/admin': ['*'],
      '/users': ['manage-users'],
      '/events': ['view', 'manage-events'],
    };

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({'klangk_jwt': _adminToken});
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  Widget buildPage() {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProvider(create: (_) => WsClient()),
      ],
      child: const MaterialApp(home: AdminUsersPage()),
    );
  }

  /// Pump the page on a wide surface (the admin tab row overflows on the
  /// default 800px test surface) and settle. Optionally navigates to the
  /// Events tab before settling.
  Future<void> pumpPage(WidgetTester tester, {bool toEvents = true}) async {
    await tester.binding.setSurfaceSize(const Size(1600, 900));
    await tester.pumpWidget(buildPage());
    await tester.pumpAndSettle();
    if (toEvents) {
      await tester.tap(find.text('Events'));
      await tester.pumpAndSettle();
    }
  }

  /// Serve the container-events endpoint via [eventsFor] (called with the
  /// parsed limit/offset/workspace_id of each request) plus the empty
  /// users/groups/schedule loads so the page itself renders.
  List<http.Request> serveEvents(
    http.Response Function(
      int limit,
      int offset,
      String? workspaceId,
    ) eventsFor,
  ) {
    final requests = <http.Request>[];
    testAuthHttpClientOverride =
        _mockClient(_adminPermissions, (request) async {
      if (request.url.path == '/api/v1/events') {
        requests.add(request);
        final limit = int.parse(request.url.queryParameters['limit'] ?? '50');
        final offset = int.parse(request.url.queryParameters['offset'] ?? '0');
        final workspaceId = request.url.queryParameters['workspace_id'];
        return eventsFor(limit, offset, workspaceId);
      }
      if (request.url.path == '/api/v1/users') {
        return http.Response(
          jsonEncode({
            'users': <Map<String, dynamic>>[],
            'page': 1,
            'page_size': 10,
            'total': 0,
          }),
          200,
        );
      }
      if (request.url.path == '/api/v1/groups') {
        return http.Response(jsonEncode([]), 200);
      }
      if (request.url.path == '/api/v1/server/schedule' &&
          request.method == 'GET') {
        return http.Response(jsonEncode({'schedules': []}), 200);
      }
      return http.Response('Not found', 404);
    });
    return requests;
  }

  group('AdminUsersPage events tab', () {
    testWidgets('renders history rows from the paged envelope', (tester) async {
      serveEvents((limit, offset, workspaceId) => http.Response(
            _eventsEnvelope([
              _event(
                'ws-1',
                workspaceName: 'events-ws',
                event: 'stop',
                actorEmail: 'admin@example.com',
                cause: 'idle_timeout',
                containerId: 'cid-9',
                networkNamespace: 'sidecar-1',
              ),
              _event(
                'gone-ws',
                actorType: 'system',
                cause: 'auto_start',
              ),
            ], total: 12),
            200,
          ));

      await pumpPage(tester);

      // Resolved workspace name, and the raw id fallback for a deleted
      // workspace.
      expect(find.text('events-ws'), findsOneWidget);
      expect(find.text('gone-ws'), findsOneWidget);
      // Actor labels: email when resolved, bare type for system rows.
      expect(
        find.text('user admin@example.com'),
        findsOneWidget,
      );
      expect(find.text('system'), findsOneWidget);
      expect(find.text('idle_timeout'), findsOneWidget);
      expect(find.text('cid-9'), findsOneWidget);
      expect(find.text('sidecar-1'), findsOneWidget);
      // Range label reflects the page slice.
      expect(find.text('1–2 of 12'), findsOneWidget);
    });

    testWidgets('next/prev page through the history', (tester) async {
      serveEvents((limit, offset, workspaceId) => http.Response(
            _eventsEnvelope(
              [
                _event('ws-${offset + 1}', workspaceName: 'page-ws'),
              ],
              total: 3,
              limit: limit,
              offset: offset,
            ),
            200,
          ));

      await pumpPage(tester);

      final next = find.ancestor(
        of: find.byTooltip('Next page'),
        matching: find.byType(IconButton),
      );
      final prev = find.ancestor(
        of: find.byTooltip('Previous page'),
        matching: find.byType(IconButton),
      );
      expect(tester.widget<IconButton>(prev).onPressed, isNull);
      expect(tester.widget<IconButton>(next).onPressed, isNotNull);
      expect(find.text('1–1 of 3'), findsOneWidget);

      await tester.tap(next);
      await tester.pumpAndSettle();
      expect(find.text('page-ws'), findsOneWidget);
      expect(find.text('2–2 of 3'), findsOneWidget);
      expect(tester.widget<IconButton>(prev).onPressed, isNotNull);

      await tester.tap(prev);
      await tester.pumpAndSettle();
      expect(find.text('1–1 of 3'), findsOneWidget);
    });

    testWidgets('workspace filter debounces into a fresh query',
        (tester) async {
      final requests =
          serveEvents((limit, offset, workspaceId) => http.Response(
                _eventsEnvelope(
                  [
                    if (workspaceId != null)
                      _event(workspaceId, workspaceName: 'Filtered WS'),
                  ],
                  total: workspaceId == null ? 0 : 1,
                ),
                200,
              ));

      await pumpPage(tester);

      await tester.enterText(
        find.byKey(const ValueKey('events-workspace-filter')),
        'ws-filtered',
      );
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpAndSettle();

      expect(find.text('Filtered WS'), findsOneWidget);
      final query = requests.last.url.queryParameters;
      expect(query['workspace_id'], 'ws-filtered');
      expect(query['offset'], '0');
    });

    testWidgets('tab hidden without the permission', (tester) async {
      // A non-delegated, non-wildcard principal: the other admin tabs
      // via their own grants, but no `container-events` on
      // /admin/container-events — so no Events tab.
      testAuthHttpClientOverride = _mockClient(
        {
          '/users': ['manage-users'],
          '/groups': ['manage-groups'],
          '/invitations': ['manage-invitations'],
          '/admin': ['admin'],
        },
        (request) async => http.Response('Not found', 404),
      );

      await pumpPage(tester, toEvents: false);
      expect(find.text('Events'), findsNothing);
      expect(find.text('Users'), findsOneWidget);
    });

    testWidgets('delegated auditor gets the Events tab as their only tab',
        (tester) async {
      // #2923 review: a principal whose only admin-sphere grant is
      // `container-events` on /admin/container-events can enter the
      // admin section and sees exactly one tab — Events. No Users tab
      // (no view on /admin/users) and no Access Control browser (it
      // reads /admin/acl/tree, which needs full admin).
      testAuthHttpClientOverride = _mockClient(
        {
          '/events': ['manage-events'],
        },
        (request) async {
          if (request.url.path == '/api/v1/events') {
            return http.Response(
              _eventsEnvelope(
                [
                  _event('ws-a', workspaceName: 'audit-ws', actorType: 'system')
                ],
                total: 1,
              ),
              200,
            );
          }
          return http.Response('Not found', 404);
        },
      );

      await pumpPage(tester, toEvents: false);
      expect(find.text('Events'), findsOneWidget);
      expect(find.text('Users'), findsNothing);
      expect(find.text('Access Control'), findsNothing);

      await tester.tap(find.text('Events'));
      await tester.pumpAndSettle();
      expect(find.text('audit-ws'), findsOneWidget);
    });
  });
}
