import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/admin/admin_users_page.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// A paged invitations envelope, matching the backend
/// `GET /admin/invitations` response.
String _invitationsEnvelope(
  List<Map<String, dynamic>> invitations, {
  int page = 1,
  int pageSize = 10,
  int total = 0,
  int pendingCount = 0,
}) =>
    jsonEncode({
      'invitations': invitations,
      'page': page,
      'page_size': pageSize,
      'total': total,
      'pending_count': pendingCount,
    });

Map<String, dynamic> _invitation(
  String email, {
  String id = '',
  String status = 'pending',
  String invitedBy = 'admin@example.com',
  String createdAt = '2026-01-01 00:00:00',
}) =>
    {
      'id': id.isEmpty ? email : id,
      'email': email,
      'invited_by': 'admin-user',
      'invited_by_email': invitedBy,
      'status': status,
      'created_at': createdAt,
      'accepted_at': null,
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

/// Build a mock client that serves config + admin permissions + a custom
/// handler for everything else.
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
          'permissions': {
            '/admin': ['*'],
            '/admin/users': ['view'],
            '/admin/groups': ['view'],
            '/admin/invitations': ['view'],
          },
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
      ],
      child: const MaterialApp(home: AdminUsersPage()),
    );
  }

  /// Pump the page on a wide surface (the admin tab row overflows on the
  /// default 800px test surface) and settle. Optionally navigates to the
  /// Invitations tab before settling.
  Future<void> pumpPage(WidgetTester tester,
      {bool toInvitations = true}) async {
    await tester.binding.setSurfaceSize(const Size(1280, 900));
    await tester.pumpWidget(buildPage());
    await tester.pumpAndSettle();
    if (toInvitations) {
      // The Invitations tab is the 3rd visible tab (Users, Groups,
      // Invitations). Tap its label to switch.
      await tester.tap(find.text('Invitations'));
      await tester.pumpAndSettle();
    }
  }

  /// The IconButton whose tooltip matches [tooltip]. The tooltip is a
  /// descendant of the IconButton, so we look up the ancestor.
  Finder iconButton(String tooltip) => find.ancestor(
        of: find.byTooltip(tooltip),
        matching: find.byType(IconButton),
      );

  /// The Invitations toolbar is rendered alongside the (offstage) Users
  /// toolbar inside the IndexedStack, so a plain `find.text(...)` can match
  /// chips in both. Scope widgets to the invitations toolbar via its key.
  Finder inInvitationsToolbar(Finder inner) => find.descendant(
        of: find.byKey(const ValueKey('admin-invitations-toolbar')),
        matching: inner,
      );

  /// Serves the admin/invitations endpoint via [invitationsFor] plus empty
  /// users/groups so the page loads. The prev/next pagination IconButtons
  /// are distinguished from the per-card Resend/Revoke IconButtons by their
  /// tooltips, so serveInvitations and the default pending total must keep
  /// the page count > 1 for those controls to be present.
  void serveInvitations(
    List<Map<String, dynamic>> Function(
            int page, int pageSize, String sort, String order, String? q)
        invitationsFor, {
    int total = 25,
    int pendingCount = 0,
  }) {
    testAuthHttpClientOverride = _mockClient((request) async {
      if (request.url.path == '/api/v1/admin/invitations') {
        final page = int.parse(request.url.queryParameters['page'] ?? '1');
        final pageSize =
            int.parse(request.url.queryParameters['page_size'] ?? '10');
        final sort = request.url.queryParameters['sort'] ?? 'created';
        final order = request.url.queryParameters['order'] ?? 'desc';
        final q = request.url.queryParameters['q'];
        return http.Response(
          _invitationsEnvelope(
            invitationsFor(page, pageSize, sort, order, q),
            page: page,
            pageSize: pageSize,
            total: total,
            pendingCount: pendingCount,
          ),
          200,
        );
      }
      if (request.url.path == '/api/v1/admin/users') {
        // Paged users envelope expected by the page.
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
      if (request.url.path == '/api/v1/admin/groups') {
        return http.Response(jsonEncode([]), 200);
      }
      return http.Response('Not found', 404);
    });
  }

  group('AdminUsersPage invitations', () {
    testWidgets('renders invitations from the paged envelope', (tester) async {
      serveInvitations(
        (page, pageSize, sort, order, q) => [
          _invitation('alice@example.com'),
          _invitation('bob@example.com'),
        ],
        total: 2,
        pendingCount: 2,
      );

      await pumpPage(tester);

      expect(
          find.text('alice@example.com', skipOffstage: false), findsOneWidget);
      expect(find.text('bob@example.com', skipOffstage: false), findsOneWidget);
    });

    testWidgets('shows pagination controls when more than one page',
        (tester) async {
      // 25 invitations with page_size 10 => 3 pages.
      serveInvitations((page, pageSize, sort, order, q) {
        final start = (page - 1) * pageSize;
        return [
          for (int i = start; i < start + pageSize && i < 25; i++)
            _invitation('inv$i@example.com'),
        ];
      });

      await pumpPage(tester);

      expect(find.text('1 / 3', skipOffstage: false), findsOneWidget);
      // Prev disabled on page 1.
      expect(tester.widget<IconButton>(iconButton('Previous page')).onPressed,
          isNull);
      // Next enabled.
      expect(tester.widget<IconButton>(iconButton('Next page')).onPressed,
          isNotNull);
    });

    testWidgets('navigates to next and previous pages', (tester) async {
      var lastPageRequested = 1;
      serveInvitations((page, pageSize, sort, order, q) {
        lastPageRequested = page;
        final start = (page - 1) * pageSize;
        return [
          for (int i = start; i < start + pageSize && i < 15; i++)
            _invitation('inv$i@example.com'),
        ];
      }, total: 15);

      await pumpPage(tester);

      // 15 invitations / 10 => 2 pages.
      expect(find.text('1 / 2', skipOffstage: false), findsOneWidget);

      await tester.tap(iconButton('Next page'));
      await tester.pumpAndSettle();

      expect(lastPageRequested, 2);
      expect(find.text('2 / 2', skipOffstage: false), findsOneWidget);
      expect(tester.widget<IconButton>(iconButton('Previous page')).onPressed,
          isNotNull);

      await tester.tap(iconButton('Previous page'));
      await tester.pumpAndSettle();

      expect(lastPageRequested, 1);
      expect(find.text('1 / 2', skipOffstage: false), findsOneWidget);
    });

    testWidgets('sends sort and order params to the backend', (tester) async {
      String? capturedSort;
      String? capturedOrder;
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/admin/invitations') {
          capturedSort = request.url.queryParameters['sort'];
          capturedOrder = request.url.queryParameters['order'];
          final page = int.parse(request.url.queryParameters['page'] ?? '1');
          return http.Response(
            _invitationsEnvelope(
              [_invitation('a@example.com')],
              page: page,
              total: 1,
            ),
            200,
          );
        }
        if (request.url.path == '/api/v1/admin/users') {
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
        if (request.url.path == '/api/v1/admin/groups') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await pumpPage(tester);

      // Defaults: created, descending. The active Created chip shows ▼.
      // Scope to the invitations toolbar: the Users toolbar is also built
      // (offstage) inside the IndexedStack, so a bare find.text would match
      // chips in both.
      expect(capturedSort, 'created');
      expect(capturedOrder, 'desc');
      expect(inInvitationsToolbar(find.text('Created ▼')), findsOneWidget);

      // Tap the Email chip to switch sort (defaults to asc).
      await tester.tap(inInvitationsToolbar(find.text('Email')));
      await tester.pumpAndSettle();

      expect(capturedSort, 'email');
      expect(capturedOrder, 'asc');
      expect(inInvitationsToolbar(find.text('Email ▲')), findsOneWidget);

      // Tap Email again to flip direction to desc.
      await tester.tap(inInvitationsToolbar(find.text('Email ▲')));
      await tester.pumpAndSettle();

      expect(capturedOrder, 'desc');
      expect(inInvitationsToolbar(find.text('Email ▼')), findsOneWidget);
    });

    testWidgets('sends email filter query live (debounced)', (tester) async {
      String? capturedQ;
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/admin/invitations') {
          capturedQ = request.url.queryParameters['q'];
          final page = int.parse(request.url.queryParameters['page'] ?? '1');
          return http.Response(
            _invitationsEnvelope(
              [_invitation('needle@example.com')],
              page: page,
              total: 1,
            ),
            200,
          );
        }
        if (request.url.path == '/api/v1/admin/users') {
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
        if (request.url.path == '/api/v1/admin/groups') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await pumpPage(tester);

      expect(capturedQ, isNull);

      // The filter re-queries as the user types, debounced — settle past
      // the debounce timer.
      await tester.enterText(
        inInvitationsToolbar(find.byType(TextField)),
        'needle',
      );
      await tester.pumpAndSettle();

      expect(capturedQ, 'needle');
    });
  });
}
