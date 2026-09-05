import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/admin/admin_users_page.dart';
import 'package:klangk_frontend/admin/audit_events_panel.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// A paged audit-events envelope, matching the backend
/// `GET /events/audit` response (#3205).
String _auditEnvelope(
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

Map<String, dynamic> _auditEvent(
  int id, {
  String event = 'user.create',
  String? actorId,
  String? actorEmail,
  String? targetType = 'user',
  String? targetId,
  Map<String, dynamic>? detail,
  String? sourceIp,
  String? userAgent,
  double createdAt = 1767225600.0,
}) =>
    {
      'id': id,
      'event': event,
      'actor_id': actorId,
      'actor_email': actorEmail,
      'target_type': targetType,
      'target_id': targetId,
      'detail': detail,
      'source_ip': sourceIp,
      'user_agent': userAgent,
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

/// The admin permission set as the server reports it.
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
  /// that exercise the table under width constraint (#3006 pattern).
  Widget buildPanel() {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProvider(create: (_) => WsClient()),
      ],
      child: const MaterialApp(
        home: Scaffold(body: AuditEventsPanel()),
      ),
    );
  }

  /// Serve the audit endpoint via [auditFor] (called with the parsed
  /// limit/offset and the three substring filters of each request)
  /// plus the empty users/groups/schedule loads so the page renders.
  List<http.Request> serveAudit(
    http.Response Function(
      int limit,
      int offset,
      String? event,
      String? actor,
      String? target,
    ) auditFor, {
    bool withPage = false,
  }) {
    final requests = <http.Request>[];
    testAuthHttpClientOverride = _mockClient(_adminPermissions, (
      request,
    ) async {
      if (request.url.path == '/api/v1/events/audit') {
        requests.add(request);
        final limit = int.parse(request.url.queryParameters['limit'] ?? '50');
        final offset = int.parse(request.url.queryParameters['offset'] ?? '0');
        final event = request.url.queryParameters['event'];
        final actor = request.url.queryParameters['actor'];
        final target = request.url.queryParameters['target'];
        return auditFor(limit, offset, event, actor, target);
      }
      if (withPage) {
        if (request.url.path == '/api/v1/events/containers') {
          requests.add(request);
          return http.Response(_auditEnvelope([]), 200);
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
      }
      return http.Response('Not found', 404);
    }, isAdmin: true);
    return requests;
  }

  /// Pump the panel alone on a wide surface and settle.
  Future<void> pumpPanel(WidgetTester tester) async {
    await tester.binding.setSurfaceSize(const Size(1600, 900));
    await tester.pumpWidget(buildPanel());
    await tester.pumpAndSettle();
  }

  group('AuditEventsPanel', () {
    testWidgets('renders rows from the paged envelope', (tester) async {
      serveAudit((limit, offset, event, actor, target) => http.Response(
            _auditEnvelope([
              _auditEvent(
                1,
                event: 'user.create',
                actorId: 'u-1',
                actorEmail: 'admin@example.com',
                targetId: 'u-2',
                sourceIp: '10.0.0.7',
                userAgent: 'Mozilla/5.0 (X11; Linux x86_64) klangk/1.0',
              ),
              _auditEvent(
                2,
                event: 'login.failed',
                targetId: 'u-9',
                detail: {'via': 'password', 'identifier': 'admin@example.com'},
              ),
              _auditEvent(
                3,
                event: 'acl.replace',
                actorId: 'u-7',
                targetType: 'workspace',
                targetId: 'ws-1',
              ),
            ], total: 12),
            200,
          ));

      await pumpPanel(tester);

      // Event names as chips, actor email / fallbacks, target labels.
      expect(find.text('user.create'), findsOneWidget);
      expect(find.text('login.failed'), findsOneWidget);
      expect(find.text('acl.replace'), findsOneWidget);
      expect(find.text('admin@example.com'), findsOneWidget);
      // No actor on login.failed -> 'anonymous'; bare id when the
      // email was never denormalized (purged actor).
      expect(find.text('anonymous'), findsOneWidget);
      expect(find.text('u-7'), findsOneWidget);
      expect(find.text('user u-2'), findsOneWidget);
      expect(find.text('user u-9'), findsOneWidget);
      expect(find.text('workspace ws-1'), findsOneWidget);
      expect(find.text('10.0.0.7'), findsOneWidget);
      expect(
        find.text('Mozilla/5.0 (X11; Linux x86_64) klangk/1.0'),
        findsOneWidget,
      );
      // Range label reflects the page slice.
      expect(find.text('1–3 of 12'), findsOneWidget);
    });

    testWidgets('next/prev page through the history', (tester) async {
      serveAudit((limit, offset, event, actor, target) => http.Response(
            _auditEnvelope(
              [
                _auditEvent(offset + 1, actorEmail: 'pager@example.com'),
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

    testWidgets('event/actor/target filters debounce into query params',
        (tester) async {
      final requests = serveAudit(
        (limit, offset, event, actor, target) => http.Response(
          _auditEnvelope(
            [
              if (event != null || actor != null || target != null)
                _auditEvent(1, actorEmail: 'filtered@example.com'),
            ],
            total: event != null || actor != null || target != null ? 1 : 0,
          ),
          200,
        ),
      );

      await pumpPanel(tester);

      await tester.enterText(
        find.byKey(const ValueKey('audit-event-filter')),
        'group.member',
      );
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const ValueKey('audit-actor-filter')),
        'admin@example.com',
      );
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byKey(const ValueKey('audit-target-filter')),
        'u-2',
      );
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpAndSettle();

      expect(find.text('filtered@example.com'), findsOneWidget);
      final query = requests.last.url.queryParameters;
      expect(query['event'], 'group.member');
      expect(query['actor'], 'admin@example.com');
      expect(query['target'], 'u-2');
      expect(query['offset'], '0');
    });

    testWidgets('tapping a row expands the read-only detail view',
        (tester) async {
      serveAudit((limit, offset, event, actor, target) => http.Response(
            _auditEnvelope([
              _auditEvent(
                7,
                event: 'user.delete',
                actorEmail: 'admin@example.com',
                targetId: 'u-2',
                detail: {
                  'email': 'gone@example.com',
                  'cascade': ['workspaces', 'sessions'],
                },
                sourceIp: '192.168.1.4',
                userAgent: 'klangk-cli/1.0',
              ),
            ], total: 1),
            200,
          ));

      await pumpPanel(tester);

      expect(find.byKey(const ValueKey('audit-event-detail')), findsNothing);
      await tester.tap(find.text('user.delete'));
      await tester.pumpAndSettle();

      final detail = find.byKey(const ValueKey('audit-event-detail'));
      expect(detail, findsOneWidget);
      // Pretty-printed JSON detail, verbatim.
      expect(
          find.textContaining('"email": "gone@example.com"'), findsOneWidget);
      expect(find.textContaining('"cascade"'), findsOneWidget);
      // Correlation fields repeated in full.
      expect(find.text('192.168.1.4'), findsWidgets);
      expect(find.text('klangk-cli/1.0'), findsWidgets);

      // Tapping again collapses.
      await tester.tap(find.text('user.delete'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('audit-event-detail')), findsNothing);
    });

    testWidgets('null detail renders a placeholder, not an error',
        (tester) async {
      serveAudit((limit, offset, event, actor, target) => http.Response(
            _auditEnvelope([
              _auditEvent(
                4,
                event: 'logout',
                actorEmail: 'u@example.com',
                detail: null,
              ),
            ], total: 1),
            200,
          ));

      await pumpPanel(tester);
      await tester.tap(find.text('logout'));
      await tester.pumpAndSettle();

      expect(find.byKey(const ValueKey('audit-event-detail')), findsOneWidget);
      // Source IP / user agent placeholders plus the detail dash.
      expect(find.text('—'), findsNWidgets(3));
    });

    testWidgets('empty history shows the empty state', (tester) async {
      serveAudit((limit, offset, event, actor, target) =>
          http.Response(_auditEnvelope([]), 200));

      await pumpPanel(tester);
      expect(find.text('No audit events recorded'), findsOneWidget);
      expect(find.text('0 events'), findsOneWidget);
    });

    testWidgets('a superseded slow response never overwrites a newer one',
        (tester) async {
      // Overlapping loads are the common case (independent filter
      // debouncers): the newer request's rows must win even when the
      // older response lands last.
      final pending = <Completer<http.Response>>[];
      testAuthHttpClientOverride =
          _mockClient(_adminPermissions, (request) async {
        if (request.url.path == '/api/v1/events/audit') {
          final actor = request.url.queryParameters['actor'];
          final completer = Completer<http.Response>();
          pending.add(completer);
          final resp = await completer.future;
          final marker = actor != null ? 'actor-row' : 'event-row';
          return http.Response(
            _auditEnvelope([
              _auditEvent(1, event: 'user.create', targetId: marker),
            ], total: 1),
            resp.statusCode,
          );
        }
        return http.Response('Not found', 404);
      }, isAdmin: true);

      await tester.binding.setSurfaceSize(const Size(1600, 900));
      await tester.pumpWidget(buildPanel());
      // Initial load.
      pending.removeAt(0).complete(http.Response('', 200));
      await tester.pumpAndSettle();

      // Two filter edits -> two overlapping requests.
      await tester.enterText(
        find.byKey(const ValueKey('audit-event-filter')),
        'user.create',
      );
      await tester.pump(const Duration(milliseconds: 350));
      await tester.enterText(
        find.byKey(const ValueKey('audit-actor-filter')),
        'admin@example.com',
      );
      await tester.pump(const Duration(milliseconds: 350));
      expect(pending.length, 2);

      // The newer (actor) response lands first...
      pending.removeLast().complete(http.Response('', 200));
      await tester.pumpAndSettle();
      expect(find.text('user actor-row'), findsOneWidget);

      // ...then the stale (event-only) one: it must be discarded.
      pending.removeAt(0).complete(http.Response('', 200));
      await tester.pumpAndSettle();
      expect(find.text('user actor-row'), findsOneWidget);
      expect(find.text('user event-row'), findsNothing);
    });

    testWidgets('a filter change collapses an open expansion', (tester) async {
      serveAudit((limit, offset, event, actor, target) => http.Response(
            _auditEnvelope([
              _auditEvent(
                1,
                event: 'user.update',
                actorEmail: 'admin@example.com',
                targetId: 'u-2',
                detail: {'field': 'handle'},
              ),
              _auditEvent(
                2,
                event: 'user.create',
                actorEmail: 'admin@example.com',
                targetId: 'u-3',
              ),
            ], total: 2),
            200,
          ));

      await pumpPanel(tester);
      await tester.tap(find.text('user.update'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('audit-event-detail')), findsOneWidget);

      // A filter change loads a fresh result set: the expansion goes.
      await tester.enterText(
        find.byKey(const ValueKey('audit-event-filter')),
        'user.create',
      );
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('audit-event-detail')), findsNothing);
    });

    testWidgets('table rows fit a narrow viewport (#3006 pattern)',
        (tester) async {
      serveAudit((limit, offset, event, actor, target) => http.Response(
            _auditEnvelope([
              _auditEvent(
                1,
                event: 'workspace.role.change',
                actorEmail: 'some-very-long-email-address@example.com',
                targetType: 'workspace',
                targetId: 'ws-0123456789abcdef',
                sourceIp: '10.1.2.3',
                userAgent: 'Mozilla/5.0 (X11; Linux x86_64) '
                    'AppleWebKit/537.36 klangk/1.0.5',
              ),
            ], total: 1),
            200,
          ));

      await tester.binding.setSurfaceSize(const Size(400, 900));
      await tester.pumpWidget(buildPanel());
      await tester.pumpAndSettle();

      expect(tester.takeException(), isNull);
      expect(find.text('workspace ws-0123456789abcdef'), findsOneWidget);
      expect(find.text('10.1.2.3'), findsOneWidget);
    });
  });

  group('AdminUsersPage events subtab', () {
    /// Pump the whole admin page on a wide surface, navigate to the
    /// Events tab, and switch to the Audit subtab.
    Future<void> pumpToAudit(WidgetTester tester) async {
      await tester.binding.setSurfaceSize(const Size(1600, 900));
      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();
      await tester.tap(find.text('Events'));
      await tester.pumpAndSettle();
      // The Containers subtab is the default: its panel is up first.
      // (Offstage panels stay mounted — the keep-alive — so
      // visibility asserts use hitTestable, not findsNothing.)
      expect(
        find.byKey(const ValueKey('events-workspace-filter')).hitTestable(),
        findsOneWidget,
      );
      await tester.tap(find.text('Audit'));
      await tester.pumpAndSettle();
    }

    testWidgets('switches between the Containers and Audit subtabs',
        (tester) async {
      serveAudit(
        (limit, offset, event, actor, target) => http.Response(
          _auditEnvelope([
            _auditEvent(1, actorEmail: 'admin@example.com', targetId: 'u-2'),
          ], total: 1),
          200,
        ),
        withPage: true,
      );

      await pumpToAudit(tester);

      // The audit panel is the visible subtab now.
      expect(
        find.byKey(const ValueKey('events-workspace-filter')).hitTestable(),
        findsNothing,
      );
      expect(
        find.byKey(const ValueKey('audit-event-filter')).hitTestable(),
        findsOneWidget,
      );
      expect(find.text('user.create'), findsOneWidget);

      // Keep-alive (#3217 review): subtab state survives switching —
      // the hidden panel stays mounted offstage, matching the
      // IndexedStack keep-alive the admin page gives its top tabs.
      await tester.enterText(
        find.byKey(const ValueKey('audit-actor-filter')),
        'kept-filter',
      );
      await tester.pump(const Duration(milliseconds: 350));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Containers'));
      await tester.pumpAndSettle();
      expect(find.byKey(const ValueKey('events-workspace-filter')),
          findsOneWidget);
      await tester.tap(find.text('Audit'));
      await tester.pumpAndSettle();
      final field = tester.widget<TextField>(
        find.byKey(const ValueKey('audit-actor-filter')),
      );
      expect(field.controller!.text, 'kept-filter');
    });

    testWidgets(
        'the audit panel loads lazily — no request before first selection',
        (tester) async {
      // The keep-alive IndexedStack builds a subtab's panel only when
      // first selected: visiting the Events tab must not fire an
      // /events/audit request.
      final requests = serveAudit(
        (limit, offset, event, actor, target) =>
            http.Response(_auditEnvelope([]), 200),
        withPage: true,
      );

      await tester.binding.setSurfaceSize(const Size(1600, 900));
      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();
      await tester.tap(find.text('Events'));
      await tester.pumpAndSettle();

      expect(
        requests.where((r) => r.url.path == '/api/v1/events/audit'),
        isEmpty,
      );

      await tester.tap(find.text('Audit'));
      await tester.pumpAndSettle();
      expect(
        requests.where((r) => r.url.path == '/api/v1/events/audit'),
        isNotEmpty,
      );
    });

    testWidgets('delegated auditor reaches the audit stream (#3217)',
        (tester) async {
      // A principal whose only admin-sphere grant is `manage-events`
      // on /events: the same gate covers both subtabs — the audit
      // stream needs no extra wiring.
      testAuthHttpClientOverride = _mockClient(
        {
          '/events': ['manage-events'],
        },
        (request) async {
          if (request.url.path == '/api/v1/events/audit') {
            return http.Response(
              _auditEnvelope([
                _auditEvent(
                  1,
                  event: 'session.revoke',
                  actorId: 'u-3',
                  targetId: 'u-3',
                ),
              ], total: 1),
              200,
            );
          }
          if (request.url.path == '/api/v1/events/containers') {
            return http.Response(_auditEnvelope([]), 200);
          }
          return http.Response('Not found', 404);
        },
      );

      await pumpToAudit(tester);
      expect(find.text('session.revoke'), findsOneWidget);
      expect(find.text('Users'), findsNothing);
    });
  });
}
