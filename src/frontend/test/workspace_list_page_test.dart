import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
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
  });
}
