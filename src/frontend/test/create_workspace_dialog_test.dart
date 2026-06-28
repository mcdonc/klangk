import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/workspace/create_workspace_dialog.dart';
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

  /// Build the dialog via showDialog so Navigator.pop works on submit.
  Widget buildDialog({
    AuthService? auth,
    String defaultImage = 'klangk-pi',
    List<String>? allowedImages,
  }) {
    final a = auth ?? AuthService();
    return MaterialApp(
      home: Scaffold(
        body: Builder(
          builder: (context) {
            // Auto-open the dialog on first build.
            WidgetsBinding.instance.addPostFrameCallback((_) {
              showDialog(
                context: context,
                builder: (_) => CreateWorkspaceDialog(
                  auth: a,
                  defaultImage: defaultImage,
                  allowedImages: allowedImages ?? [defaultImage, 'klangk-full'],
                ),
              );
            });
            return const SizedBox.shrink();
          },
        ),
      ),
    );
  }

  group('CreateWorkspaceDialog', () {
    testWidgets('renders title, fields, and buttons', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      expect(find.text('New Workspace'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
      expect(find.text('Create'), findsOneWidget);
      expect(find.byType(TextField), findsNWidgets(4));
      expect(find.byType(DropdownButtonFormField<String>), findsOneWidget);
    });

    testWidgets('does not submit with empty name', (tester) async {
      var postCalled = false;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.method == 'POST') postCalled = true;
        return http.Response('{}', 200);
      });
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      await tester.tap(find.text('Create'));
      await tester.pump();

      expect(postCalled, isFalse);
    });

    testWidgets('submits workspace on Create tap', (tester) async {
      Map<String, dynamic>? postedBody;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({'id': 'ws-1', 'name': 'My WS', 'created_at': ''}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      await tester.enterText(find.byType(TextField).first, 'My WS');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      expect(postedBody!['name'], 'My WS');
    });

    testWidgets('shows error on failure', (tester) async {
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.method == 'POST') {
          return http.Response(
            jsonEncode({'detail': 'Name already taken'}),
            409,
          );
        }
        return http.Response('Not found', 404);
      });
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      await tester.enterText(find.byType(TextField).first, 'Dup');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(find.text('Name already taken'), findsOneWidget);
    });

    testWidgets('adds mount via add button', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      // 3rd TextField is mount input
      await tester.enterText(find.byType(TextField).at(2), '/host:/container');
      await tester.tap(find.byIcon(Icons.add).first);
      await tester.pump();

      expect(find.text('/host:/container'), findsOneWidget);
    });

    testWidgets('rejects invalid mount', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      await tester.enterText(find.byType(TextField).at(2), 'invalid');
      await tester.tap(find.byIcon(Icons.add).first);
      await tester.pump();

      expect(find.textContaining('Expected source:dest'), findsOneWidget);
    });

    testWidgets('removes mount via close button', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      await tester.enterText(find.byType(TextField).at(2), '/a:/b');
      await tester.tap(find.byIcon(Icons.add).first);
      await tester.pump();
      expect(find.text('/a:/b'), findsOneWidget);

      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pump();
      expect(find.text('/a:/b'), findsNothing);
    });

    testWidgets('adds env var via add button', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      // 4th TextField is env input
      await tester.enterText(find.byType(TextField).at(3), 'FOO=bar');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pump();

      expect(find.text('FOO=bar'), findsOneWidget);
    });

    testWidgets('rejects env var without equals', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      await tester.enterText(find.byType(TextField).at(3), 'NOEQUALS');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pump();

      expect(find.text('Expected KEY=VALUE format'), findsOneWidget);
    });

    testWidgets('rejects env var with empty key', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      await tester.enterText(find.byType(TextField).at(3), '=value');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pump();

      expect(find.text('Key cannot be empty'), findsOneWidget);
    });

    testWidgets('removes env var via close button', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      await tester.enterText(find.byType(TextField).at(3), 'MYKEY=val');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();
      expect(find.widgetWithText(SelectableText, 'MYKEY=val'), findsOneWidget);

      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pumpAndSettle();
      expect(find.widgetWithText(SelectableText, 'MYKEY=val'), findsNothing);
    });

    testWidgets('mount added via Enter key submission', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      await tester.enterText(find.byType(TextField).at(2), '/a:/b');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(find.text('/a:/b'), findsOneWidget);
    });

    testWidgets('image dropdown changes selection', (tester) async {
      Map<String, dynamic>? postedBody;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.method == 'POST') {
          postedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({'id': 'ws-1', 'name': 'x', 'created_at': ''}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      // Open dropdown and select non-default
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('klangk-full').last);
      await tester.pump();

      await tester.enterText(find.byType(TextField).first, 'Custom');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody!['image'], 'klangk-full');
    });

    testWidgets('default image not sent in body', (tester) async {
      Map<String, dynamic>? postedBody;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.method == 'POST') {
          postedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({'id': 'ws-1', 'name': 'x', 'created_at': ''}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      await tester.enterText(find.byType(TextField).first, 'Default Img');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody!.containsKey('image'), isFalse);
    });

    testWidgets('clears mount error on successful add', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      // First: invalid mount to trigger error
      await tester.enterText(find.byType(TextField).at(2), 'bad');
      await tester.tap(find.byIcon(Icons.add).first);
      await tester.pump();
      expect(find.textContaining('Expected'), findsOneWidget);

      // Then: valid mount clears error
      await tester.enterText(find.byType(TextField).at(2), '/a:/b');
      await tester.tap(find.byIcon(Icons.add).first);
      await tester.pump();
      expect(find.textContaining('Expected'), findsNothing);
    });

    testWidgets('clears env error on successful add', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      final envInput = find.widgetWithText(TextField, 'KEY=VALUE');

      // Invalid env
      await tester.enterText(envInput, 'bad');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();
      expect(find.text('Expected KEY=VALUE format'), findsOneWidget);

      // Valid env clears error
      await tester.enterText(envInput, 'OK=yes');
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();
      expect(find.text('Expected KEY=VALUE format'), findsNothing);
    });
  });
}
