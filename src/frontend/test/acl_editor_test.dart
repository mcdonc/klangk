import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/utils/system_agent.dart';
import 'package:klangk_frontend/widgets/acl_editor.dart';

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

/// ACE rows as served by `GET /api/v1/workspaces/{id}/acl`: one entry for
/// this workspace's own role group, one for a manual group (also present
/// in the manual groups list — exercises the dedupe), and one user entry.
List<Map<String, dynamic>> _entries() => [
      {
        'id': 1,
        'action': 1,
        'principal_type': 2,
        'permission': 'view',
        'group_id': 'g-role-owners',
        'principal': 'owners-abc-123',
      },
      {
        'id': 2,
        'action': 1,
        'principal_type': 2,
        'permission': 'edit',
        'group_id': 'g-manual-alpha',
        'principal': 'aa-manual-alpha',
      },
      {
        'id': 3,
        'action': 1,
        'principal_type': 1,
        'permission': '*',
        'user_id': 'u1',
        'principal': 'alice@example.com',
      },
    ];

Map<String, dynamic> _group(String id, String name) => {
      'id': id,
      'name': name,
      'description': '',
      'source': 'manual',
      'created_at': '2026-01-01T00:00:00',
    };

Map<String, dynamic> _user(String email) => {
      'id': email,
      'email': email,
      'handle': '',
      'verified': true,
      'provider': 'local',
      'created_at': '2026-01-01T00:00:00',
    };

/// The built-in system agent row, as `GET /admin/users` serves it.
Map<String, dynamic> _agentUser() => {
      'id': agentUserId,
      'email': 'klangk@example.com',
      'handle': 'klangk',
      'verified': true,
      'provider': 'system',
      'created_at': '2026-01-01T00:00:00',
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

  Widget buildEditor() {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
      ],
      child: const MaterialApp(
          home: Scaffold(body: AclEditor(resource: '/workspaces/ws1'))),
    );
  }

  testWidgets('picker offers manual ∪ referenced groups, all pages',
      (tester) async {
    final requestedGroupPages = <int>[];
    String? capturedSource;
    testAuthHttpClientOverride = MockClient((request) async {
      final path = request.url.path;
      if (path == '/api/v1/workspaces/ws1/acl') {
        return http.Response(jsonEncode(_entries()), 200);
      }
      if (path == '/api/v1/users/search') {
        return http.Response(jsonEncode([]), 200);
      }
      if (path == '/api/v1/groups') {
        final page = int.parse(request.url.queryParameters['page'] ?? '1');
        requestedGroupPages.add(page);
        capturedSource = request.url.queryParameters['source'];
        // 201 manual groups: a full first page (200) plus one straggler on
        // page 2 — the picker must walk both pages (#2752).
        final rows = page == 1
            ? [
                _group('g-manual-alpha', 'aa-manual-alpha'),
                for (var i = 0; i < 199; i++)
                  _group('g-filler-$i', 'zz-filler-$i'),
              ]
            : [_group('g-page2', 'aa-from-page-2')];
        return http.Response(
          jsonEncode({
            'groups': rows,
            'page': page,
            'page_size': 200,
            'total': 201,
          }),
          200,
        );
      }
      return http.Response('Not found', 404);
    });

    await tester.pumpWidget(buildEditor());
    await tester.pumpAndSettle();

    // The entries table is unchanged by design: this workspace's own role
    // group stays visible as an existing ACE (#2752).
    expect(find.text('owners-abc-123'), findsOneWidget);
    expect(find.text('aa-manual-alpha'), findsOneWidget);

    // Open the add-entry dialog.
    await tester.tap(find.widgetWithIcon(TextButton, Icons.add));
    await tester.pumpAndSettle();

    // Switch the principal type to Group (dropdown menu item).
    final principalField = find.byWidgetPredicate(
      (widget) =>
          widget is DropdownButtonFormField<int> &&
          widget.decoration.labelText == 'Principal Type',
    );
    await tester.tap(principalField);
    await tester.pumpAndSettle();
    await tester.tap(find.text('Group'));
    await tester.pumpAndSettle();

    // The picker asked for manual groups only, and walked every page.
    expect(capturedSource, 'manual');
    expect(requestedGroupPages, [1, 2]);

    // Open the group dropdown: manual groups from both pages are offered.
    final groupField = find.byWidgetPredicate(
      (widget) =>
          widget is DropdownButtonFormField<String> &&
          widget.decoration.labelText == 'Group',
    );
    await tester.tap(groupField);
    await tester.pumpAndSettle();

    // Page-2 straggler: proves all pages were loaded, not just the first.
    // (It appears only in the menu — nothing else carries this name.)
    expect(find.text('aa-from-page-2'), findsOneWidget);
    // Referenced-only group (this workspace's role group) is unioned in:
    // once in the untouched entries table, once in the picker menu.
    expect(find.text('owners-abc-123'), findsNWidgets(2));
    // A group that is both manual and referenced appears exactly once in
    // the menu (dedupe by id): table + menu = 2, not 3.
    expect(find.text('aa-manual-alpha'), findsNWidgets(2));
  });

  testWidgets('user principal is picked via /users/search (#2890)',
      (tester) async {
    testAuthHttpClientOverride = MockClient((request) async {
      final path = request.url.path;
      if (path == '/api/v1/workspaces/ws1/acl') {
        return http.Response(jsonEncode(_entries()), 200);
      }
      if (path == '/api/v1/users/search') {
        return http.Response(
          jsonEncode([_user('bob@example.com')]),
          200,
        );
      }
      if (path == '/api/v1/groups') {
        return http.Response(
          jsonEncode({'groups': [], 'page': 1, 'page_size': 200, 'total': 0}),
          200,
        );
      }
      return http.Response('Not found', 404);
    });

    await tester.pumpWidget(buildEditor());
    await tester.pumpAndSettle();

    // Open the add-entry dialog — the user principal is the default type.
    await tester.tap(find.widgetWithIcon(TextButton, Icons.add));
    await tester.pumpAndSettle();

    // Type a query; after the debounce the results list renders.
    await tester.enterText(find.byType(TextField), 'bo');
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();

    expect(find.text('bob@example.com'), findsOneWidget);

    // Add is inert until a result is picked (no user_id → no-op).
    await tester.tap(find.widgetWithText(FilledButton, 'Add'));
    await tester.pumpAndSettle();
    expect(find.text('Add ACE'), findsOneWidget);

    // Pick the result and add: the dialog closes and the entry is
    // appended to the entries table.
    await tester.tap(find.text('bob@example.com'));
    await tester.pumpAndSettle();

    // Typing a new query invalidates the pick (#2890 review): Add goes
    // inert again even though a user was picked before.
    await tester.enterText(find.byType(TextField), 'ca');
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Add'));
    await tester.pumpAndSettle();
    expect(find.text('Add ACE'), findsOneWidget);

    // Re-pick from the refreshed results and add for real.
    await tester.enterText(find.byType(TextField), 'bo');
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();
    await tester.tap(find.text('bob@example.com'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, 'Add'));
    await tester.pumpAndSettle();

    expect(find.text('Add ACE'), findsNothing);
    expect(find.text('bob@example.com'), findsOneWidget);
  });

  testWidgets('user search omits the system agent (#2892)', (tester) async {
    testAuthHttpClientOverride = MockClient((request) async {
      final path = request.url.path;
      if (path == '/api/v1/workspaces/ws1/acl') {
        return http.Response(jsonEncode(_entries()), 200);
      }
      if (path == '/api/v1/users/search') {
        // The agent matches the prefix too — the picker must filter it.
        return http.Response(
          jsonEncode([_agentUser(), _user('carol@example.com')]),
          200,
        );
      }
      if (path == '/api/v1/groups') {
        return http.Response(
          jsonEncode({'groups': [], 'page': 1, 'page_size': 200, 'total': 0}),
          200,
        );
      }
      return http.Response('Not found', 404);
    });

    await tester.pumpWidget(buildEditor());
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithIcon(TextButton, Icons.add));
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), 'kl');
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();

    // The agent realizes capabilities through physical access, never
    // principalship — the backend rejects an ACE for it, so the search
    // must not offer it.
    expect(find.text('klangk@example.com'), findsNothing);
    expect(find.text('carol@example.com'), findsOneWidget);
  });
}
