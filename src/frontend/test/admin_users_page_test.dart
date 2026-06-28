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

/// A paged envelope, matching the backend `GET /admin/users` response.
String _usersEnvelope(
  List<Map<String, dynamic>> users, {
  int page = 1,
  int pageSize = 10,
  int total = 0,
}) =>
    jsonEncode({
      'users': users,
      'page': page,
      'page_size': pageSize,
      'total': total,
    });

Map<String, dynamic> _user(String email, {String id = ''}) => {
      'id': id.isEmpty ? email : id,
      'email': email,
      'handle': '',
      'verified': true,
      'provider': 'local',
      'created_at': '2026-01-01T00:00:00',
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
  /// default 800px test surface) and settle.
  Future<void> pumpPage(WidgetTester tester) async {
    await tester.binding.setSurfaceSize(const Size(1280, 900));
    await tester.pumpWidget(buildPage());
    await tester.pumpAndSettle();
  }

  /// The IconButton whose tooltip matches [tooltip]. The tooltip is a
  /// descendant of the IconButton, so we look up the ancestor.
  Finder iconButton(String tooltip) => find.ancestor(
        of: find.byTooltip(tooltip),
        matching: find.byType(IconButton),
      );

  /// Serves the admin/users endpoint via [usersFor] plus empty
  /// invitations/groups so the page loads.
  void serveUsers(
    List<Map<String, dynamic>> Function(
            int page, int pageSize, String sort, String order, String? q)
        usersFor, {
    int total = 25,
  }) {
    testAuthHttpClientOverride = _mockClient((request) async {
      if (request.url.path == '/api/v1/admin/users') {
        final page = int.parse(request.url.queryParameters['page'] ?? '1');
        final pageSize =
            int.parse(request.url.queryParameters['page_size'] ?? '10');
        final sort = request.url.queryParameters['sort'] ?? 'created';
        final order = request.url.queryParameters['order'] ?? 'desc';
        final q = request.url.queryParameters['q'];
        return http.Response(
          _usersEnvelope(usersFor(page, pageSize, sort, order, q),
              page: page, pageSize: pageSize, total: total),
          200,
        );
      }
      if (request.url.path == '/api/v1/admin/invitations') {
        return http.Response(jsonEncode([]), 200);
      }
      if (request.url.path == '/api/v1/admin/groups') {
        return http.Response(jsonEncode([]), 200);
      }
      return http.Response('Not found', 404);
    });
  }

  group('AdminUsersPage', () {
    testWidgets('renders users from the paged envelope', (tester) async {
      serveUsers(
        (page, pageSize, sort, order, q) => [
          _user('alice@example.com'),
          _user('bob@example.com'),
        ],
        total: 2,
      );

      await pumpPage(tester);

      expect(
          find.text('alice@example.com', skipOffstage: false), findsOneWidget);
      expect(find.text('bob@example.com', skipOffstage: false), findsOneWidget);
      // No per-card groups subtitle anymore.
      expect(find.textContaining('Groups:'), findsNothing);
    });

    testWidgets('shows pagination controls when more than one page',
        (tester) async {
      // 25 users with page_size 10 => 3 pages.
      serveUsers((page, pageSize, sort, order, q) {
        final start = (page - 1) * pageSize;
        return [
          for (int i = start; i < start + pageSize && i < 25; i++)
            _user('user$i@example.com'),
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
      serveUsers((page, pageSize, sort, order, q) {
        lastPageRequested = page;
        final start = (page - 1) * pageSize;
        return [
          for (int i = start; i < start + pageSize && i < 15; i++)
            _user('user$i@example.com'),
        ];
      }, total: 15);

      await pumpPage(tester);

      // 15 users / 10 => 2 pages.
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
        if (request.url.path == '/api/v1/admin/users') {
          capturedSort = request.url.queryParameters['sort'];
          capturedOrder = request.url.queryParameters['order'];
          return http.Response(
            _usersEnvelope([_user('a@example.com')], total: 1),
            200,
          );
        }
        if (request.url.path == '/api/v1/admin/invitations') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/api/v1/admin/groups') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await pumpPage(tester);

      // Defaults: created, descending. The active Created chip shows ▼.
      expect(capturedSort, 'created');
      expect(capturedOrder, 'desc');
      expect(find.text('Created ▼', skipOffstage: false), findsOneWidget);

      // Tap the Email chip to switch sort (defaults to asc).
      await tester.tap(find.text('Email'));
      await tester.pumpAndSettle();

      expect(capturedSort, 'email');
      expect(capturedOrder, 'asc');
      expect(find.text('Email ▲', skipOffstage: false), findsOneWidget);

      // Tap Email again to flip direction to desc.
      await tester.tap(find.text('Email ▲'));
      await tester.pumpAndSettle();

      expect(capturedOrder, 'desc');
      expect(find.text('Email ▼', skipOffstage: false), findsOneWidget);
    });

    testWidgets('sends email filter query on submit', (tester) async {
      String? capturedQ;
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/admin/users') {
          capturedQ = request.url.queryParameters['q'];
          return http.Response(
            _usersEnvelope([_user('needle@example.com')], total: 1),
            200,
          );
        }
        if (request.url.path == '/api/v1/admin/invitations') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/api/v1/admin/groups') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await pumpPage(tester);

      expect(capturedQ, isNull);

      await tester.enterText(find.byType(TextField), 'needle');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(capturedQ, 'needle');
    });

    testWidgets('omits groups from user cards', (tester) async {
      serveUsers(
        (page, pageSize, sort, order, q) => [_user('alice@example.com')],
        total: 1,
      );

      await pumpPage(tester);

      expect(find.textContaining('Groups:'), findsNothing);
    });
  });
}
