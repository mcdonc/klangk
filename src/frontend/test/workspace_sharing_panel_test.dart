import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/utils/system_agent.dart';
import 'package:klangk_frontend/workspace/workspace_sharing_panel.dart';

/// Default JWT for a logged-in user.
String get _token {
  final header = base64Url
      .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
      .replaceAll('=', '');
  final body = base64Url
      .encode(utf8.encode(jsonEncode({
        'sub': 'user-1',
        'email': 'user@example.com',
      })))
      .replaceAll('=', '');
  return '$header.$body.fakesig';
}

/// Role payload as served by `GET /api/v1/workspaces/{id}/roles`:
/// deliberately scrambled server-side so the sort order is asserted, with
/// an empty role (spectators), and an unknown suffix (auditors) that
/// exercises the icon/color fallbacks and the missing-`permissions`
/// graceful path. Owners carry the wildcard expansion the backend emits
/// for a `*` grant (collapsed to 'All permissions' in the UI).
List<Map<String, dynamic>> _roles() => [
      {
        'role': 'spectators',
        'group_id': 'g4',
        'group_name': 'spectators-ws1',
        'members': [],
        'permissions': [
          'monitor-workspace',
          'terminal',
          'spectate-on-shared-terminals',
        ],
      },
      {
        'role': 'coders',
        'group_id': 'g3',
        'group_name': 'coders-ws1',
        'members': [
          {'id': 'u-bob', 'email': 'bob@example.com'},
        ],
        'permissions': [
          'monitor-workspace',
          'terminal',
          'egress-consent',
          'code-in-isolation',
          'files-view',
        ],
      },
      {
        'role': 'auditors',
        'group_id': 'g5',
        'group_name': 'auditors-ws1',
        'members': [
          {'id': 'u-dan', 'email': 'dan@example.com'},
        ],
      },
      {
        'role': 'owners',
        'group_id': 'g1',
        'group_name': 'owners-ws1',
        'members': [
          {'id': 'u-alice', 'email': 'alice@example.com'},
        ],
        'permissions': ['*', 'view', 'terminal'],
      },
      {
        'role': 'collaborators',
        'group_id': 'g2',
        'group_name': 'collaborators-ws1',
        'members': [
          {'id': 'u-carol', 'email': 'carol@example.com'},
        ],
        'permissions': [
          'monitor-workspace',
          'terminal',
          'egress-consent',
          'share-terminals',
        ],
      },
    ];

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({'klangk_jwt': _token});
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  /// Route table for the panel's endpoints. [addStatus]/[addBody] shape
  /// the POST /roles/{role} response; [searchError] makes the user
  /// search throw (the transport-failure path).
  void stubHttp({
    int addStatus = 200,
    String addBody = '{"ok": true}',
    bool searchError = false,
    List<Map<String, dynamic>> searchResults = const [],
    List<String>? requests,
  }) {
    testAuthHttpClientOverride = MockClient((request) async {
      requests?.add('${request.method} ${request.url.path}');
      final path = request.url.path;
      if (path == '/api/v1/workspaces/ws1/roles') {
        return http.Response(jsonEncode(_roles()), 200);
      }
      if (path.startsWith('/api/v1/workspaces/ws1/roles/')) {
        if (request.method == 'POST') {
          return http.Response(addBody, addStatus);
        }
        return http.Response('{"ok": true}', 200);
      }
      if (path == '/api/v1/users/search') {
        if (searchError) {
          throw Exception('search transport failed');
        }
        return http.Response(jsonEncode(searchResults), 200);
      }
      if (path == '/api/v1/workspaces/ws1/acl') {
        return http.Response(
          jsonEncode([
            {
              'id': 1,
              'action': 1,
              'principal_type': 1,
              'permission': '*',
              'user_id': 'u1',
              'principal': 'user@example.com',
            },
          ]),
          200,
        );
      }
      return http.Response('Not found', 404);
    });
  }

  Widget buildPanel({required bool canEditAcl, bool canShare = true}) {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
      ],
      child: MaterialApp(
        home: Scaffold(
          body: WorkspaceSharingPanel(
            workspaceId: 'ws1',
            canShare: canShare,
            canEditAcl: canEditAcl,
          ),
        ),
      ),
    );
  }

  testWidgets('renders buckets sorted, with chips and empty states',
      (tester) async {
    stubHttp();
    await tester.pumpWidget(buildPanel(canEditAcl: false));
    await tester.pumpAndSettle();

    // Buckets render in the canonical role order (owners first, unknown
    // suffixes last), not in the scrambled server order.
    final ownersY = tester.getTopLeft(find.text('Owners')).dy;
    final collaboratorsY = tester.getTopLeft(find.text('Collaborators')).dy;
    final codersY = tester.getTopLeft(find.text('Coders')).dy;
    final spectatorsY = tester.getTopLeft(find.text('Spectators')).dy;
    final auditorsY = tester.getTopLeft(find.text('Auditors')).dy;
    expect(ownersY < collaboratorsY, isTrue);
    expect(collaboratorsY < codersY, isTrue);
    expect(codersY < spectatorsY, isTrue);
    expect(spectatorsY < auditorsY, isTrue);

    // Members render as chips; the empty role shows its empty state.
    expect(find.text('alice@example.com'), findsOneWidget);
    expect(find.text('No members'), findsOneWidget);

    // Permission lists render per bucket (#2986): the owners' wildcard
    // collapses to a label (the expanded names never render), granted
    // permissions render once per bucket that holds them, and a role
    // without a `permissions` key (auditors) renders no chips.
    expect(find.text('All permissions'), findsOneWidget);
    expect(find.text('terminal'), findsNWidgets(3));
    expect(find.text('egress-consent'), findsNWidgets(2));
    expect(find.text('share-terminals'), findsOneWidget);
    expect(find.text('view'), findsNothing);

    // Permission names and the collapsed 'All permissions' label share
    // one quiet tag style: outlined pill, no background fill (#2987).
    BoxDecoration tagDecoration(String label) => tester
        .widget<Container>(
          find
              .ancestor(
                of: find.text(label),
                matching: find.byType(Container),
              )
              .first,
        )
        .decoration! as BoxDecoration;
    final permDeco = tagDecoration('egress-consent');
    final allDeco = tagDecoration('All permissions');
    for (final deco in [permDeco, allDeco]) {
      expect(deco.color, isNull);
      expect(deco.border, isNotNull);
      expect(deco.borderRadius, isNotNull);
    }
    expect(
      (allDeco.borderRadius as BorderRadius).bottomLeft,
      (permDeco.borderRadius as BorderRadius).bottomLeft,
    );

    // Without change-acls (#2764) the role-write affordances are hidden:
    // no add-user buttons, chips carry no delete icons.
    expect(find.byTooltip('Add user'), findsNothing);
    expect(
      find.descendant(
          of: find.byType(Chip), matching: find.byIcon(Icons.close)),
      findsNothing,
    );
  });

  testWidgets('panel spans three quarters of the screen width (#2986)',
      (tester) async {
    tester.view.physicalSize = const Size(1600, 900);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.reset);
    stubHttp();
    await tester.pumpWidget(buildPanel(canEditAcl: false));
    await tester.pumpAndSettle();

    final constraint = tester.widget<ConstrainedBox>(
      find
          .ancestor(
            of: find.text('Owners'),
            matching: find.byType(ConstrainedBox),
          )
          .first,
    );
    expect(constraint.constraints.maxWidth, 1200);
  });

  testWidgets('role-write affordances appear with change-acls (#2764)',
      (tester) async {
    stubHttp();
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();
    expect(find.byTooltip('Add user'), findsNWidgets(5));
    expect(
      find.descendant(
          of: find.byType(Chip), matching: find.byIcon(Icons.close)),
      findsNWidgets(4),
    );
  });

  testWidgets('change-acls-only holder: no buckets, no roles fetch (#2764)',
      (tester) async {
    final requests = <String>[];
    stubHttp(requests: requests);
    await tester.pumpWidget(buildPanel(canEditAcl: true, canShare: false));
    await tester.pumpAndSettle();

    // No share-gated roles request fires; no bucket UI renders.
    expect(
      requests.where((r) => r.endsWith('/workspaces/ws1/roles')),
      isEmpty,
    );
    expect(find.text('Owners'), findsNothing);
    expect(find.byType(CircularProgressIndicator), findsNothing);
    // The advanced editor remains available.
    expect(find.text('Advanced: Access Control'), findsOneWidget);
  });

  testWidgets('hides the advanced ACL editor without change-acls (#2764)',
      (tester) async {
    stubHttp();
    await tester.pumpWidget(buildPanel(canEditAcl: false));
    await tester.pumpAndSettle();
    expect(find.text('Advanced: Access Control'), findsNothing);
  });

  testWidgets('offers the advanced ACL editor with change-acls (#2764)',
      (tester) async {
    stubHttp();
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();
    expect(find.text('Advanced: Access Control'), findsOneWidget);

    // Expanding mounts the raw ACE editor (its header row loads the
    // stubbed entry).
    await tester.ensureVisible(find.text('Advanced: Access Control'));
    await tester.tap(find.text('Advanced: Access Control'));
    await tester.pumpAndSettle();
    expect(find.text('Access Control Entries'), findsOneWidget);
    expect(find.text('user@example.com'), findsOneWidget);
  });

  testWidgets('empty roles payload renders no buckets', (tester) async {
    testAuthHttpClientOverride = MockClient((request) async {
      if (request.url.path == '/api/v1/workspaces/ws1/roles') {
        return http.Response(jsonEncode([]), 200);
      }
      return http.Response('Not found', 404);
    });
    await tester.pumpWidget(buildPanel(canEditAcl: false));
    await tester.pumpAndSettle();
    expect(find.text('Owners'), findsNothing);
  });

  testWidgets('role load failure leaves the panel empty, not stuck',
      (tester) async {
    testAuthHttpClientOverride = MockClient((request) async {
      if (request.url.path == '/api/v1/workspaces/ws1/roles') {
        return http.Response('Forbidden', 403);
      }
      return http.Response('Not found', 404);
    });
    await tester.pumpWidget(buildPanel(canEditAcl: false));
    await tester.pumpAndSettle();
    expect(find.byType(CircularProgressIndicator), findsNothing);
    expect(find.text('Owners'), findsNothing);
  });

  testWidgets('add dialog searches, and tapping a result adds the member',
      (tester) async {
    final requests = <String>[];
    stubHttp(
      requests: requests,
      searchResults: [
        {'id': 'u-new', 'email': 'newuser@example.com'},
      ],
    );
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();

    // Owners bucket is first: open its add dialog.
    await tester.tap(find.byTooltip('Add user').first);
    await tester.pumpAndSettle();
    expect(find.text('Add to owners'), findsOneWidget);

    // Typing kicks the debounced search; results appear after 300ms.
    await tester.enterText(find.byType(TextField), 'newuser');
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();
    expect(find.text('newuser@example.com'), findsOneWidget);

    // Tapping the result closes the dialog and POSTs the role change.
    await tester.tap(find.text('newuser@example.com'));
    await tester.pumpAndSettle();
    expect(find.text('Add to owners'), findsNothing);
    expect(requests, contains('POST /api/v1/workspaces/ws1/roles/owners'));
  });

  testWidgets('add dialog autocomplete omits the system agent (#2892)',
      (tester) async {
    stubHttp(
      searchResults: [
        {'id': agentUserId, 'email': 'klangk@example.com'},
        {'id': 'u-new', 'email': 'newuser@example.com'},
      ],
    );
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Add user').first);
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'k');
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();

    // The agent can never hold a role, so the backend would only reject
    // it — the autocomplete filters it out and offers the human hit only.
    expect(find.text('klangk@example.com'), findsNothing);
    expect(find.text('newuser@example.com'), findsOneWidget);
  });

  testWidgets('add dialog submits a typed email directly', (tester) async {
    final requests = <String>[];
    stubHttp(requests: requests);
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Add user').first);
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'typed@example.com');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();

    expect(find.text('Add to owners'), findsNothing);
    expect(requests, contains('POST /api/v1/workspaces/ws1/roles/owners'));
  });

  testWidgets('add dialog cancel closes without posting', (tester) async {
    final requests = <String>[];
    stubHttp(requests: requests);
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Add user').first);
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'never@example.com');
    await tester.tap(find.text('Cancel'));
    await tester.pumpAndSettle();

    expect(find.text('Add to owners'), findsNothing);
    expect(
      requests.where((r) => r.startsWith('POST')),
      isEmpty,
    );
  });

  testWidgets('clearing the query clears the results', (tester) async {
    stubHttp(
      searchResults: [
        {'id': 'u-new', 'email': 'newuser@example.com'},
      ],
    );
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Add user').first);
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'newuser');
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();
    expect(find.text('newuser@example.com'), findsOneWidget);

    // Whitespace-only input trims to empty: results reset, no new
    // debounce timer is armed.
    await tester.enterText(find.byType(TextField), '   ');
    await tester.pumpAndSettle();
    expect(find.text('newuser@example.com'), findsNothing);
  });

  testWidgets('search transport failure is swallowed', (tester) async {
    stubHttp(searchError: true);
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Add user').first);
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'newuser');
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();

    // No results, dialog still usable.
    expect(find.text('Add to owners'), findsOneWidget);
    expect(find.byType(ListTile), findsNothing);
  });

  testWidgets('add failure surfaces the server detail in a snackbar',
      (tester) async {
    stubHttp(addStatus: 404, addBody: '{"detail": "User not found"}');
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Add user').first);
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'missing@example.com');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();

    expect(find.text('User not found'), findsOneWidget);
  });

  testWidgets('add failure with an unparseable body shows a generic error',
      (tester) async {
    stubHttp(addStatus: 500, addBody: 'Internal Server Error');
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();

    await tester.tap(find.byTooltip('Add user').first);
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'anyone@example.com');
    await tester.testTextInput.receiveAction(TextInputAction.done);
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();

    expect(find.text('Error'), findsOneWidget);
  });

  testWidgets('deleting a chip removes the member from the role',
      (tester) async {
    final requests = <String>[];
    stubHttp(requests: requests);
    await tester.pumpWidget(buildPanel(canEditAcl: true));
    await tester.pumpAndSettle();

    final aliceChip = find.ancestor(
      of: find.text('alice@example.com'),
      matching: find.byType(Chip),
    );
    await tester.tap(find.descendant(
      of: aliceChip,
      matching: find.byIcon(Icons.close),
    ));
    await tester.pumpAndSettle();

    expect(
      requests,
      contains('DELETE /api/v1/workspaces/ws1/roles/owners/u-alice'),
    );
  });
}
