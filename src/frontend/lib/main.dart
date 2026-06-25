import 'package:flterm/flterm.dart';
import 'package:flutter/foundation.dart' show kIsWeb;
import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:klangk_plugins/klangk_plugins.dart';
import 'package:provider/provider.dart';
import 'app.dart';
import 'auth/auth_service.dart';
import 'ws/ws_client.dart';
import 'utils/web_helpers_stub.dart'
    if (dart.library.js_interop) 'utils/web_helpers_web.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // Register plugins early so their routes are available when GoRouter
  // is created (before any workspace page is opened).
  final registry = ToolPluginRegistry();
  for (final plugin in createAllPlugins()) {
    registry.register(plugin);
  }

  if (kIsWeb) {
    // libghostty's VT runs as WebAssembly in the browser; load it once before
    // any terminal is built. The bundled binary must match the resolved
    // libghostty package version or this throws.
    await initializeForWeb(
      Uri.parse('assets/assets/libghostty-wasm32-freestanding.wasm'),
    );
  }
  // Capture the hash and query string before Flutter/GoRouter can consume
  // them. The Soliplex OAuth callback lands as ?token=...#/soliplex-auth-callback
  // — GoRouter navigates on the hash, which clears the page-level query
  // params from window.location.search. Capture them here and pass to the
  // app so plugin callback routes can read them.
  final hash = getLocationHash();
  capturePageQuery();
  final initialLocation = (hash.length > 1) ? hash.substring(1) : '/';
  runApp(
    MultiProvider(
      providers: [
        ChangeNotifierProvider(create: (_) => AuthService()),
        ChangeNotifierProxyProvider<AuthService, WsClient>(
          create: (_) => WsClient(),
          update: (_, auth, client) => client!..updateAuth(auth),
        ),
      ],
      child: KlangkApp(initialLocation: initialLocation),
    ),
  );
}
