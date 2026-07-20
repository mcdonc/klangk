import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:klangk_plugin_soliplex/plugin.dart';
import 'package:klangk_plugin_soliplex/soliplex_servers.dart';
import 'package:shared_preferences/shared_preferences.dart';

http.Response _json(Object body, [int status = 200]) => http.Response(
  jsonEncode(body),
  status,
  headers: {'content-type': 'application/json'},
);

http.Response _routes(http.Request req) {
  if (req.url.path.endsWith('/api/v1/config')) {
    return _json({'soliplex_url': 'https://api'});
  }
  if (req.url.path.endsWith('/api/login')) {
    return _json({
      'keycloak': {'title': 'Keycloak SSO'},
    });
  }
  return http.Response('x', 404);
}

/// Pump the plugin's app bar icon + overlay inside a minimal app, mirroring
/// how klangk renders them: icon in AppBar.actions, overlay in body Stack.
Future<void> pumpOverlay(WidgetTester tester, SoliplexPlugin plugin) =>
    tester.pumpWidget(
      MaterialApp(
        home: Builder(
          builder: (context) {
            return Scaffold(
              appBar: AppBar(
                actions: [
                  if (plugin.buildAppBarAction(context) != null)
                    plugin.buildAppBarAction(context)!,
                ],
              ),
              body: Stack(
                children: [
                  if (plugin.buildOverlay(context) != null)
                    plugin.buildOverlay(context)!,
                ],
              ),
            );
          },
        ),
      ),
    );

SoliplexPlugin _plugin() => SoliplexPlugin(
  registry: SoliplexServerRegistry(
    httpClient: MockClient((r) async => _routes(r)),
  ),
);

const _iconKey = ValueKey('soliplex_overlay_icon');

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => SharedPreferences.setMockInitialValues({}));

  testWidgets('app bar: renders the hub icon', (tester) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    expect(find.byKey(_iconKey), findsOneWidget);
  });

  testWidgets('expand: lists the default server with a Connect action', (
    tester,
  ) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    expect(find.text('Soliplex servers'), findsOneWidget);
    expect(find.text('default'), findsOneWidget);
    expect(
      find.byKey(const ValueKey('soliplex_connect_default')),
      findsOneWidget,
    );
  });

  testWidgets('connected default shows Logout instead of Connect', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({
      'soliplex_default_access_token': 'tok',
      'soliplex_default_expires_at': DateTime.now()
          .add(const Duration(hours: 1))
          .toIso8601String(),
    });
    await pumpOverlay(tester, _plugin());
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    expect(
      find.byKey(const ValueKey('soliplex_logout_default')),
      findsOneWidget,
    );
    expect(
      find.byKey(const ValueKey('soliplex_connect_default')),
      findsNothing,
    );
  });

  testWidgets('add-server form registers a new server row', (tester) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('soliplex_add_toggle')));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('soliplex_add_name')),
      'staging',
    );
    await tester.enterText(
      find.byKey(const ValueKey('soliplex_add_url')),
      'https://staging.example',
    );
    await tester.tap(find.byKey(const ValueKey('soliplex_add_submit')));
    await tester.pumpAndSettle();
    expect(find.text('staging'), findsOneWidget);
    expect(
      find.byKey(const ValueKey('soliplex_connect_staging')),
      findsOneWidget,
    );
  });

  testWidgets('remove button appears on user-added servers, not default', (
    tester,
  ) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    // Default server should not have a remove button.
    expect(find.byKey(const ValueKey('soliplex_remove_default')), findsNothing);
    // Add a server.
    await tester.tap(find.byKey(const ValueKey('soliplex_add_toggle')));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('soliplex_add_name')),
      'staging',
    );
    await tester.enterText(
      find.byKey(const ValueKey('soliplex_add_url')),
      'https://staging.example',
    );
    await tester.tap(find.byKey(const ValueKey('soliplex_add_submit')));
    await tester.pumpAndSettle();
    // User-added server should have a remove button.
    expect(
      find.byKey(const ValueKey('soliplex_remove_staging')),
      findsOneWidget,
    );
  });

  testWidgets('remove requires confirmation tap before deleting', (
    tester,
  ) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    // Add a server.
    await tester.tap(find.byKey(const ValueKey('soliplex_add_toggle')));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('soliplex_add_name')),
      'staging',
    );
    await tester.enterText(
      find.byKey(const ValueKey('soliplex_add_url')),
      'https://staging.example',
    );
    await tester.tap(find.byKey(const ValueKey('soliplex_add_submit')));
    await tester.pumpAndSettle();
    expect(find.text('staging'), findsOneWidget);
    // First tap: shows "Remove?" confirmation.
    await tester.tap(find.byKey(const ValueKey('soliplex_remove_staging')));
    await tester.pumpAndSettle();
    expect(find.text('Remove?'), findsOneWidget);
    expect(find.text('staging'), findsOneWidget); // still there
    // Second tap: confirms removal.
    await tester.tap(
      find.byKey(const ValueKey('soliplex_remove_confirm_staging')),
    );
    await tester.pumpAndSettle();
    // Server row should be gone.
    expect(find.text('staging'), findsNothing);
    expect(find.text('default'), findsOneWidget);
  });

  testWidgets('connect flow loads a server\'s auth systems', (tester) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('soliplex_connect_default')));
    await tester.pumpAndSettle();
    expect(find.text('Keycloak SSO'), findsOneWidget);
    expect(
      find.byKey(const ValueKey('soliplex_connect_submit')),
      findsOneWidget,
    );
  });

  testWidgets('row-level Connect button hides while auth picker is expanded', (
    tester,
  ) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    // Row-level Connect is visible before expanding the picker.
    expect(
      find.byKey(const ValueKey('soliplex_connect_default')),
      findsOneWidget,
    );
    // Expand the auth picker.
    await tester.tap(find.byKey(const ValueKey('soliplex_connect_default')));
    await tester.pumpAndSettle();
    // Row-level Connect should be gone; only the picker's Connect remains.
    expect(
      find.byKey(const ValueKey('soliplex_connect_default')),
      findsNothing,
    );
    expect(
      find.byKey(const ValueKey('soliplex_connect_submit')),
      findsOneWidget,
    );
  });

  // Open-server scenario: /api/login returns {} (no-auth server). Connecting
  // must say "no login required", NOT "Failed to load providers".
  testWidgets('no-auth server (empty /api/login) shows open, not an error', (
    tester,
  ) async {
    final plugin = SoliplexPlugin(
      registry: SoliplexServerRegistry(
        httpClient: MockClient((r) async {
          if (r.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (r.url.path.endsWith('/api/login'))
            return _json({}); // open server
          return http.Response('x', 404);
        }),
      ),
    );
    await pumpOverlay(tester, plugin);
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('soliplex_connect_default')));
    await tester.pumpAndSettle();
    expect(find.textContaining('No login required'), findsOneWidget);
    expect(find.text('Failed to load providers'), findsNothing);
    // An open server is usable immediately, so connecting marks it connected:
    // the collapsed icon goes green (authenticated) and the row flips to Logout.
    expect(plugin.authenticated, isTrue);
    expect(
      find.byKey(const ValueKey('soliplex_logout_default')),
      findsOneWidget,
    );
  });

  testWidgets('expanded overlay wraps its content in a SelectionArea', (
    tester,
  ) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    // All overlay text (server names, providers, errors) is selectable.
    expect(find.byType(SelectionArea), findsOneWidget);
  });

  testWidgets('close button (X) collapses the expanded overlay', (
    tester,
  ) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    // Expand.
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    expect(find.text('Soliplex servers'), findsOneWidget);
    // Close via the X button.
    await tester.tap(find.byKey(const ValueKey('soliplex_overlay_close')));
    await tester.pumpAndSettle();
    // Expanded panel is gone; only the collapsed icon remains.
    expect(find.text('Soliplex servers'), findsNothing);
    expect(find.byKey(_iconKey), findsOneWidget);
  });

  testWidgets('logout flips a connected server back to Connect', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({
      'soliplex_default_access_token': 'tok',
      'soliplex_default_expires_at': DateTime.now()
          .add(const Duration(hours: 1))
          .toIso8601String(),
    });
    await pumpOverlay(tester, _plugin());
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    // Should show Logout initially.
    expect(
      find.byKey(const ValueKey('soliplex_logout_default')),
      findsOneWidget,
    );
    // Tap Logout.
    await tester.tap(find.byKey(const ValueKey('soliplex_logout_default')));
    await tester.pumpAndSettle();
    // Now should show Connect instead.
    expect(
      find.byKey(const ValueKey('soliplex_connect_default')),
      findsOneWidget,
    );
    expect(find.byKey(const ValueKey('soliplex_logout_default')), findsNothing);
  });

  testWidgets('add-server form shows validation error for reserved name', (
    tester,
  ) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('soliplex_add_toggle')));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('soliplex_add_name')),
      'default',
    );
    await tester.enterText(
      find.byKey(const ValueKey('soliplex_add_url')),
      'https://x',
    );
    await tester.tap(find.byKey(const ValueKey('soliplex_add_submit')));
    await tester.pumpAndSettle();
    // The form should show the "reserved" error inline.
    expect(find.textContaining('reserved'), findsOneWidget);
  });

  testWidgets('add-server form clears after successful add', (tester) async {
    await pumpOverlay(tester, _plugin());
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('soliplex_add_toggle')));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byKey(const ValueKey('soliplex_add_name')),
      'staging',
    );
    await tester.enterText(
      find.byKey(const ValueKey('soliplex_add_url')),
      'https://staging',
    );
    await tester.tap(find.byKey(const ValueKey('soliplex_add_submit')));
    await tester.pumpAndSettle();
    // Form should be gone (replaced by the "Add server" toggle).
    expect(find.byKey(const ValueKey('soliplex_add_toggle')), findsOneWidget);
    // The new server row should appear.
    expect(find.text('staging'), findsOneWidget);
  });

  testWidgets('real fetch failure (non-200 /api/login) shows the error', (
    tester,
  ) async {
    final plugin = SoliplexPlugin(
      registry: SoliplexServerRegistry(
        httpClient: MockClient((r) async {
          if (r.url.path.endsWith('/api/v1/config')) {
            return _json({'soliplex_url': 'https://api'});
          }
          if (r.url.path.endsWith('/api/login')) {
            return http.Response('nope', 503);
          }
          return http.Response('x', 404);
        }),
      ),
    );
    await pumpOverlay(tester, plugin);
    await tester.pump();
    await tester.tap(find.byKey(_iconKey));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('soliplex_connect_default')));
    await tester.pumpAndSettle();
    expect(find.text('Failed to load providers'), findsOneWidget);
  });
}
