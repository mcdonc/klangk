import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/workspace/import_workspace_dialog.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
    testPickFileBytesOverride = null;
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
    testPickFileBytesOverride = null;
  });

  http.Client mockClient(Future<http.Response> Function(http.Request) handler) {
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
            'permissions': {},
            'groups': [],
          }),
          200,
        );
      }
      return handler(request);
    });
  }

  Widget buildDialog({AuthService? auth}) {
    final a = auth ?? AuthService();
    return MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) {
            WidgetsBinding.instance.addPostFrameCallback((_) {
              showDialog(
                context: context,
                builder: (_) => ImportWorkspaceDialog(auth: a),
              );
            });
            return const SizedBox.shrink();
          },
        ),
      ),
    );
  }

  group('ImportWorkspaceDialog', () {
    testWidgets('renders title, buttons, and file picker', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      expect(find.text('Import Workspace'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
      expect(find.text('Import'), findsOneWidget);
      expect(find.text('Select .tar.gz file'), findsOneWidget);
      expect(find.byType(TextField), findsOneWidget);
    });

    testWidgets('Import button disabled without file', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      final importButton = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Import'),
      );
      expect(importButton.onPressed, isNull);
    });

    testWidgets('file picker sets bytes and shows size', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      testPickFileBytesOverride = ({String accept = ''}) async {
        return List<int>.filled(2048, 0);
      };

      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('Select .tar.gz file'));
      await tester.pumpAndSettle();

      expect(find.text('workspace.tar.gz'), findsOneWidget);
      expect(find.text('2 KB'), findsOneWidget);

      // Import button should now be enabled
      final importButton = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Import'),
      );
      expect(importButton.onPressed, isNotNull);
    });

    testWidgets('file picker returning null does not change state',
        (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      testPickFileBytesOverride = ({String accept = ''}) async => null;

      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('Select .tar.gz file'));
      await tester.pumpAndSettle();

      expect(find.text('Select .tar.gz file'), findsOneWidget);
      final importButton = tester.widget<FilledButton>(
        find.widgetWithText(FilledButton, 'Import'),
      );
      expect(importButton.onPressed, isNull);
    });

    testWidgets('successful import closes dialog with true', (tester) async {
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces/import') {
          return http.Response(
            jsonEncode({'id': 'ws-imp', 'name': 'Imported'}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      testPickFileBytesOverride = ({String accept = ''}) async {
        return [1, 2, 3];
      };

      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      // Pick file
      await tester.tap(find.text('Select .tar.gz file'));
      await tester.pumpAndSettle();

      // Tap Import
      await tester.tap(find.text('Import'));
      await tester.pumpAndSettle();

      // Dialog should be closed
      expect(find.text('Import Workspace'), findsNothing);
    });

    testWidgets('import with custom name sends query param', (tester) async {
      String? requestedUrl;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces/import') {
          requestedUrl = request.url.toString();
          return http.Response(
            jsonEncode({'id': 'ws-imp', 'name': 'Custom'}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      testPickFileBytesOverride = ({String accept = ''}) async => [1, 2, 3];

      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('Select .tar.gz file'));
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField), 'Custom Name');
      await tester.tap(find.text('Import'));
      await tester.pumpAndSettle();

      expect(requestedUrl, contains('name=Custom%20Name'));
    });

    testWidgets('import failure shows error message', (tester) async {
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces/import') {
          return http.Response(
            jsonEncode({'detail': 'Archive is corrupt'}),
            400,
          );
        }
        return http.Response('Not found', 404);
      });
      testPickFileBytesOverride = ({String accept = ''}) async => [1, 2, 3];

      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('Select .tar.gz file'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Import'));
      await tester.pumpAndSettle();

      expect(find.text('Import failed: Archive is corrupt'), findsOneWidget);
    });

    testWidgets('import failure without detail shows status code',
        (tester) async {
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces/import') {
          return http.Response('not json', 500);
        }
        return http.Response('Not found', 404);
      });
      testPickFileBytesOverride = ({String accept = ''}) async => [1, 2, 3];

      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('Select .tar.gz file'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Import'));
      await tester.pumpAndSettle();

      expect(find.text('Import failed: 500'), findsOneWidget);
    });

    testWidgets('network exception shows generic error', (tester) async {
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces/import') {
          throw Exception('Connection refused');
        }
        return http.Response('Not found', 404);
      });
      testPickFileBytesOverride = ({String accept = ''}) async => [1, 2, 3];

      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('Select .tar.gz file'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Import'));
      await tester.pumpAndSettle();

      expect(find.text('Import failed'), findsOneWidget);
    });

    testWidgets('cancel closes dialog', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(find.text('Import Workspace'), findsNothing);
    });

    testWidgets('import failure with null detail shows status code',
        (tester) async {
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces/import') {
          return http.Response(
            jsonEncode({'detail': null}),
            400,
          );
        }
        return http.Response('Not found', 404);
      });
      testPickFileBytesOverride = ({String accept = ''}) async => [1, 2, 3];

      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('Select .tar.gz file'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Import'));
      await tester.pumpAndSettle();

      expect(find.text('Import failed: 400'), findsOneWidget);
    });

    testWidgets('201 status also closes dialog', (tester) async {
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces/import') {
          return http.Response(
            jsonEncode({'id': 'ws-imp', 'name': 'Created'}),
            201,
          );
        }
        return http.Response('Not found', 404);
      });
      testPickFileBytesOverride = ({String accept = ''}) async => [1, 2, 3];

      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      await tester.tap(find.text('Select .tar.gz file'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Import'));
      await tester.pumpAndSettle();

      expect(find.text('Import Workspace'), findsNothing);
    });
  });
}
