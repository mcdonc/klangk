import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_frontend/utils/system_agent.dart';
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
Finder _allowedDomainsInput() => find.byWidgetPredicate(
      (w) => w is TextField && w.decoration?.hintText == 'github.com:443',
    );
Finder _rejectedDomainsInput() => find.byWidgetPredicate(
      (w) => w is TextField && w.decoration?.hintText == 'evil.example.com',
    );

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
  Object? saveResponse,
  List<Map<String, dynamic>>? saveRecorder,
  int saveStatus = 200,
  int exportStatus = 200,
  bool imagesFail = false,
  int transferStatus = 200,
  Map<String, dynamic>? transferResponse,
  List<Map<String, dynamic>>? searchResults,
  bool netfilterEnabled = false,
  bool nixAvailable = false,
  bool sudoAvailable = false,
  bool perHandleHomeAvailable = true,
  List<String>? stopRecorder,
  int stopStatus = 200,
  bool stopThrows = false,
  List<String>? putRecorder,
}) {
  final ws = (workspace ?? _workspace);
  return MockClient((request) async {
    final p = request.url.path;
    if (p == '/api/v1/config') {
      return http.Response(
        jsonEncode({
          'netfilter_enabled': netfilterEnabled,
          'nix_available': nixAvailable,
          'sudo_available': sudoAvailable,
          'per_handle_home_available': perHandleHomeAvailable,
        }),
        200,
      );
    }
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
      putRecorder?.add(p);
      saveRecorder?.add(jsonDecode(request.body) as Map<String, dynamic>);
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
    if (p == '/api/v1/workspaces/$_wsId/stop' && request.method == 'POST') {
      stopRecorder?.add(p);
      if (stopThrows) throw Exception('network down');
      return http.Response(jsonEncode({'status': 'stopped'}), stopStatus);
    }
    return http.Response('not found', 404);
  });
}

Widget _buildPanel(
        {VoidCallback? onRestart,
        bool canExport = true,
        bool canRestart = true}) =>
    MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProvider.value(value: WsClient()),
      ],
      // Non-const so that the Dart VM's coverage instrumentation observes
      // the constructor call at runtime.  Const constructors are evaluated
      // at compile time and invisible to coverage (dart-lang/sdk#38934).
      child: MaterialApp(
        home: Scaffold(
          body: WorkspaceSettingsPanel(
            workspaceId: _wsId,
            canExport: canExport,
            canRestart: canRestart,
            onRestart: onRestart ?? () {},
          ),
        ),
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

  group('classification banner (#2768)', () {
    testWidgets('renders the field populated from the workspace',
        (tester) async {
      testAuthHttpClientOverride = _client(
        workspace: {..._workspace, 'classification_banner': 'SECRET'},
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      final field = find.widgetWithText(TextField, 'Classification Banner');
      expect(field, findsOneWidget);
      final tf = tester.widget<TextField>(field);
      expect(tf.controller?.text, 'SECRET');
    });

    testWidgets('saves an edited marking and clears with an empty field',
        (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/config') {
          return http.Response(jsonEncode({}), 200);
        }
        if (p == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode([
              {..._workspace, 'classification_banner': 'SECRET'}
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
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Edit to CUI, save.
      final field = find.widgetWithText(TextField, 'Classification Banner');
      await tester.enterText(field, 'CUI');
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(savedBody, isNotNull);
      expect(savedBody!['classification_banner'], 'CUI');
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();

      // Empty field clears the override (server normalizes '' to inherit).
      await tester.enterText(field, '');
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(savedBody!['classification_banner'], '');
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });
  });

  group('WorkspaceSettingsPanel load + render', () {
    testWidgets('renders config fields populated from the workspace',
        (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Config area renders as sectioned panes (#2229); the General pane
      // icon is present when the config loaded.
      expect(find.byIcon(Icons.tune), findsOneWidget);
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

      // Falls back to default image; panel still renders its sections.
      expect(find.byIcon(Icons.tune), findsOneWidget);
    });

    testWidgets('section nav jumps to the chosen section', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // The Netfilter section's domain input starts below the fold.
      final domainInput = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.hintText == 'github.com:443',
      );
      expect(domainInput.hitTestable(), findsNothing);

      // Tapping the nav pill scrolls the section into view (#2229).
      await tester.tap(find.text('Netfilter').first);
      await tester.pumpAndSettle();

      expect(domainInput.hitTestable(), findsOneWidget);
    });
  });

  group('nix toggle', () {
    // #2233: the per-workspace "Mount /nix dir" toggle mirrors the create
    // dialog. It is shown only when the server reports nix_available, is
    // pre-populated from settings.nix, sends settings.nix when opted in,
    // and (because the /nix mount is set up at create time) prompts a
    // restart when toggled on a running workspace.
    testWidgets('hides the toggle when nix is not available', (tester) async {
      testAuthHttpClientOverride = _client(); // nixAvailable defaults false
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('Mount /nix dir'), findsNothing);
    });

    testWidgets('shows the toggle labeled "Mount /nix dir" when available',
        (tester) async {
      testAuthHttpClientOverride = _client(nixAvailable: true);
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(
        find.widgetWithText(CheckboxListTile, 'Mount /nix dir'),
        findsOneWidget,
      );
    });

    testWidgets('keeps the toggle when the images endpoint fails (#2994)',
        (tester) async {
      // The toggles ride the /config cache, not the images payload — a
      // failed images fetch must only degrade the image dropdown, not
      // silently hide the nix toggle.
      testAuthHttpClientOverride =
          _client(imagesFail: true, nixAvailable: true);
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(
        find.widgetWithText(CheckboxListTile, 'Mount /nix dir'),
        findsOneWidget,
      );
    });

    testWidgets('pre-populates the toggle from settings.nix', (tester) async {
      testAuthHttpClientOverride = _client(
        nixAvailable: true,
        workspace: {
          ..._workspace,
          'settings': {'nix': true},
        },
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      final cb = tester.widget<CheckboxListTile>(
        find.widgetWithText(CheckboxListTile, 'Mount /nix dir'),
      );
      expect(cb.value, isTrue);
    });

    testWidgets('sends settings.nix when toggled on', (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/config') {
          return http.Response(
            jsonEncode({'nix_available': true}),
            200,
          );
        }
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
              'allowed': ['klangk-pi'],
            }),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
          savedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      final nix = find.widgetWithText(CheckboxListTile, 'Mount /nix dir');
      await tester.ensureVisible(nix);
      await tester.tap(nix);
      await tester.pump();
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(savedBody, isNotNull);
      expect(savedBody!['settings'], {'nix': true});
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets('toggling nix on a running container shows the restart notice',
        (tester) async {
      testAuthHttpClientOverride = _client(
        nixAvailable: true,
        workspace: {
          ..._workspace,
          'running': true,
          // nix off in the stored bag — turning it on is a create-time change.
        },
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      final nix = find.widgetWithText(CheckboxListTile, 'Mount /nix dir');
      await tester.ensureVisible(nix);
      await tester.tap(nix);
      await tester.pump();
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });
  });

  group('nix toggle (off direction)', () {
    // #2233: PUT settings is a full-replace bag, so the off direction must
    // emit an explicit nix=false — omitting the key would leave the stale
    // bag (the mount would survive a restart).
    testWidgets('clears settings.nix when toggled off', (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/config') {
          return http.Response(jsonEncode({'nix_available': true}), 200);
        }
        if (p == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode([
              {
                ..._workspace,
                'settings': {'nix': true}
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
              'allowed': ['klangk-pi'],
            }),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
          savedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Pre-populated on; uncheck it.
      final nix = find.widgetWithText(CheckboxListTile, 'Mount /nix dir');
      expect((tester.widget(nix) as CheckboxListTile).value, isTrue);
      await tester.ensureVisible(nix);
      await tester.tap(nix);
      await tester.pump();
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(savedBody, isNotNull);
      expect(savedBody!['settings'], {'nix': false});
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets(
        'no restart notice when nix backend is gone but the bag has nix',
        (tester) async {
      // nix_available is false (no backend) but the stored bag still has
      // nix=true. Saving without touching the (hidden) toggle must not fire
      // a spurious restart notice — nix isn't emitted or compared.
      testAuthHttpClientOverride = _client(
        workspace: {
          ..._workspace,
          'running': true,
          'settings': {'nix': true},
        },
      ); // nixAvailable defaults false
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      expect(
          find.textContaining('Restart the workspace to apply'), findsNothing);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });
  });

  group('sudo toggle', () {
    // #2017/#3046: the per-workspace sudo toggle. The deploy-wide
    // allow_sudo is a ceiling, so the toggle is shown only when the
    // deploy allows sudo; it is pre-populated from settings.allow_sudo
    // (absent = false = locked-down in the UI, #3046 — the server still
    // resolves absent as follow-deploy until saved), always emits an
    // explicit value (full-replace bag), and prompts a restart on a
    // running workspace (the sudoers rule is written at create time).
    testWidgets('hides the toggle when the deploy forbids sudo',
        (tester) async {
      testAuthHttpClientOverride = _client(); // sudoAvailable defaults false
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('Allow sudo'), findsNothing);
    });

    testWidgets('shows the toggle labeled "Allow sudo" when available',
        (tester) async {
      testAuthHttpClientOverride = _client(sudoAvailable: true);
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(
        find.widgetWithText(CheckboxListTile, 'Allow sudo'),
        findsOneWidget,
      );
    });

    testWidgets('pre-populates from settings.allow_sudo', (tester) async {
      testAuthHttpClientOverride = _client(
        sudoAvailable: true,
        workspace: {
          ..._workspace,
          'settings': {'allow_sudo': false},
        },
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      final cb = tester.widget<CheckboxListTile>(
        find.widgetWithText(CheckboxListTile, 'Allow sudo'),
      );
      expect(cb.value, isFalse);
    });

    testWidgets(
        'defaults to unchecked (absent bag) and sends allow_sudo=false with'
        ' no restart notice (#3047)', (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/config') {
          return http.Response(jsonEncode({'sudo_available': true}), 200);
        }
        if (p == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode([
              {..._workspace, 'running': true}
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
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      final sudo = find.widgetWithText(CheckboxListTile, 'Allow sudo');
      // #3046: absent bag key reads as locked-down — no tap needed.
      expect((tester.widget(sudo) as CheckboxListTile).value, isFalse);
      await tester.ensureVisible(sudo);
      await tester.pump();
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(savedBody, isNotNull);
      expect(savedBody!['settings']['allow_sudo'], isFalse);
      // #3047: an absent key already means OFF, so storing an explicit
      // false is not a posture flip — no restart notice.
      expect(
          find.textContaining('Restart the workspace to apply'), findsNothing);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets(
        'unchecking an opted-in workspace flips posture and notices a restart'
        ' (#3047)', (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/config') {
          return http.Response(jsonEncode({'sudo_available': true}), 200);
        }
        if (p == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode([
              {
                ..._workspace,
                'running': true,
                'settings': {'allow_sudo': true},
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
              'allowed': ['klangk-pi'],
            }),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
          savedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      final sudo = find.widgetWithText(CheckboxListTile, 'Allow sudo');
      expect((tester.widget(sudo) as CheckboxListTile).value, isTrue);
      await tester.ensureVisible(sudo);
      await tester.tap(sudo); // uncheck = lock the workspace down
      await tester.pump();
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(savedBody, isNotNull);
      expect(savedBody!['settings']['allow_sudo'], isFalse);
      // The sudoers rule is written at container-create time — the flip
      // on a running workspace is a create-time change.
      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets('revert (check) clears a stored lock-down with allow_sudo=true',
        (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/config') {
          return http.Response(jsonEncode({'sudo_available': true}), 200);
        }
        if (p == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode([
              {
                ..._workspace,
                'settings': {'allow_sudo': false},
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
              'allowed': ['klangk-pi'],
            }),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
          savedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      final sudo = find.widgetWithText(CheckboxListTile, 'Allow sudo');
      expect((tester.widget(sudo) as CheckboxListTile).value, isFalse);
      await tester.ensureVisible(sudo);
      await tester.tap(sudo); // check = revert to the deploy posture
      await tester.pump();
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(savedBody, isNotNull);
      expect(savedBody!['settings']['allow_sudo'], isTrue);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });
  });

  testWidgets(
      'saving with both toggles hidden preserves the stored bag (#2017)',
      (tester) async {
    // Deploy sudo off + no nix backend → neither toggle shown. A plain
    // resource edit must not full-replace-wipe a stored allow_sudo
    // lock-down or API-only keys.
    Map<String, dynamic>? savedBody;
    testAuthHttpClientOverride = MockClient((request) async {
      final p = request.url.path;
      if (p == '/api/v1/config') return http.Response(jsonEncode({}), 200);
      if (p == '/api/v1/workspaces') {
        return http.Response(
          jsonEncode([
            {
              ..._workspace,
              'settings': {'allow_sudo': false, 'bridge_timeout': 60},
            }
          ]),
          200,
        );
      }
      if (p == '/api/v1/workspaces/shared') {
        return http.Response(jsonEncode([]), 200);
      }
      if (p == '/api/v1/images') {
        // Neither nix_available nor sudo_available.
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
      return http.Response('not found', 404);
    });
    await tester.pumpWidget(_buildPanel());
    await tester.pumpAndSettle();

    await tester.enterText(
        find.widgetWithText(TextField, 'CPU Limit').first, '2.0');
    await tester.pumpAndSettle();
    await _scrollToAndTap(tester, find.text('Save'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 100));

    expect(savedBody, isNotNull);
    expect(savedBody!['settings'], {
      'allow_sudo': false,
      'bridge_timeout': 60,
      'cpu_limit': 2.0,
    });
    await tester.pump(const Duration(seconds: 2));
    await tester.pumpAndSettle();
  });

  group('nix toggle (settings preservation)', () {
    // #2234 re-review: PUT settings is a full-replace bag. With a nix
    // backend configured the save now always emits settings, so it must
    // seed from the existing bag to preserve API-only keys the form does
    // not represent (e.g. bridge_timeout) instead of wiping them.
    testWidgets('preserves API-only settings keys across save', (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/config') {
          return http.Response(jsonEncode({}), 200);
        }
        if (p == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode([
              {
                ..._workspace,
                'settings': {'bridge_timeout': 60, 'nix': true},
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
              'allowed': ['klangk-pi'],
            }),
            200,
          );
        }
        if (p == '/api/v1/workspaces/$_wsId' && request.method == 'PUT') {
          savedBody = jsonDecode(request.body) as Map<String, dynamic>;
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Leave the nix toggle untouched (pre-populated on) and save.
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(savedBody, isNotNull);
      expect(savedBody!['settings']['bridge_timeout'], 60);
      expect(savedBody!['settings']['nix'], true);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
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
      // The General pane grew (Per-handle home tile, #2721), pushing the
      // env editor below the fold — scroll it in before tapping.
      await tester.ensureVisible(closes.last);
      await tester.pumpAndSettle();
      await tester.tap(closes.last);
      await tester.pump();

      expect(find.text('FOO=bar'), findsNothing);
    });
  });

  group('allowed domains editor', () {
    testWidgets('adds a valid host:port', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await tester.enterText(_allowedDomainsInput(), 'example.com:443');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(find.text('example.com:443'), findsOneWidget);
    });

    testWidgets('rejects a spec with whitespace', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await tester.enterText(_allowedDomainsInput(), 'bad spec');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(
          find.text('Expected host, host:port, or *.domain'), findsOneWidget);
      expect(
        find.byWidgetPredicate(
          (w) => w is SelectableText && (w.data ?? '') == 'bad spec',
        ),
        findsNothing,
      );
    });

    testWidgets('removes an allowed domain via its close button',
        (tester) async {
      // Mounts/env empty so the only close icon on screen belongs to the
      // allowed-domains chip.
      testAuthHttpClientOverride = _client(workspace: {
        ..._workspace,
        'mounts': <String>[],
        'env': <String, String>{},
        'allowed_domains': <String>['example.com:443'],
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('example.com:443'), findsOneWidget);
      await tester.ensureVisible(find.byIcon(Icons.close));
      await tester.tap(find.byIcon(Icons.close));
      await tester.pump();

      expect(find.text('example.com:443'), findsNothing);
    });

    testWidgets('reload after save re-reads allowed_domains from server',
        (tester) async {
      // A successful save calls _loadData(), which re-fetches the workspace
      // and rebuilds the form with a fresh map. didUpdateWidget must
      // re-read allowed_domains (#1365) so the editor tracks server state
      // instead of holding a stale local copy. The PUT drops the server's
      // allowed_domains entirely so the null-coalescing fallback in the
      // refresh path is also exercised.
      List<String>? domains = ['example.com:443'];
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/workspaces') {
          final ws = <String, dynamic>{..._workspace};
          if (domains != null)
            ws['allowed_domains'] = List<String>.from(domains!);
          return http.Response(jsonEncode([ws]), 200);
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
          domains = null;
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();
      expect(find.text('example.com:443'), findsOneWidget);

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      // After the post-save reload the server no longer carries
      // allowed_domains, so didUpdateWidget refreshed _allowedDomains to
      // empty (the null-coalescing fallback) and the chip is gone.
      expect(find.text('example.com:443'), findsNothing);

      // Advance past the 2s save-message auto-clear timer so no timer is
      // pending at dispose (flutter_test fails on pending timers).
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });
  });

  // #2386: the rejected-domains editor mirrors allowed-domains.
  group('rejected domains editor', () {
    testWidgets('adds a valid host', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Use a value distinct from the input's hint ('evil.example.com').
      await tester.enterText(_rejectedDomainsInput(), 'blocked.example.com');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      expect(find.text('blocked.example.com'), findsOneWidget);
    });

    testWidgets('rejects a CIDR (name-level NXDOMAIN)', (tester) async {
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await tester.enterText(_rejectedDomainsInput(), '10.0.0.0/8');
      await tester.testTextInput.receiveAction(TextInputAction.done);
      await tester.pump();

      // 'for rejected domains' appears only in the error, not the help text.
      expect(find.textContaining('for rejected domains'), findsOneWidget);
      expect(
        find.byWidgetPredicate(
          (w) => w is SelectableText && (w.data ?? '') == '10.0.0.0/8',
        ),
        findsNothing,
      );
    });

    testWidgets('removes a rejected domain via its close button',
        (tester) async {
      testAuthHttpClientOverride = _client(workspace: {
        ..._workspace,
        'mounts': <String>[],
        'env': <String, String>{},
        'allowed_domains': null,
        'rejected_domains': <String>['blocked.example.com'],
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      expect(find.text('blocked.example.com'), findsOneWidget);
      // Only the rejected chip is on screen (mounts/env/allowed empty), so
      // the single close icon belongs to it.
      await tester.ensureVisible(find.byIcon(Icons.close));
      await tester.tap(find.byIcon(Icons.close));
      await tester.pump();

      expect(find.text('blocked.example.com'), findsNothing);
    });

    testWidgets('reload after save re-reads rejected_domains from server',
        (tester) async {
      // didUpdateWidget must re-read rejected_domains (#2386) so the editor
      // tracks server state instead of holding a stale local copy.
      List<String>? rejected = ['old.example.com'];
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/workspaces') {
          final ws = <String, dynamic>{..._workspace};
          if (rejected != null)
            ws['rejected_domains'] = List<String>.from(rejected!);
          return http.Response(jsonEncode([ws]), 200);
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
          rejected = null;
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();
      expect(find.text('old.example.com'), findsOneWidget);

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      // After the post-save reload the server no longer carries
      // rejected_domains, so didUpdateWidget refreshed _rejectedDomains to
      // empty and the chip is gone.
      expect(find.text('old.example.com'), findsNothing);

      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });
  });

  // #1769: a workspace that declares allowed_domains while the deploy has
  // netfilter disabled starts unrestricted (fail-open). The gap must be
  // surfaced to the user who set the list, not just operator logs.
  group('egress not-enforced notice (#1769)', () {
    testWidgets(
      'shows the notice when allowed_domains are set and netfilter is off',
      (tester) async {
        testAuthHttpClientOverride = _client(
          workspace: {
            ..._workspace,
            'allowed_domains': <String>['example.com:443'],
          },
        );
        await tester.pumpWidget(_buildPanel());
        await tester.pumpAndSettle();

        expect(
          find.textContaining('NOT being enforced'),
          findsOneWidget,
        );
      },
    );

    testWidgets(
      'shows the notice when rejected_domains are set and netfilter is off (#2386)',
      (tester) async {
        testAuthHttpClientOverride = _client(
          workspace: {
            ..._workspace,
            'rejected_domains': <String>['blocked.example.com'],
          },
        );
        await tester.pumpWidget(_buildPanel());
        await tester.pumpAndSettle();

        // The reject-list notice (distinct from the allow-list one).
        expect(
          find.textContaining('rejected-domains list'),
          findsOneWidget,
        );
        expect(
          find.textContaining('will be reachable'),
          findsOneWidget,
        );
      },
    );

    testWidgets(
      'hides the notice when netfilter is enabled (allow-list enforced)',
      (tester) async {
        testAuthHttpClientOverride = _client(
          workspace: {
            ..._workspace,
            'allowed_domains': <String>['example.com:443'],
          },
          netfilterEnabled: true,
        );
        await tester.pumpWidget(_buildPanel());
        await tester.pumpAndSettle();

        expect(find.textContaining('NOT being enforced'), findsNothing);
      },
    );

    testWidgets(
      'hides the notice when no allowed_domains are set (unrestricted by '
      'design)',
      (tester) async {
        // Default workspace has no allowed_domains; nothing to enforce.
        testAuthHttpClientOverride = _client();
        await tester.pumpWidget(_buildPanel());
        await tester.pumpAndSettle();

        expect(find.textContaining('NOT being enforced'), findsNothing);
      },
    );
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

    testWidgets(
        'a cleared Name blocks the save inline and sends no PUT (#3130)',
        (tester) async {
      final puts = <String>[];
      testAuthHttpClientOverride = _client(putRecorder: puts);
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Clear the Name field and try to save.
      final nameField = find.byWidgetPredicate(
        (w) => w is TextField && w.decoration?.labelText == 'Name',
      );
      await tester.enterText(nameField, '');
      // Unfocus so the focused field's keep-visible doesn't fight the
      // scroll-to-Save below (settle its animated scroll fully).
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pumpAndSettle();
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      // The guard fired: inline validation on the field, no PUT sent, no
      // blanket failure banner.
      expect(
        tester.widget<TextField>(nameField).decoration?.errorText,
        'Workspace name cannot be empty or only whitespace',
      );
      expect(puts, isEmpty);
      expect(find.textContaining('Failed'), findsNothing);

      // Typing a name clears the error; the save then goes through.
      await tester.enterText(nameField, 'renamed-ws');
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pumpAndSettle();
      expect(
        tester.widget<TextField>(nameField).decoration?.errorText,
        isNull,
      );
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(puts, hasLength(1));
      expect(find.text('Settings saved'), findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets(
        'save failure renders a 422 detail list instead of a bare code '
        '(#3130)', (tester) async {
      // FastAPI/Pydantic validation errors put a list of error objects
      // under detail — the message must surface the first msg (minus
      // Pydantic's "Value error, " prefix), not degrade to "Error: 422".
      testAuthHttpClientOverride = _client(
        saveStatus: 422,
        saveResponse: <String, dynamic>{
          'detail': [
            {
              'type': 'value_error',
              'loc': ['body', 'name'],
              'msg': 'Value error, Workspace name cannot be empty or only '
                  'whitespace',
              'input': '',
            }
          ]
        },
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.textContaining('Failed'), findsOneWidget);
      expect(
        find.textContaining('Workspace name cannot be empty'),
        findsOneWidget,
      );
      // The raw Pydantic prefix is stripped.
      expect(find.textContaining('Value error'), findsNothing);
      await tester.pumpAndSettle();
    });

    testWidgets(
        'a map body with an unusable detail falls back to the raw body '
        '(#3130)', (tester) async {
      // detail is neither a string nor a list of error objects — the
      // banner shows the raw body instead of a bare status code.
      testAuthHttpClientOverride = _client(
        saveStatus: 400,
        saveResponse: {'detail': 42},
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.textContaining('Failed'), findsOneWidget);
      expect(find.textContaining('{"detail":42}'), findsOneWidget);
      await tester.pumpAndSettle();
    });

    testWidgets('a non-map JSON body falls back to the raw body (#3130)',
        (tester) async {
      // A JSON string root parses fine but carries no detail key — the
      // banner shows the raw body.
      testAuthHttpClientOverride = _client(
        saveStatus: 502,
        saveResponse: 'plain gateway error',
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.textContaining('Failed'), findsOneWidget);
      expect(find.textContaining('plain gateway error'), findsOneWidget);
      await tester.pumpAndSettle();
    });

    testWidgets(
        'allowed_domains change on a running container shows restart notice',
        (tester) async {
      // #1365: the egress filter is baked at container create time, so a
      // saved change only takes effect after a restart. The notice appears
      // only when a container is running AND allowed_domains changed.
      // Mounts/env emptied so the only close icon is the domain chip's.
      testAuthHttpClientOverride = _client(workspace: {
        ..._workspace,
        'mounts': <String>[],
        'env': <String, String>{},
        'allowed_domains': <String>['old.example:443'],
        'running': true,
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Remove the existing domain so the save differs from the loaded ws.
      await tester.ensureVisible(find.byIcon(Icons.close).first);
      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pump();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets('no restart notice when allowed_domains unchanged on save',
        (tester) async {
      // Saving without touching the filter must not nag.
      testAuthHttpClientOverride = _client(workspace: {
        ..._workspace,
        'allowed_domains': <String>['stable.example:443'],
        'running': true,
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      expect(
          find.textContaining('Restart the workspace to apply'), findsNothing);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets('no restart notice when container is not running',
        (tester) async {
      // A stopped workspace picks the new rules up on next start — no
      // action needed, so no notice even though allowed_domains changed.
      testAuthHttpClientOverride = _client(workspace: {
        ..._workspace,
        'mounts': <String>[],
        'env': <String, String>{},
        'allowed_domains': <String>['old.example:443'],
        'running': false,
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pump();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      expect(
          find.textContaining('Restart the workspace to apply'), findsNothing);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets('image change on a running container shows restart notice',
        (tester) async {
      // #1780: the restart notice is generalized to ALL create-time fields,
      // not just allowed_domains. Changing the image on a running workspace
      // must prompt a restart.
      testAuthHttpClientOverride = _client(workspace: {
        ..._workspace,
        'running': true,
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Change the image via the dropdown (klangk-pi -> other:latest).
      await _scrollToAndTap(tester, find.text('klangk-pi'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('other:latest').last);
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets(
        'service_command change on a running container shows restart notice',
        (tester) async {
      // #1780: changing the shell command on a running workspace prompts a
      // restart.
      testAuthHttpClientOverride = _client(workspace: {
        ..._workspace,
        'running': true,
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Replace the loaded service_command ('pi') so the save differs.
      await tester.enterText(
        find.byWidgetPredicate(
          (w) =>
              w is TextField &&
              w.decoration?.hintText == 'Optional — runs on terminal open',
        ),
        'pi --updated',
      );
      await tester.pump();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      // The offer-to-restart action is present (#1780).
      expect(find.text('Restart now'), findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets('mounts change on a running container shows restart notice',
        (tester) async {
      // #1780: removing a mount on a running workspace prompts a restart.
      testAuthHttpClientOverride = _client(workspace: {
        ..._workspace,
        'running': true,
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Remove the existing mount (/host:/cont) — the first close icon.
      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pump();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets('env change on a running container shows restart notice',
        (tester) async {
      // #1780: removing an env var on a running workspace prompts a restart.
      testAuthHttpClientOverride = _client(workspace: {
        ..._workspace,
        'running': true,
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Remove the existing env var (FOO=bar) — the last close icon.
      // The General pane grew (Per-handle home tile, #2721), pushing the
      // env editor below the fold — scroll it in before tapping.
      await tester.ensureVisible(find.byIcon(Icons.close).last);
      await tester.pumpAndSettle();
      await tester.tap(find.byIcon(Icons.close).last);
      await tester.pump();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets(
        'hides "Restart now" but keeps the notice without restart-workspace '
        '(#2939)', (tester) async {
      // Same mount-removal flow as the notice test above, but the member
      // cannot restart: the pending-restart information still shows
      // (they need to know the change waits for a container create);
      // only the action is hidden. A canRestart:true control in the same
      // test pins that the flow really did raise the notice.
      Future<void> runRemove(bool canRestart) async {
        testAuthHttpClientOverride = _client(workspace: {
          ..._workspace,
          'running': true,
        });
        await tester.pumpWidget(_buildPanel(canRestart: canRestart));
        await tester.pumpAndSettle();

        await tester.ensureVisible(find.byIcon(Icons.close).first);
        await tester.pumpAndSettle();
        await tester.tap(find.byIcon(Icons.close).first);
        await tester.pump();

        await _scrollToAndTap(tester, find.text('Save'));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 100));
      }

      // Control: the notice raises and the action shows.
      await runRemove(true);
      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      expect(find.text('Restart now'), findsOneWidget);

      // Without restart-workspace: same notice, no action.
      await runRemove(false);
      expect(find.text('Settings saved'), findsOneWidget);
      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      expect(find.text('Restart now'), findsNothing);
    });

    testWidgets('"Restart now" invokes onRestart and dismisses the notice',
        (tester) async {
      // #1780: the notice offers to restart now. Tapping it routes through
      // the workspace page's restart callback and clears the notice.
      var restartRequested = 0;
      testAuthHttpClientOverride = _client(workspace: {
        ..._workspace,
        'mounts': <String>[],
        'env': <String, String>{},
        'allowed_domains': <String>['old.example:443'],
        'running': true,
      });
      await tester.pumpWidget(_buildPanel(onRestart: () => restartRequested++));
      await tester.pumpAndSettle();

      // Trigger the notice by removing the existing domain.
      await tester.ensureVisible(find.byIcon(Icons.close).first);
      await tester.tap(find.byIcon(Icons.close).first);
      await tester.pump();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      expect(find.text('Restart now'), findsOneWidget);

      await _scrollToAndTap(tester, find.text('Restart now'));
      await tester.pump();

      expect(restartRequested, 1);
      // The notice (and its button) are dismissed once restart is requested.
      expect(
          find.textContaining('Restart the workspace to apply'), findsNothing);
      expect(find.text('Restart now'), findsNothing);
      await tester.pumpAndSettle();
    });
  });

  group('egress mode (#2409)', () {
    testWidgets('seeds the picker from the workspace and saves a change',
        (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/config') {
          return http.Response(jsonEncode({}), 200);
        }
        if (p == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode([
              {
                'id': _wsId,
                'name': 'my-ws',
                'image': 'klangk-pi',
                'service_command': 'pi',
                'mounts': <String>['/host:/cont'],
                'env': <String, String>{'FOO': 'bar'},
                'egress_mode': 'static',
              }
            ]),
            200,
          );
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
          return http.Response(jsonEncode({'status': 'updated'}), 200);
        }
        return http.Response('not found', 404);
      });
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Seeded from the workspace (static), not the default interactive.
      expect(find.text('static (deny + record)'), findsOneWidget);
      // Open the egress picker and switch to allow.
      await _scrollToAndTap(tester, find.text('static (deny + record)'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('allow (default-permit)').last);
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(savedBody, isNotNull);
      expect(savedBody!['egress_mode'], 'allow');
      // Drain the 2s auto-clear timer.
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets(
        'changing egress mode on a running container shows the restart notice',
        (tester) async {
      testAuthHttpClientOverride = _client(
        workspace: {
          ..._workspace,
          'running': true,
          'egress_mode': 'interactive',
        },
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // Switch interactive -> static (a create-time change).
      await _scrollToAndTap(tester, find.text('interactive (ask first)'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('static (deny + record)').last);
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(find.text('Settings saved'), findsOneWidget);
      expect(find.textContaining('Restart the workspace to apply'),
          findsOneWidget);
      await tester.pump(const Duration(seconds: 2));
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
      // The Per-handle home checkbox (#2721) shows under the deploy
      // ceiling (#3135 — armed by the default _client) — it is the only
      // checkbox when auto-start is not allowed.
      expect(find.text('Per-handle home'), findsOneWidget);
      expect(find.byType(Checkbox), findsOneWidget);
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
      // Scoped to the Auto start tile — the Per-handle home checkbox
      // (#2721) is always present too.
      final checkbox = find.descendant(
        of: find.widgetWithText(CheckboxListTile, 'Auto start'),
        matching: find.byType(Checkbox),
      );
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

    testWidgets(
        'per_handle_home seeds from the workspace and saves a flip (#2721)',
        (tester) async {
      Map<String, dynamic>? savedBody;
      testAuthHttpClientOverride = MockClient((request) async {
        final p = request.url.path;
        if (p == '/api/v1/config') {
          return http.Response(
            jsonEncode({'per_handle_home_available': true}),
            200,
          );
        }
        if (p == '/api/v1/workspaces') {
          return http.Response(
            jsonEncode([
              {
                ..._workspace,
                'egress_mode': 'interactive',
                'per_handle_home': true,
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

      // Seeded from the workspace (per-handle).
      final checkbox = find.descendant(
        of: find.widgetWithText(CheckboxListTile, 'Per-handle home'),
        matching: find.byType(Checkbox),
      );
      expect(checkbox, findsOneWidget);
      expect(tester.widget<Checkbox>(checkbox).value, isTrue);

      // Toggle to shared and save; the flip reaches the PUT body.
      await tester.ensureVisible(checkbox);
      await tester.tap(checkbox);
      await tester.pump();
      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      expect(savedBody, isNotNull);
      expect(savedBody!['per_handle_home'], false);
      // Drain the 2s auto-clear timer (see save-success test).
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
    });

    testWidgets(
        'per_handle_home hidden and omitted while the ceiling is off (#3135)',
        (tester) async {
      final savedBodies = <Map<String, dynamic>>[];
      testAuthHttpClientOverride = _client(
        perHandleHomeAvailable: false,
        workspace: {..._workspace, 'per_handle_home': true},
        saveRecorder: savedBodies,
      );
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      // The stored column is true, but the deploy forbids per-handle
      // homes — the toggle is hidden (it could only show a no-op).
      expect(find.text('Per-handle home'), findsNothing);

      await _scrollToAndTap(tester, find.text('Save'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));

      // The PUT omits the field — the stored column is left untouched
      // (inert server-side), like the sudo/nix gated toggles.
      expect(savedBodies, isNotEmpty);
      expect(savedBodies.single.containsKey('per_handle_home'), isFalse);
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

    testWidgets('export card hidden without the export permission (#2707)',
        (tester) async {
      await tester.pumpWidget(_buildPanel(canExport: false));
      await tester.pumpAndSettle();

      expect(find.text('Export Workspace'), findsNothing);
      expect(find.text('Export'), findsNothing);
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
      final posts = <String>[];
      testAuthHttpClientOverride = _client(stopRecorder: posts);
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Shut Down Container'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Cancel'));
      await tester.pumpAndSettle();

      // Dialog gone, no shutdown POST.
      expect(find.text('Shut Down'), findsNothing);
      expect(posts, isEmpty);
    });

    testWidgets('confirm sends shutdown and dismisses the dialog',
        (tester) async {
      final posts = <String>[];
      testAuthHttpClientOverride = _client(stopRecorder: posts);
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Shut Down Container'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Shut Down').last);
      await tester.pumpAndSettle();

      expect(posts, contains('/api/v1/workspaces/$_wsId/stop'));
      expect(find.text('Cancel'), findsNothing);
    });

    testWidgets('shut down failure shows a snackbar', (tester) async {
      testAuthHttpClientOverride = _client(stopStatus: 500);
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Shut Down Container'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Shut Down').last);
      await tester.pumpAndSettle();

      expect(find.textContaining('Shut down failed'), findsOneWidget);
    });

    testWidgets('shut down network error shows a snackbar', (tester) async {
      testAuthHttpClientOverride = _client(stopThrows: true);
      await tester.pumpWidget(_buildPanel());
      await tester.pumpAndSettle();

      await _scrollToAndTap(tester, find.text('Shut Down Container'));
      await tester.pumpAndSettle();

      await tester.tap(find.text('Shut Down').last);
      await tester.pumpAndSettle();

      expect(find.textContaining('network error'), findsOneWidget);
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

    testWidgets('search omits the system agent (#2892)', (tester) async {
      testAuthHttpClientOverride = _client(
        searchResults: [
          {'id': agentUserId, 'email': 'klangk@example.com'},
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

      // The agent can never own a workspace — the backend would only
      // reject the transfer — so the autocomplete filters it out.
      expect(find.text('klangk@example.com'), findsNothing);
      expect(find.text('target@test.com'), findsOneWidget);
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

    testWidgets('transfer failure with a list detail renders its msg (#3130)',
        (tester) async {
      // A Pydantic 422 detail list must render the message, not degrade
      // to a bare status code off a string-cast TypeError.
      testAuthHttpClientOverride = _client(
        searchResults: [
          {'id': 'u2', 'email': 'target@test.com', 'handle': 'target'},
        ],
        transferStatus: 422,
        transferResponse: {
          'detail': [
            {
              'type': 'value_error',
              'loc': ['body', 'email'],
              'msg': 'Value error, not a valid email',
              'input': 'target',
            }
          ]
        },
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
        find.textContaining('Transfer failed: not a valid email'),
        findsOneWidget,
      );
      expect(find.textContaining('Value error'), findsNothing);
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
