import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/workspace/create_workspace_dialog.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Identity-based finders for the dialog's TextFields, matched by their
/// InputDecoration rather than by position so reordering fields can't break
/// these tests - see #1124.
Finder _nameField() => find.byWidgetPredicate(
      (w) => w is TextField && w.decoration?.labelText == 'Name',
    );
Finder _mountInput() => find.byWidgetPredicate(
      (w) =>
          w is TextField &&
          w.decoration?.hintText == '/host/path:/container/path',
    );
Finder _envInput() => find.byWidgetPredicate(
      (w) => w is TextField && w.decoration?.hintText == 'KEY=VALUE',
    );

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
    bool allowAutostart = false,
    List<String> defaultAllowedDomains = const [],
    bool netfilterEnabled = false,
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
                  allowAutostart: allowAutostart,
                  defaultAllowedDomains: defaultAllowedDomains,
                  netfilterEnabled: netfilterEnabled,
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
      expect(find.byType(TextField), findsNWidgets(6));
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

      await tester.enterText(_nameField(), 'My WS');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      expect(postedBody!['name'], 'My WS');
    });

    testWidgets('submits health_check when provided', (tester) async {
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

      await tester.enterText(_nameField(), 'My WS');
      final healthCheckField = find.byWidgetPredicate(
        (w) =>
            w is TextField && w.decoration?.labelText == 'Health Check Command',
      );
      await tester.ensureVisible(healthCheckField);
      await tester.enterText(
        healthCheckField,
        'curl -sf http://localhost:8080/health',
      );
      // Submit via Enter key in the health-check field — exercises its
      // onSubmitted -> _submit() path (the 'Create' tap is covered by
      // the basic submit test above).
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      expect(
          postedBody!['health_check'], 'curl -sf http://localhost:8080/health');
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

      await tester.enterText(_nameField(), 'Dup');
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
      await tester.enterText(_mountInput(), '/host:/container');
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

      await tester.enterText(_mountInput(), 'invalid');
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

      await tester.enterText(_mountInput(), '/a:/b');
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
      await tester.enterText(_envInput(), 'FOO=bar');
      await tester.ensureVisible(find.byIcon(Icons.add).at(1));
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

      await tester.enterText(_envInput(), 'NOEQUALS');
      await tester.ensureVisible(find.byIcon(Icons.add).at(1));
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

      await tester.enterText(_envInput(), '=value');
      await tester.ensureVisible(find.byIcon(Icons.add).at(1));
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

      await tester.enterText(_envInput(), 'MYKEY=val');
      await tester.ensureVisible(find.byIcon(Icons.add).at(1));
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

      await tester.enterText(_mountInput(), '/a:/b');
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

      // Open dropdown and select non-default (now near the dialog's
      // bottom after the field reordering, so scroll it into view first).
      await tester.ensureVisible(find.byType(DropdownButtonFormField<String>));
      await tester.tap(find.byType(DropdownButtonFormField<String>));
      await tester.pumpAndSettle();
      await tester.tap(find.text('klangk-full').last);
      await tester.pump();

      await tester.enterText(_nameField(), 'Custom');
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

      await tester.enterText(_nameField(), 'Default Img');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody!.containsKey('image'), isFalse);
      // allowed_domains is omitted entirely when none are added.
      expect(postedBody!.containsKey('allowed_domains'), isFalse);
    });

    testWidgets('includes allowed domains in body', (tester) async {
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

      final input = find.widgetWithText(TextField, 'github.com:443');
      await tester.ensureVisible(input);
      await tester.enterText(input, 'github.com:443');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      await tester.enterText(_nameField(), 'Filtered');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody!['allowed_domains'], ['github.com:443']);
    });

    testWidgets('pre-fills allowed domains from the deploy default',
        (tester) async {
      // #1365: the editor inherits KLANGKD_NETFILTER_DEFAULT_DOMAINS so a
      // new workspace starts from the deployer's floor. The creator's
      // edits replace (not merge with) the default.
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
      await tester.pumpWidget(buildDialog(
        defaultAllowedDomains: ['github.com:443', 'pypi.org'],
      ));
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      // Both defaults render as chips (SelectableText, not the input's
      // hintText which also carries 'github.com:443').
      expect(
        find.byWidgetPredicate(
          (w) => w is SelectableText && (w.data ?? '') == 'github.com:443',
        ),
        findsOneWidget,
      );
      expect(find.text('pypi.org'), findsOneWidget);

      // Submitting without edits sends the inherited default as the
      // workspace's own allowed_domains.
      await tester.enterText(_nameField(), 'Inherited');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody!['allowed_domains'], ['github.com:443', 'pypi.org']);
    });

    testWidgets('creator edits replace the inherited default', (tester) async {
      // Removing a chip and adding a new one produces exactly the edited
      // set — the default is not unioned back in.
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
      await tester.pumpWidget(buildDialog(
        defaultAllowedDomains: ['github.com:443', 'pypi.org'],
      ));
      await tester.pump();
      await tester.pump();

      // Remove pypi.org (the second chip's close button).
      await tester.tap(find.byIcon(Icons.close).at(1));
      await tester.pump();
      expect(find.text('pypi.org'), findsNothing);

      // Add a new domain.
      final input = find.widgetWithText(TextField, 'github.com:443');
      await tester.ensureVisible(input);
      await tester.enterText(input, 'added.io');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      await tester.enterText(_nameField(), 'Edited');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      // The default's pypi.org is gone; the added domain is present; the
      // unedited default entry survives. Pure override, no merge.
      expect(postedBody!['allowed_domains'], ['github.com:443', 'added.io']);
    });

    testWidgets('rejects an invalid allowed domain spec', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      final input = find.widgetWithText(TextField, 'github.com:443');
      await tester.ensureVisible(input);
      await tester.enterText(input, 'bad spec');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(find.text('Expected host or host:port'), findsOneWidget);
      // The bad spec did not become a list item.
      expect(
        find.byWidgetPredicate(
          (w) => w is SelectableText && (w.data ?? '') == 'bad spec',
        ),
        findsNothing,
      );
    });

    testWidgets('removes an allowed domain via its close button',
        (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      final input = find.widgetWithText(TextField, 'github.com:443');
      await tester.ensureVisible(input);
      await tester.enterText(input, 'example.com:443');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      expect(find.text('example.com:443'), findsOneWidget);

      // The only close icon on screen is this chip's remove button
      // (no mounts/env chips were added).
      await tester.tap(find.byIcon(Icons.close));
      await tester.pump();
      expect(find.text('example.com:443'), findsNothing);
    });

    testWidgets('copies an allowed domain via its copy button', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      final input = find.widgetWithText(TextField, 'github.com:443');
      await tester.ensureVisible(input);
      await tester.enterText(input, 'example.com:443');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      // The only copy icon on screen is this chip's copy button.
      await tester.tap(find.byIcon(Icons.copy));
      await tester.pump();
      // Tapping copy fired the chip's onPressed (Clipboard.setData) —
      // the chip is otherwise unchanged.
      expect(find.text('example.com:443'), findsOneWidget);
    });

    testWidgets('rejects port > 65535', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      final input = find.widgetWithText(TextField, 'github.com:443');
      await tester.ensureVisible(input);
      await tester.enterText(input, 'a.com:99999');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(find.textContaining('65535'), findsOneWidget);
    });

    testWidgets('shows not-enforced notice when netfilter disabled',
        (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog(
        defaultAllowedDomains: ['github.com:443'],
        netfilterEnabled: false,
      ));
      await tester.pump();
      await tester.pump();

      expect(find.textContaining('NOT be enforced'), findsOneWidget);
    });

    testWidgets('hides not-enforced notice when netfilter enabled',
        (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog(
        defaultAllowedDomains: ['github.com:443'],
        netfilterEnabled: true,
      ));
      await tester.pump();
      await tester.pump();

      expect(find.textContaining('NOT be enforced'), findsNothing);
    });

    testWidgets('clears mount error on successful add', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      // First: invalid mount to trigger error
      await tester.enterText(_mountInput(), 'bad');
      await tester.tap(find.byIcon(Icons.add).first);
      await tester.pump();
      expect(find.textContaining('Expected'), findsOneWidget);

      // Then: valid mount clears error
      await tester.enterText(_mountInput(), '/a:/b');
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
      await tester.ensureVisible(find.byIcon(Icons.add).at(1));
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();
      expect(find.text('Expected KEY=VALUE format'), findsOneWidget);

      // Valid env clears error
      await tester.enterText(envInput, 'OK=yes');
      await tester.ensureVisible(find.byIcon(Icons.add).at(1));
      await tester.tap(find.byIcon(Icons.add).at(1));
      await tester.pumpAndSettle();
      expect(find.text('Expected KEY=VALUE format'), findsNothing);
    });

    testWidgets('hides auto-start checkbox when not allowed', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog()); // allowAutostart defaults false
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      expect(find.text('Auto start'), findsNothing);
      expect(find.byType(Checkbox), findsNothing);
    });

    testWidgets('shows auto-start checkbox and sends auto_start when allowed',
        (tester) async {
      Map<String, dynamic>? postedBody;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({'id': 'ws-1', 'name': 'Auto', 'created_at': ''}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      await tester.pumpWidget(buildDialog(allowAutostart: true));
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      expect(find.text('Auto start'), findsOneWidget);
      // Toggle the checkbox on, then submit (checkbox is at the bottom of
      // the dialog, so ensure it's visible before tapping).
      final checkbox = find.byType(Checkbox);
      await tester.ensureVisible(checkbox);
      await tester.tap(checkbox);
      await tester.pump();
      await tester.enterText(_nameField(), 'Auto');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      expect(postedBody!['auto_start'], true);
    });

    testWidgets('does not send auto_start when checkbox is left off',
        (tester) async {
      Map<String, dynamic>? postedBody;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({'id': 'ws-1', 'name': 'Auto', 'created_at': ''}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      await tester.pumpWidget(buildDialog(allowAutostart: true));
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      // Checkbox is present but unchecked.
      expect(find.byType(Checkbox), findsOneWidget);
      await tester.enterText(_nameField(), 'Auto');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      expect(postedBody!.containsKey('auto_start'), isFalse);
    });
  });
}
