import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/workspace/workspace_settings_panel.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:provider/provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Identity-based finders for the settings panel's add-row inputs, matched
/// by hint rather than by position so reordering can't break these tests -
/// see #1124.
Finder _mountInput() => find.byWidgetPredicate(
      (w) =>
          w is TextField &&
          w.decoration?.hintText == '/host/path:/container/path',
    );
Finder _envInput() => find.byWidgetPredicate(
      (w) => w is TextField && w.decoration?.hintText == 'KEY=VALUE',
    );

/// A WsClient whose sendShutdownContainer we can observe, for the danger-zone
/// confirm dialog test.
class _MockWsClient extends WsClient {
  bool shutdownSent = false;
  @override
  void sendShutdownContainer() => shutdownSent = true;
}

/// JWT with sub=test-user (logged in) so AuthService.isLoggedIn is true.
String _jwt() {
  final header = base64Url
      .encode(utf8.encode(jsonEncode({'alg': 'HS256', 'typ': 'JWT'})))
      .replaceAll('=', '');
  final body = base64Url
      .encode(
        utf8.encode(jsonEncode({'sub': 'test-user', 'email': 't@x.com'})),
      )
      .replaceAll('=', '');
  return '$header.$body.sig';
}

/// Default workspace the panel loads.
const _wsId = 'ws-1';
const _workspace = {
  'id': _wsId,
  'name': 'my-ws',
  'image': 'klangk-pi',
  'service_command': 'pi',
  'mounts': <String>['/host:/cont'],
  'env': <String, String>{'FOO': 'bar'},
};

/// Build a MockClient handler. Extra routes can be tuned via the params;
/// the defaults serve the workspace list, images, and a 200 PUT on save.
http.Client _client({
  Map<String, dynamic>? workspace,
  Map<String, String>? saveResponse,
  int saveStatus = 200,
  int exportStatus = 200,
  bool imagesFail = false,
  int transferStatus = 200,
  Map<String, dynamic>? transferResponse,
  List<Map<String, dynamic>>? searchResults,
}) {
  final ws = (workspace ?? _workspace);
  return MockClient((request) async {
    final p = request.url.path;
    if (p == '/api/v1/workspaces') {
      return http.Response(jsonEncode([ws]), 200);
    }
    if (p == '/api/v1/workspaces/shared') {
      return http.Response(jsonEncode([]), 200);
    }
    if (p == '/api/v1/images') {
      if (imagesFail) return http.Response('boom', 500);
      return http.Response(
        jsonEncode({
          'default': 'klangk-pi',
          'allowed': ['klangk-pi', 'other:latest'],
        }),
        200,
      );
    }
    if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
      return http.Response(
        jsonEncode(saveResponse ?? {'status': 'updated'}),
        saveStatus,
      );
    }
    if (p == '/api/v1/workspaces/$_wsId/export' && request.method == 'GET') {
      if (exportStatus != 200) return http.Response('err', exportStatus);
      return http.Response.bytes([1, 2, 3], 200);
    }
    if (p == '/api/v1/workspaces/$_wsId/transfer' && request.method == 'POST') {
      return http.Response(
        jsonEncode(transferResponse ?? {'id': _wsId, 'user_id': 'new-owner'}),
        transferStatus,
      );
    }
    if (p == '/api/v1/users/search') {
      return http.Response(
        jsonEncode(searchResults ?? []),
        200,
      );
    }
    return http.Response('not found', 404);
  });
}

Widget _buildPanel({WsClient? wsClient}) => MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProvider.value(value: wsClient ?? WsClient()),
      ],
      child: const MaterialApp(
        home: Scaffold(body: WorkspaceSettingsPanel(workspaceId: _wsId)),
      ),
    );

/// Scroll a finder into view then tap it. The settings panel is a
/// SingleChildScrollView, so Export / Shut Down (near the bottom) are
/// off-screen until scrolled to — `tester.tap` on an off-screen widget
/// does not register.
Future<void> _scrollToAndTap(WidgetTester tester, Finder f) async {
  await tester.ensureVisible(f);
  await tester.pumpAndSettle();
  await tester.tap(f);
  await tester.pump();
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({'klangk_jwt': _jwt()});
    testAuthHttpClientOverride = _client();
  });

  tearDown(() {
    testBaseUrlOverride = null;
    testAuthHttpClientOverride = null;
  });

  group('WorkspaceSettingsPanel load + render', () {
    testWidgets('renders config fields populated from the workspace',
        (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('Workspace Configuration'), findsOneWidget);
      // Name field is pre-filled.
      expect(find.text('my-ws'), findsOneWidget);
      // Service command is pre-filled.
      expect(find.text('pi'), findsOneWidget);
      // Mounts/env from the workspace are listed.
      expect(find.text('/host:/cont'), findsOneWidget);
      expect(find.text('FOO=bar'), findsOneWidget);
    });

    testWidgets('shows error view when workspace not found', (tester) async {
      // Workspace list omits the panel's workspace id.
      testAuthHttpClientOverride = _client(
        workspace: {'id': 'other', 'name': 'x', 'image': 'klangk-pi'},
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('Workspace not found'), findsOneWidget);
    });

    testWidgets('still renders when images endpoint fails (falls back)',
        (tester) async {
      testAuthHttpClientOverride = _client(imagesFail: true);
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Falls back to default image; panel still renders.
      expect(find.text('Workspace Configuration'), findsOneWidget);
    });
  });

  group('mounts editor', () {
    testWidgets('adds a valid mount', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await tester.enterText(
        _mountInput(), // mounts add-row input
        '/etc:/etc',
      );
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(find.text('/etc:/etc'), findsOneWidget);
    });

    testWidgets('rejects a mount without a colon (error, not added)',
        (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await tester.enterText(
        _mountInput(),
        'no-colon',
      );
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      // The validation error is shown...
      expect(find.text('Expected host:container format'), findsOneWidget);
      // ...and the bad value did not become a list item. The input field
      // retains the typed text (controller is not cleared on error), so
      // assert via SelectableText (list items), not find.text (input too).
      expect(
        find.byWidgetPredicate(
          (w) => w is SelectableText && (w.data ?? '') == 'no-colon',
        ),
        findsNothing,
      );
    });

    testWidgets('removes a mount via its close button', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('/host:/cont'), findsOneWidget);
      // First close icon is the existing mount's remove button.
      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pump();

      expect(find.text('/host:/cont'), findsNothing);
    });
  });

  group('env vars editor', () {
    testWidgets('adds a valid KEY=VALUE', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Env add-row is the last TextField.
      await tester.enterText(
        _envInput(),
        'BAR=baz',
      );
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(find.text('BAR=baz'), findsOneWidget);
    });

    testWidgets('rejects an env var without =', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await tester.enterText(_envInput(), 'NOEQUALS');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(find.text('Expected KEY=VALUE format'), findsOneWidget);
      expect(
        find.byWidgetPredicate(
          (w) => w is SelectableText && (w.data ?? '') == 'NOEQUALS',
        ),
        findsNothing,
      );
    });

    testWidgets('rejects an empty key', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await tester.enterText(_envInput(), '=val');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(find.text('Key cannot be empty'), findsOneWidget);
    });

    testWidgets('removes an env var via its close button', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('FOO=bar'), findsOneWidget);
      // Close icons: [mount-remove, mount-copy, env-remove, env-copy].
      // The env-remove is the close icon after the mounts section.
      final closes = find.byIcon(Icons.close);
      await tester.tap(closes.last);
      await tester.pump();

      expect(find.text('FOO=bar'), findsNothing);
    });
  });

  group('save', () {
    testWidgets('save success shows "Settings saved" message', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      // Pump a few frames to let the async PUT + setState land, without
      // settling through the 2s Future.delayed that auto-clears the msg.
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      // Advance the clock past the 2s auto-clear Future.delayed so its
      // timer fires (clearing the message) and none is left pending at
      // dispose — flutter_test fails on pending timers. pumpAndSettle
      // alone won't fire it (a timer isn't a scheduled frame).
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets('save sends health_check when a command is set',
        (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/workspaces') {
          return http.Response(jsonEncode([_workspace]), 200);
        }
        if (p == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode([]), 200);
        }
        if (p == '/api/v1/images') {
          return http.Response(
            jsonEncode({
              'default': 'klangk-pi',
              'allowed': ['klangk-pi', 'other:latest'],
            }),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
          savedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(
            jsonEncode({'status': 'updated'}),
            200,
          );
        }
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      final healthCheckField = find.byWidgetPredicate(
        (w) =>
            w is TextField && w.decoration?.labelText == 'Health Check Command',
      );
      await tester.ensureVisible(healthCheckField);
      await tester.pump();
      await tester.enterText(
        healthCheckField,
        'curl -sf http://localhost:8080/',
      );

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(savedBody, isNotNull);
      expect(savedBody!['health_check'], 'curl -sf http://localhost:8080/');
      // Drain the 2s auto-clear timer.
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets('save failure shows a "Failed:" message', (tester) async {
      testAuthHttpClientOverride = _client(
        saveStatus: 400,
        saveResponse: {'detail': 'bad mounts'},
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.textContaining('Failed'), findsOneWidget);
      expect(find.textContaining('bad mounts'), findsOneWidget);
      // Drain the 2s auto-clear timer (see save-success test).
      await tester.pumpAndSettle();
    });
  });

  group('auto start', () {
    testWidgets('hides the checkbox when auto-start is not allowed',
        (tester) async {
      // Default _client returns 404 for /config -> allow_autostart false.
      testAuthHttpClientOverride = _client();
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('Auto start'), findsNothing);
      expect(find.byType(Checkbox), findsNothing);
    });

    testWidgets('shows the checkbox and round-trips auto_start when allowed',
        (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/config') {
          return http.Response(
            jsonEncode({'allow_autostart': true}),
            200,
          );
        }
        if (p == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode([
              {..._workspace, 'auto_start': true}
            ]),
            200,
          );
        }
        if (p == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode([]), 200);
        }
        if (p == '/api/v1/images') {
          return http.Response(
            jsonEncode({
              'default': 'klangk-pi',
              'allowed': ['klangk-pi'],
            }),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
          savedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('nf', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Checkbox present + checked (workspace had auto_start: true).
      final checkbox = find.byType(Checkbox);
      expect(checkbox, findsOneWidget);
      expect(tester.widget<Checkbox>(checkbox).value, isTrue);

      // Toggle it off and save.
      await tester.ensureVisible(checkbox);
      await tester.tap(checkbox);
      await tester.pump();
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(savedBody, isNotNull);
      expect(savedBody!['auto_start'], false);
      // Drain the 2s auto-clear timer (see save-success test).
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });
  });

  group('export', () {
    testWidgets('export success triggers a download (no error snackbar)',
        (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Export Workspace'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      // No failure snackbar on success.
      expect(find.textContaining('Export failed'), findsNothing);
    });

    testWidgets('export failure shows an error snackbar', (tester) async {
      testAuthHttpClientOverride = _client(exportStatus: 500);
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Export Workspace'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.textContaining('Export failed'), findsOneWidget);
    });
  });

  group('danger zone', () {
    testWidgets('shut down opens a confirmation dialog', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Shut Down Container'));
      await tester.pumpAndSettle();

      // Dialog title + the button both say "Shut Down Container".
      expect(find.text('Shut Down Container'), findsNWidgets(2));
      expect(find.textContaining('stop the container'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
      expect(find.text('Shut Down'), findsOneWidget);
    });

    testWidgets('cancel dismisses the dialog without shutting down',
        (tester) async {
      final ws = _MockWsClient();
      await tester.pumpWidget(_buildPanel(wsClient: ws));
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Shut Down Container'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      // Dialog gone, no shutdown sent.
      expect(find.text('Shut Down'), findsNothing);
      expect(ws.shutdownSent, isFalse);
    });

    testWidgets('confirm sends shutdown and dismisses the dialog',
        (tester) async {
      final ws = _MockWsClient();
      await tester.pumpWidget(_buildPanel(wsClient: ws));
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Shut Down Container'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Shut Down').last);
      await tester.pumpAndSettle();

      expect(ws.shutdownSent, isTrue);
      expect(find.text('Cancel'), findsNothing);
    });
  });

  group('shared workspace + sparse data', () {
    testWidgets('loads workspace from the shared list when not owned',
        (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(jsonEncode([]), 200); // not in owned
        }
        if (request.url.path == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode([_workspace]), 200);
        }
        if (request.url.path == '/api/v1/images') {
          return http.Response(
            jsonEncode({
              'default': 'klangk-pi',
              'allowed': ['klangk-pi']
            }),
            200,
          );
        }
        return http.Response('nf', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('my-ws'), findsOneWidget);
    });

    testWidgets('renders when workspace has no mounts/env (defaults)',
        (tester) async {
      testAuthHttpClientOverride = _client(
        workspace: {'id': _wsId, 'name': 'bare', 'image': 'klangk-pi'},
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('bare'), findsOneWidget);
      // No mount/env list items.
      expect(find.byIcon(Icons.close), findsNothing);
    });
  });

  group('image dropdown', () {
    testWidgets('changing the image dropdown updates selection',
        (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Open the dropdown and pick the non-default image. The dropdown
      // sits at the bottom of the config card (after the field reorder),
      // so scroll it into view first.
      await _scrollToAndTap(tester, find.text('klangk-pi'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('other:latest').last);
      await tester.pumpAndSettle();

      // The dropdown now shows the selected image.
      expect(find.text('other:latest'), findsOneWidget);
    });
  });

  group('copy buttons', () {
    testWidgets('tapping a mount copy button writes to clipboard',
        (tester) async {
      // Stub the clipboard platform channel so Clipboard.setData is a no-op
      // (otherwise it throws without a real platform).
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        (call) async {
          if (call.method == 'Clipboard.setData') return null;
          return null;
        },
      );

      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.copy).first);
      await tester.pump();

      // Reaching here without throwing means the copy onPressed ran.
      expect(find.byIcon(Icons.copy), findsNWidgets(2));
    });
  });

  group('didUpdateWidget resync', () {
    testWidgets('resyncs mounts, env, and image after save reloads data',
        (tester) async {
      // First load returns the default workspace; after save, the backend
      // returns updated mounts/env/image so _loadData rebuilds the form
      // with new props, triggering didUpdateWidget.
      var loadCount = 0;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/workspaces') {
          loadCount++;
          if (loadCount <= 1) {
            return http.Response(jsonEncode([_workspace]), 200);
          }
          // After save, return updated data.
          return http.Response(
            jsonEncode([
              {
                ..._workspace,
                'mounts': ['/new:/path'],
                'env': {'NEW_KEY': 'new_val'},
                'image': 'other:latest',
              }
            ]),
            200,
          );
        }
        if (p == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode([]), 200);
        }
        if (p == '/api/v1/images') {
          return http.Response(
            jsonEncode({
              'default': 'klangk-pi',
              'allowed': ['klangk-pi', 'other:latest'],
            }),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('nf', 404);
      });

      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Verify initial data is shown.
      expect(find.text('/host:/cont'), findsOneWidget);
      expect(find.text('FOO=bar'), findsOneWidget);

      // Save triggers _loadData which fetches updated data.
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      await tester.pumpAndSettle();

      // After reload, the form should show the new values.
      expect(find.text('/new:/path'), findsOneWidget);
      expect(find.text('NEW_KEY=new_val'), findsOneWidget);
      expect(find.text('other:latest'), findsOneWidget);
      // Old values should be gone.
      expect(find.text('/host:/cont'), findsNothing);
      expect(find.text('FOO=bar'), findsNothing);

      // Drain the 2s auto-clear timer.
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets(
        'falls back to default image when reloaded image not in allowed',
        (tester) async {
      var loadCount = 0;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/workspaces') {
          loadCount++;
          if (loadCount <= 1) {
            return http.Response(jsonEncode([_workspace]), 200);
          }
          // After save, return an image not in the allowed list.
          return http.Response(
            jsonEncode([
              {..._workspace, 'image': 'unknown:latest'}
            ]),
            200,
          );
        }
        if (p == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode([]), 200);
        }
        if (p == '/api/v1/images') {
          return http.Response(
            jsonEncode({
              'default': 'klangk-pi',
              'allowed': ['klangk-pi', 'other:latest'],
            }),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('nf', 404);
      });

      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      await tester.pumpAndSettle();

      // Should fall back to default image, not show the unknown one.
      expect(find.text('klangk-pi'), findsOneWidget);
      expect(find.text('unknown:latest'), findsNothing);

      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets('handles null env after save (falls back to empty map)',
        (tester) async {
      var loadCount = 0;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/workspaces') {
          loadCount++;
          if (loadCount <= 1) {
            return http.Response(jsonEncode([_workspace]), 200);
          }
          // After save, env is null.
          return http.Response(
            jsonEncode([
              {
                ..._workspace,
                'env': null,
              }
            ]),
            200,
          );
        }
        if (p == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode([]), 200);
        }
        if (p == '/api/v1/images') {
          return http.Response(
            jsonEncode({
              'default': 'klangk-pi',
              'allowed': ['klangk-pi', 'other:latest'],
            }),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('nf', 404);
      });

      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Initially has env vars.
      expect(find.text('FOO=bar'), findsOneWidget);

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      await tester.pumpAndSettle();

      // Env vars should be gone after reload with null env.
      expect(find.text('FOO=bar'), findsNothing);

      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });
  });

  group('save error detail parsing', () {
    testWidgets('save failure with non-JSON body falls back to status code',
        (tester) async {
      testAuthHttpClientOverride = _client(
        saveStatus: 400,
        saveResponse: null, // ignored: handler returns non-JSON body below
      );
      // Override the save response to be non-JSON so the detail-parse
      // catch path runs.
      testAuthHttpClientOverride = MockClient((request) async {
        if (request.url.path == '/api/v1/workspaces') {
          return http.Response(jsonEncode([_workspace]), 200);
        }
        if (request.url.path == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode([]), 200);
        }
        if (request.url.path == '/api/v1/images') {
          return http.Response(
            jsonEncode({
              'default': 'klangk-pi',
              'allowed': ['klangk-pi']
            }),
            200,
          );
        }
        if (request.url.path == '/api/v1/workspaces/$_wsId' &&
            request.method == 'PUT') {
          return http.Response('plain text error', 400);
        }
        return http.Response('nf', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.textContaining('Failed'), findsOneWidget);
      expect(find.textContaining('400'), findsOneWidget);
    });
  });

  group('transfer ownership', () {
    testWidgets('renders the transfer card', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await tester.ensureVisible(find.text('Transfer Ownership').first);
      await tester.pump();
      expect(find.text('Transfer Ownership'), findsNWidgets(2));
      expect(find.textContaining('lose owner access'), findsOneWidget);
    });

    testWidgets('opens search dialog on button tap', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(
        tester,
        find.widgetWithText(OutlinedButton, 'Transfer Ownership'),
      );
      await tester.pumpAndSettle();

      expect(find.textContaining('Search for the user'), findsOneWidget);
      expect(find.text('Cancel'), findsOneWidget);
    });

    testWidgets('cancel dismisses the search dialog', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(
        tester,
        find.widgetWithText(OutlinedButton, 'Transfer Ownership'),
      );
      await tester.pumpAndSettle();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Search for the user'), findsNothing);
    });

    testWidgets('search shows results and tapping opens confirm dialog',
        (tester) async {
      testAuthHttpClientOverride = _client(
        searchResults: [
          {'id': 'u2', 'email': 'target@test.com', 'handle': 'target'},
        ],
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(
        tester,
        find.widgetWithText(OutlinedButton, 'Transfer Ownership'),
      );
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byType(TextField).last,
        'target',
      );
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();

      expect(find.text('target@test.com'), findsOneWidget);

      await tester.tap(find.text('target@test.com'));
      await tester.pumpAndSettle();

      expect(find.text('Confirm Transfer'), findsOneWidget);
      expect(find.textContaining('target@test.com'), findsOneWidget);
    });

    testWidgets('confirm executes transfer successfully', (tester) async {
      testAuthHttpClientOverride = _client(
        searchResults: [
          {'id': 'u2', 'email': 'target@test.com', 'handle': 'target'},
        ],
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(
        tester,
        find.widgetWithText(OutlinedButton, 'Transfer Ownership'),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).last, 'target');
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();

      await tester.tap(find.text('target@test.com'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Transfer'));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('transferred to target@test.com'),
        findsOneWidget,
      );
    });

    testWidgets('transfer failure shows error snackbar', (tester) async {
      testAuthHttpClientOverride = _client(
        searchResults: [
          {'id': 'u2', 'email': 'target@test.com', 'handle': 'target'},
        ],
        transferStatus: 409,
        transferResponse: {'detail': 'already the owner'},
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(
        tester,
        find.widgetWithText(OutlinedButton, 'Transfer Ownership'),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).last, 'target');
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();

      await tester.tap(find.text('target@test.com'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Transfer'));
      await tester.pumpAndSettle();

      expect(
        find.textContaining('Transfer failed'),
        findsOneWidget,
      );
    });

    testWidgets('transfer failure with non-JSON body shows status code',
        (tester) async {
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/workspaces') {
          return http.Response(jsonEncode([_workspace]), 200);
        }
        if (p == '/api/v1/workspaces/shared') {
          return http.Response(jsonEncode([]), 200);
        }
        if (p == '/api/v1/images') {
          return http.Response(
            jsonEncode({
              'default': 'klangk-pi',
              'allowed': ['klangk-pi', 'other:latest'],
            }),
            200,
          );
        }
        if (p == '/api/v1/users/search') {
          return http.Response(
            jsonEncode([
              {'id': 'u2', 'email': 'target@test.com', 'handle': 'target'},
            ]),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId/transfer' &&
            request.method == 'POST') {
          return http.Response('plain text error', 500);
        }
        return http.Response('nf', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(
        tester,
        find.widgetWithText(OutlinedButton, 'Transfer Ownership'),
      );
      await tester.pumpAndSettle();

      await tester.enterText(find.byType(TextField).last, 'target');
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();

      await tester.tap(find.text('target@test.com'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Transfer'));
      await tester.pumpAndSettle();

      expect(find.textContaining('500'), findsOneWidget);
    });

    testWidgets('submitting email directly opens confirm dialog',
        (tester) async {
      testAuthHttpClientOverride = _client();
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(
        tester,
        find.widgetWithText(OutlinedButton, 'Transfer Ownership'),
      );
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byType(TextField).last,
        'direct@test.com',
      );
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      expect(find.text('Confirm Transfer'), findsOneWidget);
      expect(find.textContaining('direct@test.com'), findsOneWidget);
    });

    testWidgets('clearing the search field clears results', (tester) async {
      testAuthHttpClientOverride = _client(
        searchResults: [
          {'id': 'u2', 'email': 'target@test.com', 'handle': 'target'},
        ],
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(
        tester,
        find.widgetWithText(OutlinedButton, 'Transfer Ownership'),
      );
      await tester.pumpAndSettle();

      // Type to get results.
      await tester.enterText(find.byType(TextField).last, 'target');
      await tester.pump(const Duration(milliseconds: 400));
      await tester.pumpAndSettle();
      expect(find.text('target@test.com'), findsOneWidget);

      // Clear the field to trigger the empty-query branch.
      await tester.enterText(find.byType(TextField).last, '');
      await tester.pump();

      // Results should be cleared.
      expect(find.text('target@test.com'), findsNothing);
    });

    testWidgets('cancel with pending debounce cancels timer', (tester) async {
      testAuthHttpClientOverride = _client(
        searchResults: [
          {'id': 'u2', 'email': 'target@test.com', 'handle': 'target'},
        ],
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(
        tester,
        find.widgetWithText(OutlinedButton, 'Transfer Ownership'),
      );
      await tester.pumpAndSettle();

      // Type to trigger debounce timer (don't wait for it to fire).
      await tester.enterText(find.byType(TextField).last, 'target');
      await tester.pump();

      // Cancel while the debounce timer is still pending.
      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(find.textContaining('Search for the user'), findsNothing);
    });

    testWidgets('cancel on confirm dialog dismisses without transferring',
        (tester) async {
      testAuthHttpClientOverride = _client();
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(
        tester,
        find.widgetWithText(OutlinedButton, 'Transfer Ownership'),
      );
      await tester.pumpAndSettle();

      await tester.enterText(
        find.byType(TextField).last,
        'cancel@test.com',
      );
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pumpAndSettle();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      expect(find.text('Confirm Transfer'), findsNothing);
    });
  });
}
