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
    bool nixAvailable = false,
    bool? defaultPerHandleHome = true,
    bool sudoAvailable = false,
    bool perHandleHomeAvailable = true,
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
                  nixAvailable: nixAvailable,
                  defaultPerHandleHome: defaultPerHandleHome,
                  sudoAvailable: sudoAvailable,
                  perHandleHomeAvailable: perHandleHomeAvailable,
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
      expect(find.byType(TextField), findsNWidgets(13));
      expect(find.byType(DropdownButtonFormField<String>), findsNWidgets(2));
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

    testWidgets('submits classification_banner when provided (#2768)',
        (tester) async {
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
      final bannerField = find.byWidgetPredicate(
        (w) =>
            w is TextField &&
            w.decoration?.labelText == 'Classification Banner',
      );
      await tester.ensureVisible(bannerField);
      await tester.enterText(bannerField, 'CUI');
      // Submit via Enter in the marking field — exercises its onSubmitted
      // -> _submit() path.
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      expect(postedBody!['classification_banner'], 'CUI');
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
      await tester.ensureVisible(find.byIcon(Icons.add).first);
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
      await tester.ensureVisible(find.byIcon(Icons.add).first);
      await tester.tap(find.byIcon(Icons.add).first);
      await tester.pump();
      expect(find.text('/a:/b'), findsOneWidget);

      await tester.ensureVisible(find.byIcon(Icons.close).first);
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

    testWidgets(
        'egress mode dropdown defaults to interactive and sends selection (#2409)',
        (tester) async {
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

      // Default selection is interactive (the server default).
      expect(find.text('interactive (ask first)'), findsOneWidget);
      // The egress-mode picker is the second DropdownButtonFormField
      // (after the image picker in the General section).
      final egressDropdown = find.byType(DropdownButtonFormField<String>).at(1);
      await tester.ensureVisible(egressDropdown);
      await tester.tap(egressDropdown);
      await tester.pumpAndSettle();
      await tester.tap(find.text('allow (default-permit)').last);
      await tester.pump();

      await tester.enterText(_nameField(), 'Allow');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody!['egress_mode'], 'allow');
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
      // The image picker is the first DropdownButtonFormField (the second
      // is the egress-mode picker added in #2409).
      final imageDropdown = find.byType(DropdownButtonFormField<String>).at(0);
      await tester.ensureVisible(imageDropdown);
      await tester.tap(imageDropdown);
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

    testWidgets('includes rejected domains in body', (tester) async {
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
      await tester.pump();
      await tester.pump();

      final input = find.widgetWithText(TextField, 'evil.example.com');
      await tester.ensureVisible(input);
      await tester.enterText(input, 'evil.example.com');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      await tester.enterText(_nameField(), 'Filtered');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody!['rejected_domains'], ['evil.example.com']);
    });

    testWidgets('rejects a CIDR for rejected domains', (tester) async {
      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      final input = find.widgetWithText(TextField, 'evil.example.com');
      await tester.ensureVisible(input);
      await tester.enterText(input, '10.0.0.0/8');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      // CIDR is meaningless for a name-level NXDOMAIN deny-list.
      expect(
          find.textContaining('CIDR ranges are not supported'), findsOneWidget);
      expect(
        find.byWidgetPredicate(
          (w) => w is SelectableText && (w.data ?? '') == '10.0.0.0/8',
        ),
        findsNothing,
      );
    });

    testWidgets('removes a rejected domain via its close button',
        (tester) async {
      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      // Use a value distinct from the input's hint ('evil.example.com') so
      // find.text matches only the list chip, not the rendered hint.
      final input = find.widgetWithText(TextField, 'evil.example.com');
      await tester.ensureVisible(input);
      await tester.enterText(input, 'blocked.example.com');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();
      expect(find.text('blocked.example.com'), findsOneWidget);

      // The rejected chip's close icon is the last one on screen.
      final closeIcons = find.byIcon(Icons.close);
      await tester.ensureVisible(closeIcons.last);
      await tester.tap(closeIcons.last);
      await tester.pump();

      expect(find.text('blocked.example.com'), findsNothing);
    });

    testWidgets('labels both domains editors with titles (#2508)',
        (tester) async {
      // The allowed-domains editor previously had no title, unlike the
      // rejected-domains editor below it and the settings panel's copy.
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      expect(find.text('Allowed Domains'), findsOneWidget);
      expect(find.text('Rejected Domains'), findsOneWidget);
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
      await tester.ensureVisible(find.byIcon(Icons.close).at(1));
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

      expect(
          find.text('Expected host, host:port, or *.domain'), findsOneWidget);
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
      final closeIcon = find.byIcon(Icons.close);
      await tester.ensureVisible(closeIcon);
      await tester.tap(closeIcon);
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
      final copyIcon = find.byIcon(Icons.copy);
      await tester.ensureVisible(copyIcon);
      await tester.tap(copyIcon);
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

    testWidgets(
      'shows not-enforced notice for rejected domains when netfilter disabled (#2386)',
      (tester) async {
        testAuthHttpClientOverride =
            mockClient((_) async => http.Response('Not found', 404));
        await tester.pumpWidget(buildDialog(netfilterEnabled: false));
        await tester.pump();
        await tester.pump();

        final input = find.widgetWithText(TextField, 'evil.example.com');
        await tester.ensureVisible(input);
        await tester.enterText(input, 'blocked.example.com');
        await tester.testTextInput.receiveAction(TextInputAction.done);
        await tester.pump();

        expect(
          find.textContaining('rejected-domains list will NOT be enforced'),
          findsOneWidget,
        );
      },
    );

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
      await tester.ensureVisible(find.byIcon(Icons.add).first);
      await tester.tap(find.byIcon(Icons.add).first);
      await tester.pump();
      expect(find.textContaining('Expected'), findsOneWidget);

      // Then: valid mount clears error
      await tester.enterText(_mountInput(), '/a:/b');
      await tester.ensureVisible(find.byIcon(Icons.add).first);
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
      // The Per-handle home checkbox (#2721) shows under the deploy
      // ceiling (#3135 — armed by the helper default) — it is the only
      // checkbox when auto-start is not allowed.
      expect(find.text('Per-handle home'), findsOneWidget);
      expect(find.byType(Checkbox), findsOneWidget);
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
      // the dialog, so ensure it's visible before tapping). Scoped to the
      // Auto start tile — the Per-handle home checkbox (#2721) is present
      // too while the ceiling is on.
      final checkbox = find.descendant(
        of: find.widgetWithText(CheckboxListTile, 'Auto start'),
        matching: find.byType(Checkbox),
      );
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

      // Both checkboxes are present (Auto start off, Per-handle home on
      // by default); auto_start stays unsent while the box is unchecked.
      expect(find.byType(Checkbox), findsNWidgets(2));
      await tester.enterText(_nameField(), 'Auto');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      expect(postedBody!.containsKey('auto_start'), isFalse);
    });

    testWidgets(
      'per-handle home checkbox pre-reflects the deploy default, toggles, and omits when unknown (#2721, #2737)',
      (tester) async {
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
        // Deploy default is shared (default_per_handle_home: false).
        await tester.pumpWidget(buildDialog(defaultPerHandleHome: false));
        await tester.pump(); // post-frame callback
        await tester.pump(); // dialog renders

        final checkbox = find.descendant(
          of: find.widgetWithText(CheckboxListTile, 'Per-handle home'),
          matching: find.byType(Checkbox),
        );
        expect(checkbox, findsOneWidget);
        expect(tester.widget<Checkbox>(checkbox).value, isFalse);

        // Untouched form submits the deploy default (shared).
        await tester.enterText(_nameField(), 'Shared');
        await tester.tap(find.text('Create'));
        await tester.pump();
        await tester.pump();
        expect(postedBody!['per_handle_home'], false);

        // Toggle the checkbox ON (exercises the onChanged handler) and
        // submit — the flipped value reaches the POST body.
        postedBody = null;
        await tester.pumpWidget(buildDialog(defaultPerHandleHome: false));
        await tester.pump();
        await tester.pump();
        await tester.ensureVisible(checkbox);
        await tester.tap(checkbox);
        await tester.pump();
        expect(tester.widget<Checkbox>(checkbox).value, isTrue);
        await tester.enterText(_nameField(), 'Toggled');
        await tester.tap(find.text('Create'));
        await tester.pump();
        await tester.pump();
        expect(postedBody!['per_handle_home'], true);

        // Per-handle-default dialog: submitted as true even untouched —
        // an untouched form equals a silent POST against that deploy.
        postedBody = null;
        await tester.pumpWidget(buildDialog(defaultPerHandleHome: true));
        await tester.pump();
        await tester.pump();
        await tester.enterText(_nameField(), 'PerHandle');
        await tester.tap(find.text('Create'));
        await tester.pump();
        await tester.pump();
        expect(postedBody!['per_handle_home'], true);

        // Unknown default (fetch failed / old server): no tile, and the
        // field is omitted so the server applies its own default.
        postedBody = null;
        await tester.pumpWidget(buildDialog(defaultPerHandleHome: null));
        await tester.pump();
        await tester.pump();
        expect(find.text('Per-handle home'), findsNothing);
        await tester.enterText(_nameField(), 'Unknown');
        await tester.tap(find.text('Create'));
        await tester.pump();
        await tester.pump();
        expect(postedBody!.containsKey('per_handle_home'), isFalse);
      },
    );

    testWidgets(
      'per-handle home checkbox hidden while the deploy ceiling is off (#3135)',
      (tester) async {
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
        // Ceiling off — even with a known true deploy default the tile is
        // hidden and the field omitted: every workspace gets the shared
        // home regardless (the server clamps a stored true at start).
        await tester.pumpWidget(buildDialog(
          defaultPerHandleHome: true,
          perHandleHomeAvailable: false,
        ));
        await tester.pump(); // post-frame callback
        await tester.pump(); // dialog renders

        expect(find.text('Per-handle home'), findsNothing);
        await tester.enterText(_nameField(), 'Clamped');
        await tester.tap(find.text('Create'));
        await tester.pump();
        await tester.pump();
        expect(postedBody!.containsKey('per_handle_home'), isFalse);
      },
    );

    testWidgets('submits settings via resource field Enter key',
        (tester) async {
      Map<String, dynamic>? postedBody;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({'id': 'ws-1', 'name': 'R', 'created_at': ''}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      await tester.pumpWidget(buildDialog());
      await tester.pump();
      await tester.pump();

      await tester.enterText(_nameField(), 'R');

      final idleField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.labelText == 'Idle Timeout (s)',
      );
      final cpuField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.labelText == 'CPU Limit',
      );
      final memField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.labelText == 'Memory Limit',
      );
      final pidsField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.labelText == 'PIDs Limit',
      );
      final tmpField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.labelText == '/tmp size',
      );

      await tester.ensureVisible(idleField);
      await tester.enterText(idleField, '600');
      await tester.ensureVisible(cpuField);
      await tester.enterText(cpuField, '1.5');
      await tester.ensureVisible(memField);
      await tester.enterText(memField, '4g');
      await tester.ensureVisible(pidsField);
      await tester.enterText(pidsField, '256');
      await tester.ensureVisible(tmpField);
      await tester.enterText(tmpField, '2g');

      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      expect(postedBody!['settings'], {
        'idle_timeout': 600,
        'cpu_limit': 1.5,
        'memory_limit': '4g',
        'pids_limit': 256,
        'tmp_size': '2g',
      });
    });

    testWidgets('hides Nix checkbox when not available', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog()); // nixAvailable defaults false
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      expect(find.text('Mount /nix dir'), findsNothing);
    });

    testWidgets('shows Nix checkbox when nixAvailable', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog(nixAvailable: true));
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      expect(find.widgetWithText(CheckboxListTile, 'Mount /nix dir'),
          findsOneWidget);
    });

    testWidgets('sends settings.nix when Nix toggled on', (tester) async {
      Map<String, dynamic>? postedBody;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({'id': 'ws-1', 'name': 'Nix', 'created_at': ''}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      await tester.pumpWidget(buildDialog(nixAvailable: true));
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      final nix = find.widgetWithText(CheckboxListTile, 'Mount /nix dir');
      await tester.ensureVisible(nix);
      await tester.tap(nix);
      await tester.pump();
      await tester.enterText(_nameField(), 'Nix');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      expect(postedBody!['settings'], {'nix': true});
    });

    testWidgets('hides sudo checkbox when not available', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog()); // sudoAvailable defaults false
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      expect(find.text('Allow sudo'), findsNothing);
    });

    testWidgets('shows sudo checkbox when sudoAvailable', (tester) async {
      testAuthHttpClientOverride = mockClient(
        (_) async => http.Response('Not found', 404),
      );
      await tester.pumpWidget(buildDialog(sudoAvailable: true));
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      expect(
          find.widgetWithText(CheckboxListTile, 'Allow sudo'), findsOneWidget);
    });

    testWidgets('defaults to unchecked and sends allow_sudo=false (#3046)',
        (tester) async {
      Map<String, dynamic>? postedBody;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({'id': 'ws-1', 'name': 'Locked', 'created_at': ''}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      await tester.pumpWidget(buildDialog(sudoAvailable: true));
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      final sudo = find.widgetWithText(CheckboxListTile, 'Allow sudo');
      await tester.ensureVisible(sudo);
      // #3046: starts unchecked (locked down) — no tap needed.
      expect((tester.widget(sudo) as CheckboxListTile).value, isFalse);
      await tester.enterText(_nameField(), 'Locked');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      // The lock-down is emitted by default; opting in emits an explicit
      // true (#3047 — the bag is the sole posture source, absent = off;
      // the deploy flag stays the ceiling).
      expect(postedBody!['settings'], {'allow_sudo': false});
    });

    testWidgets('opting in (check) sends allow_sudo=true (#3047)',
        (tester) async {
      Map<String, dynamic>? postedBody;
      testAuthHttpClientOverride = mockClient((request) async {
        if (request.url.path == '/api/v1/workspaces' &&
            request.method == 'POST') {
          postedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({'id': 'ws-1', 'name': 'OptIn', 'created_at': ''}),
            200,
          );
        }
        return http.Response('Not found', 404);
      });
      await tester.pumpWidget(buildDialog(sudoAvailable: true));
      await tester.pump(); // post-frame callback
      await tester.pump(); // dialog renders

      final sudo = find.widgetWithText(CheckboxListTile, 'Allow sudo');
      await tester.ensureVisible(sudo);
      await tester.tap(sudo); // starts unchecked — check opts in
      await tester.pump();
      await tester.enterText(_nameField(), 'OptIn');
      await tester.tap(find.text('Create'));
      await tester.pump();
      await tester.pump();

      expect(postedBody, isNotNull);
      // #3047: the bag is the sole posture source — opting in emits an
      // explicit true (an absent key means OFF).
      expect(postedBody!['settings'], {'allow_sudo': true});
    });
  });
}
