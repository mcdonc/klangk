import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/admin/admin_users_page.dart';
import 'package:klangk_frontend/admin/container_events_panel.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// A paged events envelope, matching the backend
/// `GET /events/containers` response.
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
      .encode(
        utf8.encode(
          jsonEncode({'sub': 'admin-user', 'email': 'admin@example.com'}),
        ),
      )
      .replaceAll('=', '');
  return '$header.$body.fakesig';
}

/// Build a mock client serving config + my-permissions ([permissions],
/// [isAdmin]) plus a custom handler for everything else.
http.Client _mockClient(
  Map<String, List<String>> permissions,
  Future<http.Response> Function(http.Request) handler, {
  bool isAdmin = false,
}) {
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
          'is_admin': isAdmin,
          'permissions': permissions,
          'groups': [
            {'id': 'g1', 'name': 'admin'},
          ],
        }),
        200,
      );
    }
    return handler(request);
  });
}

/// The admin permission set as the server reports it: the is_admin
/// flag covers instance-admin status (#2995); the tab permissions live
/// on their first-class resources in `/my-permissions`.
Map<String, List<String>> get _adminPermissions => {
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

  /// The panel alone (no admin-page chrome), for surface-size tests
  /// that exercise the table under width constraint (#3006).
  Widget buildPanel() {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProvider(create: (_) => WsClient()),
      ],
      child: const MaterialApp(
        home: Scaffold(body: ContainerEventsPanel()),
      ),
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
  /// parsed limit/offset/workspace query of each request) plus the empty
  /// users/groups/schedule loads so the page itself renders.
  List<http.Request> serveEvents(
    http.Response Function(
      int limit,
      int offset,
      String? workspace,
    ) eventsFor,
  ) {
    final requests = <http.Request>[];
    testAuthHttpClientOverride = _mockClient(_adminPermissions, (
      request,
    ) async {
      if (request.url.path == '/api/v1/events/containers') {
        requests.add(request);
        final limit = int.parse(request.url.queryParameters['limit'] ?? '50');
        final offset = int.parse(request.url.queryParameters['offset'] ?? '0');
        final workspace = request.url.queryParameters['workspace'];
        return eventsFor(limit, offset, workspace);
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
    }, isAdmin: true);
    return requests;
  }

  group('AdminUsersPage events tab', () {
    testWidgets('renders history rows from the paged envelope', (tester) async {
      serveEvents((limit, offset, workspace) => http.Response(
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
      expect(find.text('user admin@example.com'), findsOneWidget);
      expect(find.text('system'), findsOneWidget);
      expect(find.text('idle_timeout'), findsOneWidget);
      expect(find.text('cid-9'), findsOneWidget);
      expect(find.text('sidecar-1'), findsOneWidget);
      // Range label reflects the page slice.
      expect(find.text('1–2 of 12'), findsOneWidget);
    });

    testWidgets('next/prev page through the history', (tester) async {
      serveEvents((limit, offset, workspace) => http.Response(
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
      final requests = serveEvents((limit, offset, workspace) => http.Response(
            _eventsEnvelope(
              [
                if (workspace != null)
                  _event(workspace, workspaceName: 'Filtered WS'),
              ],
              total: workspace == null ? 0 : 1,
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
      // #3006: the id-or-name `workspace` query param.
      expect(query['workspace'], 'ws-filtered');
      expect(query['workspace_id'], isNull);
      expect(query['offset'], '0');
    });

    testWidgets('table rows fit a narrow viewport (#3006)', (tester) async {
      // The whole point of the flex-weight rewrite: at 400px the seven
      // columns ellipsize instead of laying out past the right edge.
      // Long values stay present for finders (ellipsis is paint-only)
      // and no RenderFlex overflow exception is thrown.
      serveEvents((limit, offset, workspace) => http.Response(
            _eventsEnvelope([
              _event(
                'ws-narrow',
                workspaceName: 'narrow-ws',
                containerId: 'cid-0123456789abcdef',
                networkNamespace: 'sidecar-ns-0123456789',
              ),
            ], total: 1),
            200,
          ));

      await tester.binding.setSurfaceSize(const Size(400, 900));
      await tester.pumpWidget(buildPanel());
      await tester.pumpAndSettle();

      expect(tester.takeException(), isNull);
      expect(find.text('cid-0123456789abcdef'), findsOneWidget);
      expect(find.text('sidecar-ns-0123456789'), findsOneWidget);
    });

    testWidgets('a superseded slow response never overwrites a newer one',
        (tester) async {
      // Same race the audit panel guards against (#3217 review):
      // double-Next or refresh-while-pending makes responses land out
      // of issue order; the newest request's page must win.
      final pending = <Completer<http.Response>>[];
      testAuthHttpClientOverride =
          _mockClient(_adminPermissions, (request) async {
        if (request.url.path == '/api/v1/events/containers') {
          final offset = request.url.queryParameters['offset'];
          final completer = Completer<http.Response>();
          pending.add(completer);
          await completer.future;
          return http.Response(
            _eventsEnvelope([
              _event('ws-$offset', workspaceName: 'page-$offset'),
            ], total: 5),
            200,
          );
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
      }, isAdmin: true);

      await tester.binding.setSurfaceSize(const Size(1600, 900));
      await tester.pumpWidget(buildPage());
      // Permissions resolve asynchronously — pump until the mounted
      // Events tab fires the container panel's initial load.
      while (pending.isEmpty) {
        await tester.pump(const Duration(milliseconds: 10));
      }
      pending.removeAt(0).complete(http.Response('', 200));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Events'));
      await tester.pumpAndSettle();
      expect(find.text('page-0'), findsOneWidget);

      // Two quick Next taps -> two overlapping requests (offset 1,
      // then 2 — paging advances by rows-on-page).
      final next = find.ancestor(
        of: find.byTooltip('Next page'),
        matching: find.byType(IconButton),
      );
      await tester.tap(next);
      await tester.pump();
      await tester.tap(next);
      await tester.pump();
      expect(pending.length, 2);

      // The newer (offset 2) response lands first...
      pending.removeLast().complete(http.Response('', 200));
      await tester.pumpAndSettle();
      expect(find.text('page-2'), findsOneWidget);
      expect(find.text('3–3 of 5'), findsOneWidget);

      // ...then the stale (offset 1) one: it must be discarded.
      pending.removeAt(0).complete(http.Response('', 200));
      await tester.pumpAndSettle();
      expect(find.text('page-2'), findsOneWidget);
      expect(find.text('page-1'), findsNothing);
    });

    testWidgets('tab hidden without the permission', (tester) async {
      // A delegated principal without the Events grant: the other admin
      // tabs via their own grants, but no `manage-events` on /events —
      // so no Events tab.
      testAuthHttpClientOverride = _mockClient({
        '/users': ['manage-users'],
        '/groups': ['manage-groups'],
        '/invitations': ['manage-invitations'],
      }, (request) async => http.Response('Not found', 404));

      await pumpPage(tester, toEvents: false);
      expect(find.text('Events'), findsNothing);
      expect(find.text('Users'), findsOneWidget);
    });

    testWidgets('delegated auditor gets the Events tab as their only tab', (
      tester,
    ) async {
      // #2923 review: a principal whose only admin-sphere grant is
      // `manage-events` on /events can enter the admin section and
      // sees exactly one tab — Events. No Users tab (no manage-users)
      // and no Access Control browser (it reads /acl/tree, which
      // needs manage-acls).
      testAuthHttpClientOverride = _mockClient(
        {
          '/events': ['manage-events'],
        },
        (request) async {
          if (request.url.path == '/api/v1/events/containers') {
            return http.Response(
              _eventsEnvelope([
                _event('ws-a', workspaceName: 'audit-ws', actorType: 'system'),
              ], total: 1),
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
