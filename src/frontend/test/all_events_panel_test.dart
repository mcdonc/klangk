import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/admin/all_events_panel.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// A paged merged-events envelope, matching the backend
/// `GET /events` response (#3251).
String _mergedEnvelope(
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

Map<String, dynamic> _mergedEvent(
  String source,
  dynamic id, {
  String event = 'login',
  String? actorId,
  String? actorEmail,
  String? workspaceId,
  String? workspaceName,
  double createdAt = 1767225600.0,
  Map<String, dynamic>? data,
}) =>
    {
      'source': source,
      'id': id,
      'created_at': createdAt,
      'event': event,
      'actor_id': actorId,
      'actor_email': actorEmail,
      'workspace_id': workspaceId,
      'workspace_name': workspaceName,
      'data': data ?? const {},
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

/// Build a mock client serving config + my-permissions (admin) plus a
/// custom handler for everything else.
http.Client _mockClient(
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
          'is_admin': true,
          'permissions': {
            '/users': ['manage-users'],
            '/events': ['view', 'manage-events'],
          },
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

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({'klangk_jwt': _adminToken});
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  Widget buildPanel() {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProvider(create: (_) => WsClient()),
      ],
      child: const MaterialApp(
        home: Scaffold(body: AllEventsPanel()),
      ),
    );
  }

  /// Serve the merged endpoint via [eventsFor] (called with the parsed
  /// limit/offset and the three substring filters of each request).
  List<http.Request> serveEvents(
    http.Response Function(
      int limit,
      int offset,
      String? event,
      String? actor,
      String? workspace,
    ) eventsFor,
  ) {
    final requests = <http.Request>[];
    testAuthHttpClientOverride = _mockClient((request) async {
      if (request.url.path == '/api/v1/events') {
        requests.add(request);
        final limit = int.parse(request.url.queryParameters['limit'] ?? '50');
        final offset = int.parse(request.url.queryParameters['offset'] ?? '0');
        final event = request.url.queryParameters['event'];
        final actor = request.url.queryParameters['actor'];
        final workspace = request.url.queryParameters['workspace'];
        return eventsFor(limit, offset, event, actor, workspace);
      }
      return http.Response('Not found', 404);
    });
    return requests;
  }

  Future<void> pumpPanel(WidgetTester tester) async {
    await tester.binding.setSurfaceSize(const Size(1600, 900));
    await tester.pumpWidget(buildPanel());
    await tester.pumpAndSettle();
  }

  group('AllEventsPanel', () {
    testWidgets('renders merged rows with source badges', (tester) async {
      serveEvents((limit, offset, event, actor, workspace) => http.Response(
            _mergedEnvelope([
              _mergedEvent(
                'audit',
                1,
                event: 'login',
                actorId: 'u-1',
                actorEmail: 'admin@example.com',
                data: {'via': 'password'},
              ),
              _mergedEvent(
                'container',
                1,
                event: 'start',
                actorId: 'u-1',
                actorEmail: 'admin@example.com',
                workspaceId: 'ws-1',
                workspaceName: 'events-ws',
                data: {'cause': 'api', 'container_id': 'cid-9'},
              ),
              _mergedEvent(
                'egress',
                'c-3',
                event: 'egress.denied',
                actorId: 'u-1',
                workspaceId: 'ws-1',
                workspaceName: 'events-ws',
                data: {'dest_host': 'example.com', 'decision': 'denied'},
              ),
            ], total: 3),
            200,
          ));

      await pumpPanel(tester);

      // One badge per origin table, one row per event.
      expect(find.text('audit'), findsOneWidget);
      expect(find.text('container'), findsOneWidget);
      expect(find.text('egress'), findsOneWidget);
      expect(find.text('login'), findsOneWidget);
      expect(find.text('start'), findsOneWidget);
      expect(find.text('egress.denied'), findsOneWidget);
      // Workspace names resolved for the workspace-carrying rows;
      // actor email when known, raw id otherwise (an unresolvable
      // actor — the egress row here — falls back to its id).
      expect(find.text('events-ws'), findsNWidgets(2));
      expect(find.text('admin@example.com'), findsNWidgets(2));
      expect(find.text('u-1'), findsOneWidget);
      expect(find.text('1–3 of 3'), findsOneWidget);
    });

    testWidgets('next/prev page through the merged stream', (tester) async {
      serveEvents((limit, offset, event, actor, workspace) => http.Response(
            _mergedEnvelope(
              [
                _mergedEvent('audit', offset + 1,
                    actorEmail: 'pager@example.com'),
              ],
              total: 3,
              limit: limit,
              offset: offset,
            ),
            200,
          ));

      await pumpPanel(tester);

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
      expect(find.text('2–2 of 3'), findsOneWidget);
      expect(tester.widget<IconButton>(prev).onPressed, isNotNull);

      await tester.tap(prev);
      await tester.pumpAndSettle();
      expect(find.text('1–1 of 3'), findsOneWidget);
    });

    testWidgets('filter fields debounce into fresh queries', (tester) async {
      final requests = serveEvents(
        (limit, offset, event, actor, workspace) => http.Response(
          _mergedEnvelope(
            [
              if (event == 'login')
                _mergedEvent('audit', 9, actorEmail: 'filter-hit@example.com'),
            ],
            total: event == 'login' ? 1 : 0,
          ),
          200,
        ),
      );

      await pumpPanel(tester);

      await tester.enterText(
        find.byKey(const ValueKey('all-event-filter')),
        'login',
      );
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpAndSettle();

      expect(find.text('filter-hit@example.com'), findsOneWidget);
      final query = requests.last.url.queryParameters;
      expect(query['event'], 'login');
      expect(query['actor'], isNull);
      expect(query['workspace'], isNull);
      expect(query['offset'], '0');

      await tester.enterText(
        find.byKey(const ValueKey('all-actor-filter')),
        'admin',
      );
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpAndSettle();
      expect(requests.last.url.queryParameters['actor'], 'admin');

      await tester.enterText(
        find.byKey(const ValueKey('all-workspace-filter')),
        'acme',
      );
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpAndSettle();
      expect(requests.last.url.queryParameters['workspace'], 'acme');
    });

    testWidgets('row expansion shows the origin row JSON', (tester) async {
      serveEvents((limit, offset, event, actor, workspace) => http.Response(
            _mergedEnvelope([
              _mergedEvent(
                'egress',
                'c-7',
                event: 'egress.allowed',
                actorId: 'u-1',
                workspaceId: 'ws-1',
                workspaceName: 'detail-ws',
                data: {
                  'dest_host': 'allowed.example',
                  'decision': 'allowed',
                  'duration': 'tilrestart',
                },
              ),
            ], total: 1),
            200,
          ));

      await pumpPanel(tester);

      await tester.tap(find.text('egress.allowed'));
      await tester.pumpAndSettle();

      final detail = find.byKey(const ValueKey('all-event-detail'));
      expect(detail, findsOneWidget);
      // The origin row's fields render inside the detail area.
      expect(find.textContaining('allowed.example'), findsOneWidget);
      expect(find.textContaining('tilrestart'), findsOneWidget);
    });

    testWidgets('revoked and denied egress verdicts read red', (tester) async {
      serveEvents((limit, offset, event, actor, workspace) => http.Response(
            _mergedEnvelope([
              _mergedEvent('egress', 'c-8', event: 'egress.revoked'),
              _mergedEvent('egress', 'c-9', event: 'egress.allowed'),
            ], total: 2),
            200,
          ));

      await pumpPanel(tester);

      // The revoked verdict's chip shares the negative color with
      // denied/expired rows; the plain allow stays positive. The chip
      // paints via its BoxDecoration, so walk the ancestors for the
      // first boxed one.
      Color chipColor(String text) {
        for (final element in find
            .ancestor(of: find.text(text), matching: find.byType(Container))
            .evaluate()) {
          final decoration = (element.widget as Container).decoration;
          if (decoration is BoxDecoration && decoration.color != null) {
            return decoration.color!;
          }
        }
        fail('no colored chip ancestor for $text');
      }

      expect(chipColor('egress.revoked'), KColors.accentRed);
      expect(chipColor('egress.allowed'), KColors.accentGreen);
    });

    testWidgets('empty state', (tester) async {
      serveEvents(
        (limit, offset, event, actor, workspace) => http.Response(
          _mergedEnvelope([]),
          200,
        ),
      );
      await pumpPanel(tester);
      expect(find.text('No events recorded'), findsOneWidget);
    });

    testWidgets('error state offers a retry', (tester) async {
      serveEvents(
        (limit, offset, event, actor, workspace) => http.Response('boom', 500),
      );
      await pumpPanel(tester);
      expect(find.text('Failed to load events (500)'), findsOneWidget);
      expect(find.byType(OutlinedButton), findsOneWidget);
    });
  });
}
