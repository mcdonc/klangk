import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/workspace/workspace_list_page.dart';
import 'package:klangk_frontend/workspace/import_workspace_dialog.dart';
import 'package:klangk_frontend/theme/colors.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_frontend/widgets/klangk_logo.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Wrap a workspace list in the pagination envelope returned by the API.
dynamic _envelope(dynamic items) => {
      'items': items,
      'has_more': false,
      'next_offset': null,
    };

/// A WsClient whose workspacesChanged stream can be driven from tests.
class _MockWsClient extends WsClient {
  final StreamController<void> _workspacesChanged =
      StreamController<void>.broadcast();
  final StreamController<Map<String, dynamic>> _containerStatus =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<Map<String, dynamic>> _serviceHealth =
      StreamController<Map<String, dynamic>>.broadcast();

  @override
  Stream<void> get workspacesChanged => _workspacesChanged.stream;

  @override
  Stream<Map<String, dynamic>> get containerStatus => _containerStatus.stream;

  @override
  Stream<Map<String, dynamic>> get serviceHealth => _serviceHealth.stream;

  void emitWorkspacesChanged() => _workspacesChanged.add(null);

  void emitContainerStatus(String workspaceId, bool running) =>
      _containerStatus.add({
        'type': 'container_status',
        'workspace_id': workspaceId,
        'running': running,
      });

  void emitServiceHealth(String workspaceId, bool healthy) =>
      _serviceHealth.add({
        'type': 'service_health',
        'workspace_id': workspaceId,
        'healthy': healthy,
      });

  @override
  void dispose() {
    _workspacesChanged.close();
    _containerStatus.close();
    _serviceHealth.close();
    super.dispose();
  }
}

void main() {
  /// Wraps a handler to also serve /api/config and /api/my-permissions.
  http.Client withPermissions(
    Future<http.Response> Function(http.Request) handler, {
    Map<String, List<String>>? permissions,
    List<Map<String, dynamic>>? groups,
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
            'user_id': 'test',
            'email': 'test@example.com',
            'permissions': permissions ??
                {
                  '/': ['view'],
                  '/workspaces': ['create'],
                },
            'groups': groups ?? [],
          }),
          200,
        );
      }
      return handler(request);
    });
  }

  /// Default mock that serves workspaces, config, and permissions.
  http.Client defaultMockClient() {
    return withPermissions((request) async {
      if (request.url.path == '/api/v1/workspaces') {
        return http.Response(jsonEncode(_envelope([])), 200);
      }
      if (request.url.path == '/api/v1/workspaces/shared') {
        return http.Response(jsonEncode(_envelope([])), 200);
      }
      return http.Response('Not found', 404);
    });
  }

  /// Default JWT for tests that need a logged-in user.
  late String defaultToken;

  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    // Most tests need a logged-in user with workspace create permission.
    defaultToken = base64Url
            .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
            .replaceAll('=', '') +
        '.' +
        base64Url
            .encode(utf8.encode(jsonEncode({
              'sub': 'test-user',
              'email': 'test@example.com',
            })))
            .replaceAll('=', '') +
        '.fakesig';
    SharedPreferences.setMockInitialValues({'klangk_jwt': defaultToken});
    testAuthHttpClientOverride = defaultMockClient();
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
    testPickFileBytesOverride = null;
  });

  String makeJwt(Map<String, dynamic> payload) {
    final header = base64Url
        .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
        .replaceAll('=', '');
    final body =
        base64Url.encode(utf8.encode(jsonEncode(payload))).replaceAll('=', '');
    return '$header.$body.fakesig';
  }

  Widget buildPage({WsClient? wsClient}) {
    return MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProvider.value(value: wsClient ?? WsClient()),
      ],
      child: const MaterialApp(home: WorkspaceListPage()),
    );
  }

  group('WorkspaceListPage', () {
    testWidgets('renders page with title', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byType(WorkspaceListPage), findsOneWidget);
      expect(find.text('Workspaces'), findsOneWidget);
    });

    testWidgets('refreshes workspace list on workspacesChanged event',
        (tester) async {
      final ws = _MockWsClient();
      var fetchCount = 0;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          fetchCount++;
          // Second fetch (after the WS event) surfaces a new workspace.
          final list = fetchCount >= 2
              ? [
                  {'id': 'ws-1', 'name': 'appeared', 'created_at': ''}
                ]
              : [];
          return http.Response(jsonEncode(_envelope(list)), 200);
        }
        if (request.url.path == '/api/v1/workspaces/ws-1/members') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage(wsClient: ws));
      await tester.pumpAndSettle();
      expect(fetchCount, 1);
      expect(find.text('appeared'), findsNothing);

      // The backend signals the workspace set changed.
      ws.emitWorkspacesChanged();
      await tester.pumpAndSettle();

      expect(fetchCount, greaterThan(1));
      expect(find.text('appeared'), findsOneWidget);

      ws.dispose();
    });

    testWidgets('has FAB for creating workspaces', (tester) async {
      final token = makeJwt({'sub': 'u1', 'email': 'u@example.com'});
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      testAuthHttpClientOverride = defaultMockClient();
      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.byTooltip('New Workspace'), findsOneWidget);
      expect(find.byTooltip('Import Workspace'), findsOneWidget);
    });

    testWidgets('no FAB when user lacks create permission', (tester) async {
      testAuthHttpClientOverride = withPermissions(
        (request) async {
          if (request.url.path == '/api/v1/workspaces') {
            return http.Response(jsonEncode(_envelope([])), 200);
          }
          if (request.url.path == '/api/v1/workspaces/shared') {
            return http.Response(jsonEncode(_envelope([])), 200);
          }
          return http.Response('Not found', 404);
        },
        permissions: {
          '/': ['view']
        },
      );
      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.byTooltip('New Workspace'), findsNothing);
    });

    testWidgets('has logout button', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byIcon(Icons.logout), findsOneWidget);
    });

    testWidgets('shows klangk logo', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byType(KlangkLogo), findsOneWidget);
      expect(find.byIcon(Icons.smart_toy_outlined), findsOneWidget);
    });

    testWidgets('shows workspace list from mock', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'Project A',
                'container_id': null,
                'created_at': '2026-01-15 14:30:00'
              },
              {
                'id': 'ws-2',
                'name': 'Project B',
                'container_id': null,
                'created_at': '2026-06-02 09:00:00'
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.text('Project A'), findsOneWidget);
      expect(find.text('Project B'), findsOneWidget);
      expect(find.byIcon(Icons.terminal), findsNWidgets(2));
      // Dates formatted as local time (VM tests run in UTC)
      expect(find.textContaining('Jan 15, 2026'), findsOneWidget);
      expect(find.textContaining('Jun 2, 2026'), findsOneWidget);
    });

    testWidgets('shows member avatars on workspace cards', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'Shared Project',
                'container_id': null,
                'created_at': '2026-01-15 14:30:00',
              },
            ])),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/ws-1/members') {
          return http.Response(
            jsonEncode([
              {'id': 'uid-2', 'email': 'alice@example.com'},
              {'id': 'uid-3', 'email': 'bob@example.com'},
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.text('Shared Project'), findsOneWidget);
      // Two CircleAvatar widgets for the two members
      expect(find.byType(CircleAvatar), findsNWidgets(2));
      // First letters of email addresses
      expect(find.text('A'), findsOneWidget);
      expect(find.text('B'), findsOneWidget);
    });

    testWidgets('shows shared workspaces section', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'My Project',
                'container_id': null,
                'created_at': '2026-01-15 14:30:00',
              },
            ])),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/shared') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-shared-1',
                'name': 'Team Project',
                'container_id': null,
                'created_at': '2026-02-01 10:00:00',
                'owner_email': 'alice@example.com',
              },
              {
                'id': 'ws-shared-2',
                'name': 'Other Project',
                'container_id': null,
                'created_at': '2026-03-01 10:00:00',
                'owner_email': 'bob@example.com',
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      // Both tab labels render; the Owned tab is active by default.
      expect(find.text('Owned by Me'), findsOneWidget);
      expect(find.text('Shared with Me'), findsOneWidget);
      expect(find.text('My Project'), findsOneWidget);

      // Switch to the Shared tab to see shared workspaces.
      await tester.tap(find.text('Shared with Me'));
      await tester.pumpAndSettle();

      expect(find.text('Team Project'), findsOneWidget);
      expect(find.text('Other Project'), findsOneWidget);
      expect(find.textContaining('alice@example.com'), findsOneWidget);
      expect(find.textContaining('bob@example.com'), findsOneWidget);
      // 2 shared terminals visible on this tab.
      expect(find.byIcon(Icons.terminal), findsNWidgets(2));
    });

    testWidgets('load more appends next page and hides when done',
        (tester) async {
      // Page 1: 1 workspace, signals more. Page 2 (offset=10): 1 more,
      // no more. The mock branches on the offset query param.
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          final offset = int.parse(
            request.url.queryParameters['offset'] ?? '0',
          );
          if (offset == 0) {
            return http.Response(
              jsonEncode({
                'items': [
                  {
                    'id': 'ws-1',
                    'name': 'First',
                    'container_id': null,
                    'created_at': '2026-01-01',
                  },
                ],
                'has_more': true,
                'next_offset': 10,
              }),
              200,
            );
          }
          return http.Response(
            jsonEncode({
              'items': [
                {
                  'id': 'ws-2',
                  'name': 'Second',
                  'container_id': null,
                  'created_at': '2026-02-01',
                },
              ],
              'has_more': false,
              'next_offset': null,
            }),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces/ws-1/members' ||
            request.url.path == '/api/v1/workspaces/ws-2/members') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      // First page rendered, "Load more" shown.
      expect(find.text('First'), findsOneWidget);
      expect(find.text('Second'), findsNothing);
      expect(find.text('Load more workspaces'), findsOneWidget);

      // Tap load more → second page appended, control disappears.
      await tester.tap(find.text('Load more workspaces'));
      await tester.pumpAndSettle();

      expect(find.text('First'), findsOneWidget);
      expect(find.text('Second'), findsOneWidget);
      expect(find.text('Load more workspaces'), findsNothing);
    });

    testWidgets('load more works for the shared section', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces/shared') {
          final offset = int.parse(
            request.url.queryParameters['offset'] ?? '0',
          );
          if (offset == 0) {
            return http.Response(
              jsonEncode({
                'items': [
                  {
                    'id': 'sh-1',
                    'name': 'Shared First',
                    'container_id': null,
                    'created_at': '2026-01-01',
                    'owner_email': 'a@example.com',
                  },
                ],
                'has_more': true,
                'next_offset': 10,
              }),
              200,
            );
          }
          return http.Response(
            jsonEncode({
              'items': [
                {
                  'id': 'sh-2',
                  'name': 'Shared Second',
                  'container_id': null,
                  'created_at': '2026-02-01',
                  'owner_email': 'b@example.com',
                },
              ],
              'has_more': false,
              'next_offset': null,
            }),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/sh-1/members' ||
            request.url.path == '/api/v1/workspaces/sh-2/members') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      // Switch to the Shared tab (owned is empty/active by default).
      await tester.tap(find.text('Shared with Me'));
      await tester.pumpAndSettle();

      expect(find.text('Shared First'), findsOneWidget);
      expect(find.text('Load more shared workspaces'), findsOneWidget);

      await tester.tap(find.text('Load more shared workspaces'));
      await tester.pumpAndSettle();

      expect(find.text('Shared First'), findsOneWidget);
      expect(find.text('Shared Second'), findsOneWidget);
      expect(find.text('Load more shared workspaces'), findsNothing);
    });

    testWidgets('sorting by name requests sort=name and resets to page 1',
        (tester) async {
      var lastSort = '';
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          lastSort = request.url.queryParameters['sort'] ?? '';
          return http.Response(
            jsonEncode({
              'items': [
                {
                  'id': 'ws-1',
                  'name': 'Alpha',
                  'container_id': null,
                  'created_at': ''
                },
              ],
              'has_more': false,
              'next_offset': null,
            }),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces/ws-1/members') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();
      expect(lastSort, 'created');

      // Tap the Name sort chip -> request uses sort=name (asc by default).
      await tester.tap(find.text('Name'));
      await tester.pumpAndSettle();
      expect(lastSort, 'name');

      // Tap the now-active Name chip again -> toggles direction to desc.
      await tester.tap(find.textContaining('Name'));
      await tester.pumpAndSettle();
      expect(lastSort, 'name');
    });

    testWidgets('sort state is independent per tab', (tester) async {
      String? ownedSort;
      String? sharedSort;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          ownedSort = request.url.queryParameters['sort'];
          return http.Response(
            jsonEncode({
              'items': [
                {
                  'id': 'ws-1',
                  'name': 'Alpha',
                  'container_id': null,
                  'created_at': ''
                },
              ],
              'has_more': false,
              'next_offset': null,
            }),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/shared') {
          sharedSort = request.url.queryParameters['sort'];
          return http.Response(
            jsonEncode({
              'items': [
                {
                  'id': 'sh-1',
                  'name': 'Shared',
                  'container_id': null,
                  'created_at': '',
                  'owner_email': 'o@e.com'
                },
              ],
              'has_more': false,
              'next_offset': null,
            }),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/ws-1/members' ||
            request.url.path == '/api/v1/workspaces/sh-1/members') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      // Owned tab is active: sort by name there.
      await tester.tap(find.text('Name'));
      await tester.pumpAndSettle();
      expect(ownedSort, 'name');

      // Switch to the Shared tab: its request should still use the
      // default 'created' sort, unaffected by the Owned tab's sort.
      await tester.tap(find.text('Shared with Me'));
      await tester.pumpAndSettle();
      expect(sharedSort, 'created');
    });

    testWidgets('filter box sends q= and filters results', (tester) async {
      var lastQ = '';
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          lastQ = request.url.queryParameters['q'] ?? '';
          final items = (lastQ.isEmpty)
              ? [
                  {
                    'id': 'ws-1',
                    'name': 'Alpha',
                    'container_id': null,
                    'created_at': ''
                  },
                  {
                    'id': 'ws-2',
                    'name': 'Beta',
                    'container_id': null,
                    'created_at': ''
                  },
                ]
              : [
                  {
                    'id': 'ws-1',
                    'name': 'Alpha',
                    'container_id': null,
                    'created_at': ''
                  },
                ];
          return http.Response(
            jsonEncode(
                {'items': items, 'has_more': false, 'next_offset': null}),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces/ws-1/members' ||
            request.url.path == '/api/v1/workspaces/ws-2/members') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();
      expect(find.text('Alpha'), findsOneWidget);
      expect(find.text('Beta'), findsOneWidget);

      // Type into the filter box (debounced 300ms).
      await tester.enterText(find.byType(TextField).first, 'alp');
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();

      expect(lastQ, 'alp');
      expect(find.text('Alpha'), findsOneWidget);
      expect(find.text('Beta'), findsNothing);
    });

    testWidgets('shows only shared section when no owned workspaces',
        (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces/shared') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-s1',
                'name': 'Guest Project',
                'container_id': null,
                'created_at': '2026-03-01 08:00:00',
                'owner_email': 'owner@example.com',
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      // Both tab labels always render; the Owned tab is empty.
      expect(find.text('Owned by Me'), findsOneWidget);
      expect(find.text('Shared with Me'), findsOneWidget);

      // Switch to the Shared tab to see the shared workspace.
      await tester.tap(find.text('Shared with Me'));
      await tester.pumpAndSettle();
      expect(find.text('Guest Project'), findsOneWidget);
    });

    testWidgets('handles missing and invalid created_at gracefully',
        (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'No Date',
                'container_id': null,
                'created_at': null
              },
              {
                'id': 'ws-2',
                'name': 'Bad Date',
                'container_id': null,
                'created_at': 'not-a-date'
              },
              {
                'id': 'ws-3',
                'name': 'Empty Date',
                'container_id': null,
                'created_at': ''
              },
              {
                'id': 'ws-4',
                'name': 'Midnight',
                'container_id': null,
                'created_at': '2026-03-01 00:15:00'
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.text('No Date'), findsOneWidget);
      expect(find.text('Bad Date'), findsOneWidget);
      // Invalid date falls back to raw string
      expect(find.text('not-a-date'), findsOneWidget);
    });

    testWidgets('shows empty state when no workspaces', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.textContaining('No workspaces'), findsOneWidget);
    });

    testWidgets('import FAB opens import workspace dialog', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('Import Workspace'));
      await tester.pumpAndSettle();

      expect(find.text('Import Workspace'), findsOneWidget);
      expect(find.text('Select .tar.gz file'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
      expect(find.text('Import'), findsOneWidget);

      // Cancel closes it
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(find.text('Import Workspace'), findsNothing);
    });

    testWidgets('successful import refreshes workspace list', (tester) async {
      var importCalled = false;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          if (importCalled) {
            return http.Response(
              jsonEncode(_envelope([
                {
                  'id': 'ws-imp',
                  'name': 'Imported WS',
                  'container_id': null,
                  'created_at': '2026-06-29',
                },
              ])),
              200,
            );
          }
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces/import' &&
            request.method == 'POST') {
          importCalled = true;
          return http.Response(
            jsonEncode({'id': 'ws-imp', 'name': 'Imported WS'}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      testPickFileBytesOverride = ({String accept = ''}) async => [1, 2, 3];

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('Import Workspace'));
      await tester.pumpAndSettle();

      // Pick a file
      await tester.tap(find.text('Select .tar.gz file'));
      await tester.pumpAndSettle();

      // Tap Import
      await tester.tap(find.text('Import'));
      await tester.pumpAndSettle();

      expect(importCalled, isTrue);
      expect(find.text('Imported WS'), findsOneWidget);
    });

    testWidgets('shows delete button for each workspace', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'Test WS',
                'container_id': null,
                'created_at': '2026-01-01'
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.byIcon(Icons.delete_outline), findsOneWidget);
    });

    testWidgets('shows loading indicator initially', (tester) async {
      final completer = Completer<http.Response>();
      testAuthHttpClientOverride = withPermissions((request) async {
        return completer.future;
      });

      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byType(CircularProgressIndicator), findsOneWidget);

      // Complete the request so the test can clean up
      completer.complete(http.Response(jsonEncode(_envelope([])), 200));
      await tester.pumpAndSettle();
    });

    testWidgets('shows error snackbar on load failure', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        throw Exception('Network error');
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.textContaining('Failed to load workspaces'), findsOneWidget);
    });

    testWidgets('shows created date for workspaces', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'My Project',
                'container_id': null,
                'created_at': '2026-03-15'
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.textContaining('2026-03-15'), findsOneWidget);
    });

    testWidgets('FAB opens create workspace dialog', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      expect(find.text('New Workspace'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
      expect(find.text('Create'), findsOneWidget);
    });

    testWidgets('create dialog has text field', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      expect(
          find.descendant(
              of: find.byType(AlertDialog), matching: find.byType(TextField)),
          findsNWidgets(5));
      expect(find.byType(DropdownButtonFormField<String>), findsOneWidget);
    });

    testWidgets('cancel button closes create dialog', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(find.text('New Workspace'), findsNothing);
    });

    testWidgets('delete button shows confirmation dialog', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'To Delete',
                'container_id': null,
                'created_at': '2026-01-01'
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.delete_outline));
      await tester.pumpAndSettle();

      expect(find.text('Delete Workspace'), findsOneWidget);
      expect(find.textContaining('delete the workspace'), findsOneWidget);
      expect(find.text('Delete'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
    });

    testWidgets('cancel delete closes dialog without deleting', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'Keep Me',
                'container_id': null,
                'created_at': '2026-01-01'
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.delete_outline));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      // Workspace should still be there
      expect(find.text('Keep Me'), findsOneWidget);
    });

    testWidgets('workspace cards use ListTile', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'WS 1',
                'container_id': null,
                'created_at': '2026-01-01'
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.byType(ListTile), findsOneWidget);
    });

    testWidgets('shows logged-in email in app bar', (tester) async {
      final token = makeJwt({
        'sub': 'user-1',
        'email': 'alice@example.com',
        'roles': ['user'],
      });
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.text('alice@example.com'), findsOneWidget);
    });

    testWidgets('create dialog submit adds workspace to list', (tester) async {
      var postCalled = false;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          if (postCalled) {
            return http.Response(
              jsonEncode(_envelope([
                {
                  'id': 'ws-new',
                  'name': 'New WS',
                  'container_id': null,
                  'created_at': '2026-05-21',
                },
              ])),
              200,
            );
          }
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postCalled = true;
          return http.Response(
            jsonEncode({
              'id': 'ws-new',
              'name': 'New WS',
              'container_id': null,
              'created_at': '2026-05-21',
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      // Open dialog
      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      // Type workspace name and tap Create
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .first,
          'New WS');
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(postCalled, isTrue);
      expect(find.text('New WS'), findsOneWidget);
    });

    testWidgets('create dialog shows inline error on failure', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          return http.Response(
            jsonEncode({'detail': 'Name already taken'}),
            409,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .first,
          'Duplicate');
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(find.text('Name already taken'), findsOneWidget);
    });

    testWidgets('confirm delete removes workspace from list', (tester) async {
      var deleteCalled = false;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          if (deleteCalled) {
            return http.Response(jsonEncode(_envelope([])), 200);
          }
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'Doomed',
                'container_id': null,
                'created_at': '2026-01-01',
              },
            ])),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/ws-1' &&
            request.method == 'DELETE') {
          deleteCalled = true;
          return http.Response('', 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.text('Doomed'), findsOneWidget);

      // Tap delete icon
      await tester.tap(find.byIcon(Icons.delete_outline));
      await tester.pumpAndSettle();

      // Confirm deletion
      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();

      expect(deleteCalled, isTrue);
      expect(find.text('Doomed'), findsNothing);
      expect(find.textContaining('No workspaces'), findsOneWidget);
    });

    testWidgets('tapping workspace card navigates to workspace URL',
        (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-42',
                'name': 'Nav Test',
                'container_id': null,
                'created_at': '2026-01-01',
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      String? navigatedTo;
      final router = GoRouter(
        initialLocation: '/',
        routes: [
          GoRoute(
            path: '/',
            builder: (context, state) => const WorkspaceListPage(),
          ),
          GoRoute(
            path: '/workspace/:id',
            builder: (context, state) {
              navigatedTo = state.uri.toString();
              return const Scaffold(
                body: Text('workspace detail'),
              );
            },
          ),
        ],
      );

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider(create: (_) => AuthService()),
            ChangeNotifierProvider.value(value: WsClient()),
          ],
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.text('Nav Test'));
      await tester.pumpAndSettle();

      expect(navigatedTo, '/workspace/ws-42');
    });

    testWidgets('admin icon shown when user has admin permission',
        (tester) async {
      final token = makeJwt({
        'sub': 'admin-1',
        'email': 'admin@example.com',
      });
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      testAuthHttpClientOverride = withPermissions(
        (request) async {
          if (request.url.path == '/api/v1/workspaces') {
            return http.Response(jsonEncode(_envelope([])), 200);
          }
          return http.Response('Not found', 404);
        },
        permissions: {
          '/admin': ['*'],
          '/workspaces': ['create'],
        },
        groups: [
          {'id': 'g1', 'name': 'admin'},
        ],
      );

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.byIcon(Icons.manage_accounts), findsOneWidget);
      expect(find.byTooltip('Admin'), findsOneWidget);
    });

    testWidgets('admin icon not shown for non-admin user', (tester) async {
      final token = makeJwt({
        'sub': 'user-1',
        'email': 'user@example.com',
      });
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      testAuthHttpClientOverride = withPermissions(
        (request) async {
          if (request.url.path == '/api/v1/workspaces') {
            return http.Response(jsonEncode(_envelope([])), 200);
          }
          return http.Response('Not found', 404);
        },
        permissions: {
          '/': ['view'],
        },
      );

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.byIcon(Icons.manage_accounts), findsNothing);
    });

    testWidgets('create dialog submit via text field onSubmitted',
        (tester) async {
      var postCalled = false;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          if (postCalled) {
            return http.Response(
              jsonEncode(_envelope([
                {
                  'id': 'ws-sub',
                  'name': 'Submitted',
                  'container_id': null,
                  'created_at': '2026-05-21',
                },
              ])),
              200,
            );
          }
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postCalled = true;
          return http.Response(
            jsonEncode({
              'id': 'ws-sub',
              'name': 'Submitted',
              'container_id': null,
              'created_at': '2026-05-21',
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      // Type and submit via keyboard (onSubmitted)
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .first,
          'Submitted');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(postCalled, isTrue);
      expect(find.text('Submitted'), findsOneWidget);
    });

    testWidgets('create dialog with image selection', (tester) async {
      String? postedBody;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/images' && request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'default': 'klangk',
              'allowed': ['klangk', 'klangk-custom'],
            }),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = request.body;
          return http.Response(
            jsonEncode({
              'id': 'ws-img',
              'name': 'ImgWS',
              'container_id': null,
              'created_at': '2026-05-28',
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      // Dropdown should show the default image (logo also contains 'klangk')
      expect(find.text('klangk'), findsWidgets);

      // Select non-default image
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('klangk-custom').last);
      await tester.pumpAndSettle();

      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .first,
          'ImgWS');
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(postedBody, isNotNull);
      final body = jsonDecode(postedBody!) as Map<String, dynamic>;
      expect(body['name'], 'ImgWS');
      expect(body['image'], 'klangk-custom');
    });

    testWidgets('create dialog sends default_command when provided',
        (tester) async {
      String? postedBody;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = request.body;
          return http.Response(
            jsonEncode({
              'id': 'ws-cmd',
              'name': 'CmdWS',
              'container_id': null,
              'created_at': '2026-05-28',
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      // Enter name and command
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .first,
          'CmdWS');
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(1),
          'klangk-pi');
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(postedBody, isNotNull);
      final body = jsonDecode(postedBody!) as Map<String, dynamic>;
      expect(body['name'], 'CmdWS');
      expect(body['default_command'], 'klangk-pi');
    });

    testWidgets('create dialog submit via command field onSubmitted',
        (tester) async {
      var postCalled = false;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          if (postCalled) {
            return http.Response(
              jsonEncode(_envelope([
                {
                  'id': 'ws-cmd2',
                  'name': 'CmdSubmit',
                  'container_id': null,
                  'created_at': '2026-05-28',
                },
              ])),
              200,
            );
          }
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postCalled = true;
          return http.Response(
            jsonEncode({
              'id': 'ws-cmd2',
              'name': 'CmdSubmit',
              'container_id': null,
              'created_at': '2026-05-28',
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      // Enter name, then focus command field and submit via Enter
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .first,
          'CmdSubmit');
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(1),
          'pi');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(postCalled, isTrue);
    });

    testWidgets('create workspace exception shows inline error',
        (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          throw Exception('Network error');
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .first,
          'Fail WS');
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Error:'), findsOneWidget);
    });

    testWidgets('delete workspace exception shows error snackbar',
        (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'Doomed',
                'container_id': null,
                'created_at': '2026-01-01',
              },
            ])),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/ws-1' &&
            request.method == 'DELETE') {
          throw Exception('Network error');
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.delete_outline));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Delete'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Error:'), findsOneWidget);
    });

    testWidgets('logout button calls logout and navigates', (tester) async {
      var logoutCalled = false;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/auth/logout') {
          logoutCalled = true;
          return http.Response('', 200);
        }
        return http.Response('Not found', 404);
      });

      final router = GoRouter(
        initialLocation: '/',
        routes: [
          GoRoute(
            path: '/',
            builder: (_, __) => const WorkspaceListPage(),
          ),
          GoRoute(
            path: '/login',
            builder: (_, __) => const Scaffold(body: Text('Login')),
          ),
        ],
      );

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider(create: (_) => AuthService()),
            ChangeNotifierProvider.value(value: WsClient()),
          ],
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.logout));
      await tester.pumpAndSettle();

      expect(logoutCalled, isTrue);
    });

    testWidgets('logout with oidc redirect calls navigateTo', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/auth/logout') {
          return http.Response(
            jsonEncode({
              'status': 'ok',
              'oidc_logout_url': 'https://idp.example.com/logout',
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      final router = GoRouter(
        initialLocation: '/',
        routes: [
          GoRoute(
            path: '/',
            builder: (_, __) => const WorkspaceListPage(),
          ),
          GoRoute(
            path: '/login',
            builder: (_, __) => const Scaffold(body: Text('Login')),
          ),
        ],
      );

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider(create: (_) => AuthService()),
            ChangeNotifierProvider.value(value: WsClient()),
          ],
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.logout));
      await tester.pumpAndSettle();
      // navigateTo is a stub (no-op) in VM tests — just verifying no crash
    });

    testWidgets('title tap navigates to home', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      final router = GoRouter(
        initialLocation: '/',
        routes: [
          GoRoute(
            path: '/',
            builder: (_, __) => const WorkspaceListPage(),
          ),
          GoRoute(
            path: '/workspace/:id',
            builder: (_, __) => const Scaffold(),
          ),
        ],
      );

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider(create: (_) => AuthService()),
            ChangeNotifierProvider.value(value: WsClient()),
          ],
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      // Tap the "Workspaces" title text
      await tester.tap(find.text('Workspaces'));
      await tester.pumpAndSettle();

      // Already on '/', so no navigation change — but the onTap fires
      // Just verify it didn't crash
      expect(find.text('Workspaces'), findsOneWidget);
    });

    testWidgets('admin button navigates to admin page', (tester) async {
      final token = makeJwt({
        'sub': 'user-1',
        'email': 'admin@example.com',
      });
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
      testAuthHttpClientOverride = withPermissions(
        (request) async {
          if (request.url.path == '/api/v1/workspaces') {
            return http.Response(jsonEncode(_envelope([])), 200);
          }
          return http.Response('Not found', 404);
        },
        permissions: {
          '/admin': ['*'],
          '/workspaces': ['create'],
        },
        groups: [
          {'id': 'g1', 'name': 'admin'},
        ],
      );

      String? navigatedTo;
      final router = GoRouter(
        initialLocation: '/',
        routes: [
          GoRoute(
            path: '/',
            builder: (_, __) => const WorkspaceListPage(),
          ),
          GoRoute(
            path: '/admin/users',
            builder: (_, __) {
              navigatedTo = '/admin/users';
              return const Scaffold(body: Text('Admin'));
            },
          ),
        ],
      );

      await tester.pumpWidget(
        MultiProvider(
          providers: [
            ChangeNotifierProvider(create: (_) => AuthService()),
            ChangeNotifierProvider.value(value: WsClient()),
          ],
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.manage_accounts));
      await tester.pumpAndSettle();

      expect(navigatedTo, '/admin/users');
    });

    testWidgets('create workspace with mounts', (tester) async {
      String? postedBody;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = request.body;
          return http.Response(
            jsonEncode({
              'id': 'ws-mnt',
              'name': 'MountWS',
              'container_id': null,
              'created_at': '2026-05-28',
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      // Enter workspace name
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .first,
          'MountWS');

      // Add a mount via the mount text field (last TextField) + add button
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(2),
          '/host/src:/work/src');
      // Tap the add (+) button next to the mount input
      // The FAB also has an add icon, so find the one inside the dialog
      final addIcons = find.byIcon(Icons.add);
      // The mount add icon is at index 1 (after FAB at 0, before env at 2)
      await tester.tap(addIcons.at(1));
      await tester.pumpAndSettle();

      // Mount should appear in the list
      expect(find.text('/host/src:/work/src'), findsOneWidget);

      // Add a second mount
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(2),
          'nix-vol:/nix');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();
      expect(find.text('nix-vol:/nix'), findsOneWidget);

      // Remove the first mount via its X button
      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pumpAndSettle();
      expect(find.text('/host/src:/work/src'), findsNothing);

      // Submit
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(postedBody, isNotNull);
      final body = jsonDecode(postedBody!) as Map<String, dynamic>;
      expect(body['name'], 'MountWS');
      expect(body['mounts'], ['nix-vol:/nix']);
    });

    testWidgets('create dialog adds mount via Enter key', (tester) async {
      String? postedBody;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = request.body;
          return http.Response(
            jsonEncode({
              'id': 'ws-ent',
              'name': 'EnterWS',
              'container_id': null,
              'created_at': '2026-05-28',
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .first,
          'EnterWS');

      // Add mount via Enter key on the mount text field
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(2),
          '/a:/b');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(find.text('/a:/b'), findsOneWidget);

      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(postedBody, isNotNull);
      final body = jsonDecode(postedBody!) as Map<String, dynamic>;
      expect(body['mounts'], ['/a:/b']);
    });

    testWidgets('create dialog rejects invalid mount', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      // Try adding invalid mount (no colon)
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(2),
          'bad-mount');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();

      expect(find.textContaining('Expected'), findsOneWidget);
      // Mount should NOT have been added
      expect(find.text('bad-mount'), findsOneWidget); // still in text field

      // Try adding mount with relative container path
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(2),
          '/host:relative');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();

      expect(find.textContaining('absolute'), findsOneWidget);

      // Try adding mount with unknown option
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(2),
          '/host:/container:bogus');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();

      expect(find.textContaining('Unknown option'), findsOneWidget);

      // Valid mount clears the error
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(2),
          '/a:/b');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();

      expect(find.textContaining('Unknown option'), findsNothing);
      expect(find.text('/a:/b'), findsOneWidget);
    });

    testWidgets('create workspace with env vars', (tester) async {
      String? postedBody;
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = request.body;
          return http.Response(
            jsonEncode({
              'id': 'ws-env',
              'name': 'EnvWS',
              'container_id': null,
              'created_at': '2026-05-28',
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .first,
          'EnvWS');

      // Add env var via the + button (env add is at index 2: FAB=0, mount=1, env=2)
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(3),
          'FOO=bar');
      await tester.ensureVisible(find.byIcon(Icons.add).at(2));
      await tester.tap(find.byIcon(Icons.add).at(2));
      await tester.pumpAndSettle();
      expect(find.text('FOO=bar'), findsOneWidget);

      // Add a second env var via Enter key
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(3),
          'X=1');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(find.text('X=1'), findsOneWidget);

      // Remove the first env var via X button
      // close icons: mount has none, env has 2 (FOO=bar, X=1)
      await tester.ensureVisible(find.byIcon(Icons.close).first);
      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pumpAndSettle();
      expect(find.text('FOO=bar'), findsNothing);

      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(postedBody, isNotNull);
      final body = jsonDecode(postedBody!) as Map<String, dynamic>;
      expect(body['name'], 'EnvWS');
      expect(body['env'], {'X': '1'});
    });

    testWidgets('create dialog rejects invalid env var', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      // Try adding env var without = sign
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(3),
          'NOEQ');
      await tester.tap(find.byIcon(Icons.add).at(2));
      await tester.pumpAndSettle();
      expect(find.textContaining('Expected KEY=VALUE'), findsOneWidget);

      // Try adding env var with empty key
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(3),
          '=value');
      await tester.tap(find.byIcon(Icons.add).at(2));
      await tester.pumpAndSettle();
      expect(find.textContaining('Key cannot be empty'), findsOneWidget);

      // Valid env var clears error
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(3),
          'A=1');
      await tester.tap(find.byIcon(Icons.add).at(2));
      await tester.pumpAndSettle();
      expect(find.textContaining('Key cannot'), findsNothing);
      expect(find.text('A=1'), findsOneWidget);
    });

    testWidgets('copy button appears and works for mount', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      // No copy buttons initially
      expect(find.byTooltip('Copy'), findsNothing);

      // Add a mount
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(2),
          '/src:/work');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();

      // Copy button appears for the mount
      expect(find.byTooltip('Copy'), findsOneWidget);

      // Tap copy button — exercises the Clipboard.setData call
      await tester.tap(find.byTooltip('Copy').first);
      await tester.pump();
    });

    testWidgets('copy button appears and works for env var', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'GET') {
          return http.Response(jsonEncode(_envelope([])), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byTooltip('New Workspace'));
      await tester.pumpAndSettle();

      // Add an env var
      await tester.enterText(
          find
              .descendant(
                  of: find.byType(AlertDialog),
                  matching: find.byType(TextField))
              .at(3),
          'FOO=bar');
      await tester.tap(find.byIcon(Icons.add).at(2));
      await tester.pumpAndSettle();

      // Copy button appears for the env var
      expect(find.byTooltip('Copy'), findsOneWidget);

      // Tap copy button — exercises the Clipboard.setData call
      await tester.tap(find.byTooltip('Copy').first);
      await tester.pump();
    });

    testWidgets('workspace card colors icon by running state', (tester) async {
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'Running WS',
                'container_id': null,
                'created_at': '2026-01-01',
                'running': true,
              },
              {
                'id': 'ws-2',
                'name': 'Stopped WS',
                'container_id': null,
                'created_at': '2026-01-01',
                'running': false,
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      // The terminal Icon itself signals running state: green when
      // running, grey when stopped. The Icon is the ListTile's leading.
      final tiles = tester.widgetList<ListTile>(find.byType(ListTile)).toList();
      expect(tiles.length, 2);
      final runningIcon = tiles[0].leading as Icon;
      final stoppedIcon = tiles[1].leading as Icon;
      expect(runningIcon.icon, Icons.terminal);
      expect(runningIcon.color, KColors.accentGreen);
      expect(stoppedIcon.icon, Icons.terminal);
      expect(stoppedIcon.color, KColors.textSecondary);
    });

    testWidgets('container_status event updates running state', (tester) async {
      final ws = _MockWsClient();
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'created_at': '2026-01-01',
                'running': false,
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage(wsClient: ws));
      await tester.pumpAndSettle();

      // Simulate container starting via WS event
      ws.emitContainerStatus('ws-1', true);
      await tester.pump();

      // Widget should still be rendered (no errors)
      expect(find.text('My WS'), findsOneWidget);
    });

    testWidgets('container stopping clears stale health status',
        (tester) async {
      final ws = _MockWsClient();
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'created_at': '2026-01-01',
                'running': false,
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage(wsClient: ws));
      await tester.pumpAndSettle();

      // Start the container, report it unhealthy (amber), then stop it —
      // a stopped container has no health status, so the icon must drop
      // back to grey rather than lingering on the stale unhealthy colour.
      ws.emitContainerStatus('ws-1', true);
      await tester.pump();
      await tester.pump();

      ws.emitServiceHealth('ws-1', false);
      await tester.pump();
      await tester.pump();
      final unhealthyTile = tester.widget<ListTile>(
        find.ancestor(
          of: find.text('My WS'),
          matching: find.byType(ListTile),
        ),
      );
      expect((unhealthyTile.leading as Icon).color, Colors.orange);

      ws.emitContainerStatus('ws-1', false);
      await tester.pump();
      await tester.pump();
      final stoppedTile = tester.widget<ListTile>(
        find.ancestor(
          of: find.text('My WS'),
          matching: find.byType(ListTile),
        ),
      );
      expect((stoppedTile.leading as Icon).color, KColors.textSecondary);
    });

    testWidgets('service_health event recolours the list icon', (tester) async {
      final ws = _MockWsClient();
      testAuthHttpClientOverride = withPermissions((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode(_envelope([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'created_at': '2026-01-01',
                'running': false,
              },
            ])),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage(wsClient: ws));
      await tester.pumpAndSettle();

      // Start the container, then report it as unhealthy.  The
      // leading terminal icon should turn amber for an unhealthy
      // container and green for a healthy one.
      ws.emitContainerStatus('ws-1', true);
      await tester.pump();

      ws.emitServiceHealth('ws-1', false);
      // The broadcast stream delivers on a microtask; a single pump may
      // build its frame before the microtask runs. Pump twice so the
      // handler's setState is flushed into a real rebuild.
      await tester.pump();
      await tester.pump();
      final unhealthyTile = tester.widget<ListTile>(
        find.ancestor(
          of: find.text('My WS'),
          matching: find.byType(ListTile),
        ),
      );
      expect((unhealthyTile.leading as Icon).color, Colors.orange);

      ws.emitServiceHealth('ws-1', true);
      // Same microtask flush as above.
      await tester.pump();
      await tester.pump();
      final healthyTile = tester.widget<ListTile>(
        find.ancestor(
          of: find.text('My WS'),
          matching: find.byType(ListTile),
        ),
      );
      expect((healthyTile.leading as Icon).color, KColors.accentGreen);
    });
  });
}
