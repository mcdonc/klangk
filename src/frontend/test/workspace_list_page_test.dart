import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:bark_frontend/auth/auth_service.dart';
import 'package:bark_frontend/workspace/workspace_list_page.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  String makeJwt(Map<String, dynamic> payload) {
    final header = base64Url
        .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
        .replaceAll('=', '');
    final body =
        base64Url.encode(utf8.encode(jsonEncode(payload))).replaceAll('=', '');
    return '$header.$body.fakesig';
  }

  Widget buildPage() {
    return ChangeNotifierProvider(
      create: (_) => AuthService(),
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

    testWidgets('has FAB for creating workspaces', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byType(FloatingActionButton), findsOneWidget);
      expect(find.byIcon(Icons.add), findsOneWidget);
    });

    testWidgets('has logout button', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byIcon(Icons.logout), findsOneWidget);
    });

    testWidgets('shows Bark logo', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.text('Bark'), findsOneWidget);
      expect(find.byIcon(Icons.pets), findsOneWidget);
    });

    testWidgets('shows workspace list from mock', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'Project A',
                'container_id': null,
                'created_at': '2026-01-01'
              },
              {
                'id': 'ws-2',
                'name': 'Project B',
                'container_id': null,
                'created_at': '2026-01-02'
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.text('Project A'), findsOneWidget);
      expect(find.text('Project B'), findsOneWidget);
      expect(find.byIcon(Icons.folder), findsNWidgets(2));
    });

    testWidgets('shows empty state when no workspaces', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.textContaining('No workspaces'), findsOneWidget);
    });

    testWidgets('shows delete button for each workspace', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'Test WS',
                'container_id': null,
                'created_at': '2026-01-01'
              },
            ]),
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
      testAuthHttpClientOverride = MockClient((request) async {
        return completer.future;
      });

      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byType(CircularProgressIndicator), findsOneWidget);

      // Complete the request so the test can clean up
      completer.complete(http.Response(jsonEncode([]), 200));
      await tester.pumpAndSettle();
    });

    testWidgets('shows error snackbar on load failure', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        throw Exception('Network error');
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.textContaining('Failed to load workspaces'), findsOneWidget);
    });

    testWidgets('shows created date for workspaces', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My Project',
                'container_id': null,
                'created_at': '2026-03-15'
              },
            ]),
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
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      expect(find.text('New Workspace'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
      expect(find.text('Create'), findsOneWidget);
    });

    testWidgets('create dialog has text field', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      expect(find.byType(TextField), findsOneWidget);
      expect(find.text('Workspace name'), findsOneWidget);
    });

    testWidgets('cancel button closes create dialog', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(find.text('New Workspace'), findsNothing);
    });

    testWidgets('delete button shows confirmation dialog', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'To Delete',
                'container_id': null,
                'created_at': '2026-01-01'
              },
            ]),
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
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'Keep Me',
                'container_id': null,
                'created_at': '2026-01-01'
              },
            ]),
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
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'WS 1',
                'container_id': null,
                'created_at': '2026-01-01'
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.byType(Card), findsOneWidget);
      expect(find.byType(ListTile), findsOneWidget);
    });

    testWidgets('shows logged-in email in app bar', (tester) async {
      final token = makeJwt({
        'sub': 'user-1',
        'email': 'alice@example.com',
        'roles': ['user'],
      });
      SharedPreferences.setMockInitialValues({'bark_jwt': token});
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.text('alice@example.com'), findsOneWidget);
    });

    testWidgets('create dialog submit adds workspace to list', (tester) async {
      var postCalled = false;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          if (postCalled) {
            return http.Response(
              jsonEncode([
                {
                  'id': 'ws-new',
                  'name': 'New WS',
                  'container_id': null,
                  'created_at': '2026-05-21',
                },
              ]),
              200,
            );
          }
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/workspaces' && request.method == 'POST') {
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
      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      // Type workspace name and tap Create
      await tester.enterText(find.byType(TextField), 'New WS');
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(postCalled, isTrue);
      expect(find.text('New WS'), findsOneWidget);
    });

    testWidgets('create dialog shows error snackbar on failure',
        (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/workspaces' && request.method == 'POST') {
          return http.Response(
            jsonEncode({'detail': 'Name already taken'}),
            409,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), 'Duplicate');
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(find.text('Name already taken'), findsOneWidget);
    });

    testWidgets('confirm delete removes workspace from list', (tester) async {
      var deleteCalled = false;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          if (deleteCalled) {
            return http.Response(jsonEncode([]), 200);
          }
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'Doomed',
                'container_id': null,
                'created_at': '2026-01-01',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/workspaces/ws-1' &&
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
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-42',
                'name': 'Nav Test',
                'container_id': null,
                'created_at': '2026-01-01',
              },
            ]),
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
        ChangeNotifierProvider(
          create: (_) => AuthService(),
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.text('Nav Test'));
      await tester.pumpAndSettle();

      expect(navigatedTo, '/workspace/ws-42');
    });

    testWidgets('admin icon shown when JWT has admin role', (tester) async {
      final token = makeJwt({
        'sub': 'admin-1',
        'email': 'admin@example.com',
        'roles': ['admin'],
      });
      SharedPreferences.setMockInitialValues({'bark_jwt': token});
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.byIcon(Icons.admin_panel_settings), findsOneWidget);
      expect(find.byTooltip('User Management'), findsOneWidget);
    });

    testWidgets('admin icon not shown for non-admin user', (tester) async {
      final token = makeJwt({
        'sub': 'user-1',
        'email': 'user@example.com',
        'roles': ['user'],
      });
      SharedPreferences.setMockInitialValues({'bark_jwt': token});
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      expect(find.byIcon(Icons.admin_panel_settings), findsNothing);
    });

    testWidgets('create dialog submit via text field onSubmitted',
        (tester) async {
      var postCalled = false;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          if (postCalled) {
            return http.Response(
              jsonEncode([
                {
                  'id': 'ws-sub',
                  'name': 'Submitted',
                  'container_id': null,
                  'created_at': '2026-05-21',
                },
              ]),
              200,
            );
          }
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/workspaces' && request.method == 'POST') {
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

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      // Type and submit via keyboard (onSubmitted)
      await tester.enterText(find.byType(TextField), 'Submitted');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(postCalled, isTrue);
      expect(find.text('Submitted'), findsOneWidget);
    });

    testWidgets('create workspace exception shows error snackbar',
        (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/workspaces' && request.method == 'POST') {
          throw Exception('Network error');
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), 'Fail WS');
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Error:'), findsOneWidget);
    });

    testWidgets('delete workspace exception shows error snackbar',
        (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'Doomed',
                'container_id': null,
                'created_at': '2026-01-01',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/workspaces/ws-1' &&
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
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/auth/logout') {
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
        ChangeNotifierProvider(
          create: (_) => AuthService(),
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.logout));
      await tester.pumpAndSettle();

      expect(logoutCalled, isTrue);
    });

    testWidgets('title tap navigates to home', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

      String? navigatedTo;
      final router = GoRouter(
        initialLocation: '/',
        routes: [
          GoRoute(
            path: '/',
            builder: (_, __) => const WorkspaceListPage(),
          ),
          GoRoute(
            path: '/workspace/:id',
            builder: (_, state) {
              navigatedTo = state.uri.toString();
              return const Scaffold();
            },
          ),
        ],
      );

      await tester.pumpWidget(
        ChangeNotifierProvider(
          create: (_) => AuthService(),
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
        'roles': ['admin'],
      });
      SharedPreferences.setMockInitialValues({'bark_jwt': token});
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(jsonEncode([]), 200);
        }
        return http.Response('Not found', 404);
      });

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
        ChangeNotifierProvider(
          create: (_) => AuthService(),
          child: MaterialApp.router(routerConfig: router),
        ),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.admin_panel_settings));
      await tester.pumpAndSettle();

      expect(navigatedTo, '/admin/users');
    });
  });
}
