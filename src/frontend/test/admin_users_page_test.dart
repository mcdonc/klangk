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

/// A paged envelope, matching the backend `GET /admin/groups` response.
String _groupsEnvelope(
  List<Map<String, dynamic>> groups, {
  int page = 1,
  int pageSize = 10,
  int total = 0,
}) =>
    jsonEncode({
      'groups': groups,
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

Map<String, dynamic> _group(String name,
        {String id = '', String description = ''}) =>
    {
      'id': id.isEmpty ? name : id,
      'name': name,
      'description': description,
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

  /// Empty paged invitations envelope, matching the backend
  /// `GET /admin/invitations` response shape.
  String emptyInvitationsEnvelope() => jsonEncode({
        'invitations': <Map<String, dynamic>>[],
        'page': 1,
        'page_size': 10,
        'total': 0,
        'pending_count': 0,
      });

  /// The Users toolbar is rendered alongside the (offstage) Invitations
  /// toolbar inside the IndexedStack, so a bare `find.text(...)` can match
  /// chips in both. Scope widgets to the users toolbar via its key.
  Finder inUsersToolbar(Finder inner) => find.descendant(
        of: find.byKey(const ValueKey('admin-users-toolbar')),
        matching: inner,
      );

  /// Likewise, scope widgets to the groups toolbar (the users/invitations
  /// toolbars are also built inside the IndexedStack).
  Finder inGroupsToolbar(Finder inner) => find.descendant(
        of: find.byKey(const ValueKey('admin-groups-toolbar')),
        matching: inner,
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
        return http.Response(emptyInvitationsEnvelope(), 200);
      }
      if (request.url.path == '/api/v1/admin/groups') {
        return http.Response(_groupsEnvelope([]), 200);
      }
      return http.Response('Not found', 404);
    });
  }

  /// Serves the admin/groups endpoint via [groupsFor] plus empty
  /// users/invitations so the page loads.
  void serveGroups(
    List<Map<String, dynamic>> Function(
            int page, int pageSize, String sort, String order, String? q)
        groupsFor, {
    int total = 25,
  }) {
    testAuthHttpClientOverride = _mockClient((request) async {
      if (request.url.path == '/api/v1/admin/users') {
        return http.Response(_usersEnvelope([]), 200);
      }
      if (request.url.path == '/api/v1/admin/invitations') {
        return http.Response(emptyInvitationsEnvelope(), 200);
      }
      if (request.url.path == '/api/v1/admin/groups') {
        final page = int.parse(request.url.queryParameters['page'] ?? '1');
        final pageSize =
            int.parse(request.url.queryParameters['page_size'] ?? '10');
        final sort = request.url.queryParameters['sort'] ?? 'name';
        final order = request.url.queryParameters['order'] ?? 'asc';
        final q = request.url.queryParameters['q'];
        return http.Response(
          _groupsEnvelope(groupsFor(page, pageSize, sort, order, q),
              page: page, pageSize: pageSize, total: total),
          200,
        );
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
          return http.Response(emptyInvitationsEnvelope(), 200);
        }
        if (request.url.path == '/api/v1/admin/groups') {
          return http.Response(_groupsEnvelope([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await pumpPage(tester);

      // Defaults: created, descending. The active Created chip shows ▼.
      // Scope to the users toolbar: the Invitations toolbar is also built
      // (offstage) inside the IndexedStack, so a bare find.text would match
      // chips in both.
      expect(capturedSort, 'created');
      expect(capturedOrder, 'desc');
      expect(inUsersToolbar(find.text('Created ▼')), findsOneWidget);

      // Tap the Email chip to switch sort (defaults to asc).
      await tester.tap(inUsersToolbar(find.text('Email')));
      await tester.pumpAndSettle();

      expect(capturedSort, 'email');
      expect(capturedOrder, 'asc');
      expect(inUsersToolbar(find.text('Email ▲')), findsOneWidget);

      // Tap Email again to flip direction to desc.
      await tester.tap(inUsersToolbar(find.text('Email ▲')));
      await tester.pumpAndSettle();

      expect(capturedOrder, 'desc');
      expect(inUsersToolbar(find.text('Email ▼')), findsOneWidget);
    });

    testWidgets('sends email filter query live (debounced)', (tester) async {
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
          return http.Response(emptyInvitationsEnvelope(), 200);
        }
        if (request.url.path == '/api/v1/admin/groups') {
          return http.Response(_groupsEnvelope([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await pumpPage(tester);

      expect(capturedQ, isNull);

      // The filter re-queries as the user types, debounced — settle past
      // the debounce timer. Scope to the users toolbar so we type into the
      // users filter, not the offstage invitations one.
      await tester.enterText(
        inUsersToolbar(find.byType(TextField)),
        'needle',
      );
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

  group('AdminUsersPage groups tab', () {
    /// Pump the page on the users tab, then switch to the Groups tab.
    Future<void> pumpGroupsTab(WidgetTester tester) async {
      await pumpPage(tester);
      await tester.tap(find.text('Groups'));
      await tester.pumpAndSettle();
    }

    testWidgets('renders groups from the paged envelope', (tester) async {
      serveGroups(
        (page, pageSize, sort, order, q) => [
          _group('admins', description: 'Admin team'),
          _group('editors', description: 'Editor team'),
        ],
        total: 2,
      );

      await pumpGroupsTab(tester);

      expect(find.text('admins', skipOffstage: false), findsOneWidget);
      expect(find.text('editors', skipOffstage: false), findsOneWidget);
    });

    testWidgets('shows pagination controls when more than one page',
        (tester) async {
      // 25 groups with page_size 10 => 3 pages.
      serveGroups((page, pageSize, sort, order, q) {
        final start = (page - 1) * pageSize;
        return [
          for (int i = start; i < start + pageSize && i < 25; i++)
            _group('group$i'),
        ];
      });

      await pumpGroupsTab(tester);

      expect(inGroupsToolbar(find.text('1 / 3')), findsOneWidget);
      // Prev disabled on page 1.
      expect(
          tester
              .widget<IconButton>(inGroupsToolbar(iconButton('Previous page')))
              .onPressed,
          isNull);
      // Next enabled.
      expect(
          tester
              .widget<IconButton>(inGroupsToolbar(iconButton('Next page')))
              .onPressed,
          isNotNull);
    });

    testWidgets('navigates to next and previous pages', (tester) async {
      var lastPageRequested = 1;
      serveGroups((page, pageSize, sort, order, q) {
        lastPageRequested = page;
        final start = (page - 1) * pageSize;
        return [
          for (int i = start; i < start + pageSize && i < 15; i++)
            _group('group$i'),
        ];
      }, total: 15);

      await pumpGroupsTab(tester);

      // 15 groups / 10 => 2 pages.
      expect(inGroupsToolbar(find.text('1 / 2')), findsOneWidget);

      await tester.tap(inGroupsToolbar(iconButton('Next page')));
      await tester.pumpAndSettle();

      expect(lastPageRequested, 2);
      expect(inGroupsToolbar(find.text('2 / 2')), findsOneWidget);
      expect(
          tester
              .widget<IconButton>(inGroupsToolbar(iconButton('Previous page')))
              .onPressed,
          isNotNull);

      await tester.tap(inGroupsToolbar(iconButton('Previous page')));
      await tester.pumpAndSettle();

      expect(lastPageRequested, 1);
      expect(inGroupsToolbar(find.text('1 / 2')), findsOneWidget);
    });

    testWidgets('sends sort and order params to the backend', (tester) async {
      String? capturedSort;
      String? capturedOrder;
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/admin/users') {
          return http.Response(_usersEnvelope([]), 200);
        }
        if (request.url.path == '/api/v1/admin/invitations') {
          return http.Response(emptyInvitationsEnvelope(), 200);
        }
        if (request.url.path == '/api/v1/admin/groups') {
          capturedSort = request.url.queryParameters['sort'];
          capturedOrder = request.url.queryParameters['order'];
          return http.Response(
            _groupsEnvelope([_group('g')], total: 1),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await pumpGroupsTab(tester);

      // Defaults: name, ascending. The active Name chip shows ▲. Scope to
      // the groups toolbar: the users/invitations toolbars are also built
      // (offstage) inside the IndexedStack.
      expect(capturedSort, 'name');
      expect(capturedOrder, 'asc');
      expect(inGroupsToolbar(find.text('Name ▲')), findsOneWidget);

      // Tap the Created chip to switch sort (defaults to desc for created).
      await tester.tap(inGroupsToolbar(find.text('Created')));
      await tester.pumpAndSettle();

      expect(capturedSort, 'created');
      expect(capturedOrder, 'desc');
      expect(inGroupsToolbar(find.text('Created ▼')), findsOneWidget);

      // Tap Created again to flip direction to asc.
      await tester.tap(inGroupsToolbar(find.text('Created ▼')));
      await tester.pumpAndSettle();

      expect(capturedOrder, 'asc');
      expect(inGroupsToolbar(find.text('Created ▲')), findsOneWidget);
    });

    testWidgets('sends name filter query live (debounced)', (tester) async {
      String? capturedQ;
      testAuthHttpClientOverride = _mockClient((request) async {
        if (request.url.path == '/api/v1/admin/users') {
          return http.Response(_usersEnvelope([]), 200);
        }
        if (request.url.path == '/api/v1/admin/invitations') {
          return http.Response(emptyInvitationsEnvelope(), 200);
        }
        if (request.url.path == '/api/v1/admin/groups') {
          capturedQ = request.url.queryParameters['q'];
          return http.Response(
            _groupsEnvelope([_group('needle-group')], total: 1),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await pumpGroupsTab(tester);

      expect(capturedQ, isNull);

      // The filter re-queries as the user types, debounced — settle past
      // the debounce timer. Scope to the groups toolbar so we type into the
      // groups filter, not the offstage users/invitations ones.
      await tester.enterText(
        inGroupsToolbar(find.byType(TextField)),
        'needle',
      );
      await tester.pumpAndSettle();

      expect(capturedQ, 'needle');
    });
  });
}
