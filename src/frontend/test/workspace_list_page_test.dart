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
import 'package:klangk_frontend/widgets/klangk_logo.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

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

    testWidgets('shows klangk logo', (tester) async {
      await tester.pumpWidget(buildPage());
      await tester.pump();

      expect(find.byType(KlangkLogo), findsOneWidget);
      expect(find.byIcon(Icons.smart_toy_outlined), findsOneWidget);
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
                'created_at': '2026-01-15 14:30:00'
              },
              {
                'id': 'ws-2',
                'name': 'Project B',
                'container_id': null,
                'created_at': '2026-06-02 09:00:00'
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
      // Dates formatted as local time (VM tests run in UTC)
      expect(find.textContaining('Jan 15, 2026'), findsOneWidget);
      expect(find.textContaining('Jun 2, 2026'), findsOneWidget);
    });

    testWidgets('handles missing and invalid created_at gracefully',
        (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces') {
          return http.Response(
            jsonEncode([
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
            ]),
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

      expect(find.byType(TextField), findsNWidgets(4));
      expect(find.byType(DropdownButtonFormField<String>), findsOneWidget);
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
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
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
      await tester.enterText(find.byType(TextField).first, 'New WS');
      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(postCalled, isTrue);
      expect(find.text('New WS'), findsOneWidget);
    });

    testWidgets('create dialog shows inline error on failure', (tester) async {
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

      await tester.enterText(find.byType(TextField).first, 'Duplicate');
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
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
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
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
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
      await tester.enterText(find.byType(TextField).first, 'Submitted');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(postCalled, isTrue);
      expect(find.text('Submitted'), findsOneWidget);
    });

    testWidgets('create dialog with image selection', (tester) async {
      String? postedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/images' && request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'default': 'klangk',
              'allowed': ['klangk', 'klangk-custom'],
            }),
            200,
          );
        }
        if (request.url.path == '/workspaces' && request.method == 'POST') {
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

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      // Dropdown should show the default image (logo also contains 'klangk')
      expect(find.text('klangk'), findsWidgets);

      // Select non-default image
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('klangk-custom').last);
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).first, 'ImgWS');
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
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/workspaces' && request.method == 'POST') {
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

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      // Enter name and command
      await tester.enterText(find.byType(TextField).first, 'CmdWS');
      await tester.enterText(find.byType(TextField).at(1), 'klangk-pi');
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
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          if (postCalled) {
            return http.Response(
              jsonEncode([
                {
                  'id': 'ws-cmd2',
                  'name': 'CmdSubmit',
                  'container_id': null,
                  'created_at': '2026-05-28',
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

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      // Enter name, then focus command field and submit via Enter
      await tester.enterText(find.byType(TextField).first, 'CmdSubmit');
      await tester.enterText(find.byType(TextField).at(1), 'pi');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(postCalled, isTrue);
    });

    testWidgets('create workspace exception shows inline error',
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

      await tester.enterText(find.byType(TextField).first, 'Fail WS');
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
      SharedPreferences.setMockInitialValues({'klangk_jwt': token});
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

    testWidgets('settings icon opens edit dialog', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': 'klangk-pi',
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      expect(find.text('Edit Workspace'), findsOneWidget);
      // Name field has current name, command field has current command
      final textFields = tester.widgetList<TextField>(find.byType(TextField));
      final texts = textFields.map((tf) => tf.controller!.text).toList();
      expect(texts, contains('My WS'));
      expect(texts, contains('klangk-pi'));
    });

    testWidgets('edit dialog saves default command', (tester) async {
      String? putBody;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/images' && request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'default': 'klangk',
              'allowed': ['klangk', 'klangk-custom'],
            }),
            200,
          );
        }
        if (request.url.path == '/workspaces/ws-1' && request.method == 'PUT') {
          putBody = request.body;
          return http.Response('{"status":"updated"}', 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).at(1), 'pi');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(putBody, isNotNull);
      final body = jsonDecode(putBody!) as Map<String, dynamic>;
      expect(body['name'], 'My WS');
      expect(body['default_command'], 'pi');
    });

    testWidgets('edit dialog changes image', (tester) async {
      String? putBody;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/images' && request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'default': 'klangk',
              'allowed': ['klangk', 'klangk-custom'],
            }),
            200,
          );
        }
        if (request.url.path == '/workspaces/ws-1' && request.method == 'PUT') {
          putBody = request.body;
          return http.Response('{"status":"updated"}', 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      // Change image
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('klangk-custom').last);
      await tester.pumpAndSettle();

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(putBody, isNotNull);
      final body = jsonDecode(putBody!) as Map<String, dynamic>;
      expect(body['image'], 'klangk-custom');
    });

    testWidgets('edit dialog submit via Enter', (tester) async {
      var putCalled = false;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.method == 'PUT') {
          putCalled = true;
          return http.Response('{"status":"updated"}', 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).at(1), 'pi');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(putCalled, isTrue);
    });

    testWidgets('edit dialog cancel does not save', (tester) async {
      var putCalled = false;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': 'bash',
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.method == 'PUT') {
          putCalled = true;
          return http.Response('{}', 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(putCalled, isFalse);
    });

    testWidgets('edit dialog error shows inline error', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.method == 'PUT') {
          return http.Response('{"detail":"fail"}', 500);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).at(1), 'pi');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Failed to update'), findsOneWidget);
    });

    testWidgets('edit dialog exception shows inline error', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.method == 'PUT') {
          throw Exception('Network error');
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).at(1), 'pi');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Error:'), findsOneWidget);
    });

    testWidgets('create workspace with mounts', (tester) async {
      String? postedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/workspaces' && request.method == 'POST') {
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

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      // Enter workspace name
      await tester.enterText(find.byType(TextField).first, 'MountWS');

      // Add a mount via the mount text field (last TextField) + add button
      await tester.enterText(
          find.byType(TextField).at(2), '/host/src:/work/src');
      // Tap the add (+) button next to the mount input
      // The FAB also has an add icon, so find the one inside the dialog
      final addIcons = find.byIcon(Icons.add);
      // The mount add icon is at index 1 (after FAB at 0, before env at 2)
      await tester.tap(addIcons.at(1));
      await tester.pumpAndSettle();

      // Mount should appear in the list
      expect(find.text('/host/src:/work/src'), findsOneWidget);

      // Add a second mount
      await tester.enterText(find.byType(TextField).at(2), 'nix-vol:/nix');
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

    testWidgets('edit workspace mounts', (tester) async {
      String? putBody;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'mounts': ['/old:/old'],
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/images' && request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'default': 'klangk',
              'allowed': ['klangk'],
            }),
            200,
          );
        }
        if (request.url.path == '/workspaces/ws-1' && request.method == 'PUT') {
          putBody = request.body;
          return http.Response('{"status":"updated"}', 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      // Existing mount should be visible
      expect(find.text('/old:/old'), findsOneWidget);

      // Add a new mount via the + button inside the dialog
      await tester.enterText(find.byType(TextField).at(2), '/new:/new');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();
      expect(find.text('/new:/new'), findsOneWidget);

      // Remove the old mount (first X button)
      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pumpAndSettle();
      expect(find.text('/old:/old'), findsNothing);

      // Save
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(putBody, isNotNull);
      final body = jsonDecode(putBody!) as Map<String, dynamic>;
      expect(body['mounts'], ['/new:/new']);
    });

    testWidgets('create dialog adds mount via Enter key', (tester) async {
      String? postedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/workspaces' && request.method == 'POST') {
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

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).first, 'EnterWS');

      // Add mount via Enter key on the mount text field
      await tester.enterText(find.byType(TextField).at(2), '/a:/b');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(find.text('/a:/b'), findsOneWidget);

      await tester.tap(find.text('Create'));
      await tester.pumpAndSettle();

      expect(postedBody, isNotNull);
      final body = jsonDecode(postedBody!) as Map<String, dynamic>;
      expect(body['mounts'], ['/a:/b']);
    });

    testWidgets('edit dialog adds mount via Enter key', (tester) async {
      String? putBody;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/images' && request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'default': 'klangk',
              'allowed': ['klangk']
            }),
            200,
          );
        }
        if (request.url.path == '/workspaces/ws-1' && request.method == 'PUT') {
          putBody = request.body;
          return http.Response('{"status":"updated"}', 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      // Add mount via Enter key
      await tester.enterText(find.byType(TextField).at(2), '/x:/y');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(find.text('/x:/y'), findsOneWidget);

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(putBody, isNotNull);
      final body = jsonDecode(putBody!) as Map<String, dynamic>;
      expect(body['mounts'], ['/x:/y']);
    });

    testWidgets('edit dialog shows error for non-JSON response',
        (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.method == 'PUT') {
          return http.Response('plain text error', 500);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(find.textContaining('plain text error'), findsOneWidget);
    });

    testWidgets('edit dialog shows body when JSON has no detail key',
        (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.method == 'PUT') {
          return http.Response(jsonEncode({'error': 'something'}), 400);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      // Falls back to response.body since no 'detail' key
      expect(find.textContaining('something'), findsOneWidget);
    });

    testWidgets('create dialog rejects invalid mount', (tester) async {
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

      // Try adding invalid mount (no colon)
      await tester.enterText(find.byType(TextField).at(2), 'bad-mount');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();

      expect(find.textContaining('Expected'), findsOneWidget);
      // Mount should NOT have been added
      expect(find.text('bad-mount'), findsOneWidget); // still in text field

      // Try adding mount with relative container path
      await tester.enterText(find.byType(TextField).at(2), '/host:relative');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();

      expect(find.textContaining('absolute'), findsOneWidget);

      // Try adding mount with unknown option
      await tester.enterText(
          find.byType(TextField).at(2), '/host:/container:bogus');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();

      expect(find.textContaining('Unknown option'), findsOneWidget);

      // Valid mount clears the error
      await tester.enterText(find.byType(TextField).at(2), '/a:/b');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();

      expect(find.textContaining('Unknown option'), findsNothing);
      expect(find.text('/a:/b'), findsOneWidget);
    });

    testWidgets('edit dialog rejects invalid mount', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/images' && request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'default': 'klangk',
              'allowed': ['klangk']
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      // Try adding invalid mount via Enter key
      await tester.enterText(find.byType(TextField).at(2), 'nope');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(find.textContaining('Expected'), findsOneWidget);

      // Valid mount clears the error
      await tester.enterText(find.byType(TextField).at(2), '/x:/y');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(find.textContaining('Expected'), findsNothing);
      expect(find.text('/x:/y'), findsOneWidget);
    });

    testWidgets('create workspace with env vars', (tester) async {
      String? postedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/workspaces' && request.method == 'POST') {
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

      await tester.tap(find.byType(FloatingActionButton));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).first, 'EnvWS');

      // Add env var via the + button (env add is at index 2: FAB=0, mount=1, env=2)
      await tester.enterText(find.byType(TextField).at(3), 'FOO=bar');
      await tester.tap(find.byIcon(Icons.add).at(2));
      await tester.pumpAndSettle();
      expect(find.text('FOO=bar'), findsOneWidget);

      // Add a second env var via Enter key
      await tester.enterText(find.byType(TextField).at(3), 'X=1');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(find.text('X=1'), findsOneWidget);

      // Remove the first env var via X button
      // close icons: mount has none, env has 2 (FOO=bar, X=1)
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

    testWidgets('edit workspace env vars', (tester) async {
      String? putBody;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'env': {'OLD': 'val'},
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/images' && request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'default': 'klangk',
              'allowed': ['klangk']
            }),
            200,
          );
        }
        if (request.url.path == '/workspaces/ws-1' && request.method == 'PUT') {
          putBody = request.body;
          return http.Response('{"status":"updated"}', 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      // Existing env var should be visible
      expect(find.text('OLD=val'), findsOneWidget);

      // Add a new env var via Enter key
      await tester.enterText(find.byType(TextField).at(3), 'NEW=123');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(find.text('NEW=123'), findsOneWidget);

      // Remove the old env var (first X in env section)
      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pumpAndSettle();
      expect(find.text('OLD=val'), findsNothing);

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(putBody, isNotNull);
      final body = jsonDecode(putBody!) as Map<String, dynamic>;
      expect(body['env'], {'NEW': '123'});
    });

    testWidgets('create dialog rejects invalid env var', (tester) async {
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

      // Try adding env var without = sign
      await tester.enterText(find.byType(TextField).at(3), 'NOEQ');
      await tester.tap(find.byIcon(Icons.add).at(2));
      await tester.pumpAndSettle();
      expect(find.textContaining('Expected KEY=VALUE'), findsOneWidget);

      // Try adding env var with empty key
      await tester.enterText(find.byType(TextField).at(3), '=value');
      await tester.tap(find.byIcon(Icons.add).at(2));
      await tester.pumpAndSettle();
      expect(find.textContaining('Key cannot be empty'), findsOneWidget);

      // Valid env var clears error
      await tester.enterText(find.byType(TextField).at(3), 'A=1');
      await tester.tap(find.byIcon(Icons.add).at(2));
      await tester.pumpAndSettle();
      expect(find.textContaining('Key cannot'), findsNothing);
      expect(find.text('A=1'), findsOneWidget);
    });

    testWidgets('edit dialog adds env via button and rejects empty key',
        (tester) async {
      String? putBody;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/images' && request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'default': 'klangk',
              'allowed': ['klangk']
            }),
            200,
          );
        }
        if (request.url.path == '/workspaces/ws-1' && request.method == 'PUT') {
          putBody = request.body;
          return http.Response('{"status":"updated"}', 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      // Try empty key
      await tester.enterText(find.byType(TextField).at(3), '=val');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(find.textContaining('Key cannot be empty'), findsOneWidget);

      // Add valid env via + button
      await tester.enterText(find.byType(TextField).at(3), 'OK=1');
      // The env + button is at index 2 (FAB=0, mount+=1, env+=2)
      await tester.tap(find.byIcon(Icons.add).at(2));
      await tester.pumpAndSettle();
      expect(find.text('OK=1'), findsOneWidget);

      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();

      expect(putBody, isNotNull);
      final body = jsonDecode(putBody!) as Map<String, dynamic>;
      expect(body['env'], {'OK': '1'});
    });

    testWidgets('edit dialog rejects invalid env var', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'My WS',
                'container_id': null,
                'default_command': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/images' && request.method == 'GET') {
          return http.Response(
            jsonEncode({
              'default': 'klangk',
              'allowed': ['klangk']
            }),
            200,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.settings_outlined));
      await tester.pumpAndSettle();

      // Try adding invalid env via Enter key
      await tester.enterText(find.byType(TextField).at(3), 'BAD');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(find.textContaining('Expected KEY=VALUE'), findsOneWidget);

      // Valid env clears error
      await tester.enterText(find.byType(TextField).at(3), 'OK=yes');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();
      expect(find.textContaining('Expected KEY=VALUE'), findsNothing);
      expect(find.text('OK=yes'), findsOneWidget);
    });

    testWidgets('duplicate button opens dialog and duplicates workspace',
        (tester) async {
      var postCalled = false;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          if (postCalled) {
            return http.Response(
              jsonEncode([
                {
                  'id': 'ws-1',
                  'name': 'Original',
                  'container_id': null,
                  'created_at': '2026-05-28',
                },
                {
                  'id': 'ws-2',
                  'name': 'Original-copy',
                  'container_id': null,
                  'created_at': '2026-05-28',
                },
              ]),
              200,
            );
          }
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'Original',
                'container_id': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path == '/workspaces/ws-1/duplicate' &&
            request.method == 'POST') {
          postCalled = true;
          final body = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({
              'id': 'ws-2',
              'name': body['name'],
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

      // Tap the duplicate (copy) button
      await tester.tap(find.byIcon(Icons.copy_outlined));
      await tester.pumpAndSettle();

      expect(find.text('Duplicate Workspace'), findsOneWidget);
      // Default name should be "Original-copy"
      final textField = tester.widget<TextField>(find.byType(TextField));
      expect(textField.controller!.text, 'Original-copy');

      // Submit
      await tester.tap(find.text('Duplicate'));
      await tester.pumpAndSettle();

      expect(postCalled, isTrue);
      expect(find.text('Original-copy'), findsOneWidget);
    });

    testWidgets('duplicate dialog cancel does not create', (tester) async {
      var postCalled = false;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'Original',
                'container_id': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path.contains('/duplicate')) {
          postCalled = true;
          return http.Response('{}', 200);
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.copy_outlined));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(postCalled, isFalse);
    });

    testWidgets('duplicate shows error on name conflict', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'Original',
                'container_id': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path.contains('/duplicate')) {
          return http.Response(
            jsonEncode(
                {'detail': 'A workspace named \'taken\' already exists'}),
            409,
          );
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.copy_outlined));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), 'taken');
      await tester.tap(find.text('Duplicate'));
      await tester.pumpAndSettle();

      expect(find.textContaining('already exists'), findsOneWidget);
    });

    testWidgets('duplicate shows error on exception', (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'Original',
                'container_id': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path.contains('/duplicate')) {
          throw Exception('Network error');
        }
        return http.Response('Not found', 404);
      });

      await tester.pumpWidget(buildPage());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.copy_outlined));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Duplicate'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Error:'), findsOneWidget);
    });

    testWidgets('duplicate dialog submit via Enter key', (tester) async {
      var postCalled = false;
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/workspaces' && request.method == 'GET') {
          return http.Response(
            jsonEncode([
              {
                'id': 'ws-1',
                'name': 'Original',
                'container_id': null,
                'created_at': '2026-05-28',
              },
            ]),
            200,
          );
        }
        if (request.url.path.contains('/duplicate')) {
          postCalled = true;
          return http.Response(
            jsonEncode({
              'id': 'ws-2',
              'name': 'via-enter',
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

      await tester.tap(find.byIcon(Icons.copy_outlined));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), 'via-enter');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(postCalled, isTrue);
    });
  });
}
