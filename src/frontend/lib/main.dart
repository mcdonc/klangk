import 'dart:convert';
import 'package:flterm/flterm.dart';
import 'package:flutter/foundation.dart' show kIsWeb, kReleaseMode;
import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';
import 'package:http/http.dart' as http;
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:klangk_features/klangk_features.dart';
import 'package:mcp_toolkit/mcp_toolkit.dart';
import 'package:provider/provider.dart';
import 'app.dart';
import 'auth/auth_service.dart';
import 'workspace/host_services.dart';
import 'workspace_tab_filter.dart';
import 'ws/ws_client.dart';
import 'utils/web_helpers_stub.dart'
    if (dart.library.js_interop) 'utils/web_helpers_web.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  // First-party fonts only (#3149): the CSP ships with font-src 'self', so a
  // runtime fonts.gstatic.com fetch would be blocked anyway. Disabling
  // runtime fetching makes any future drift (a google_fonts call for a family
  // that is not bundled — e.g. sheetifye's tokens) fail loudly at the asset
  // lookup instead of silently attempting the network. Bundled families:
  // JetBrains Mono, Noto Emoji, RobotoMono (the exact family name sheetifye's
  // GoogleFonts.robotoMono() resolves against the asset manifest).
  GoogleFonts.config.allowRuntimeFetching = false;

  // flutter-mcp-toolkit debug bridge (#2868): registers the ext.mcp.toolkit.*
  // VM service extensions that the `fmtk` CLI (in the devenv shell) uses to
  // inspect and drive this app during development — semantic snapshots,
  // widget details, taps/typing, hot reload, logs. Const-folded out of
  // release builds (kReleaseMode), and inert in widget tests: the extensions
  // idle until an MCP client calls them. See AGENTS.md "Inspecting the
  // running frontend".
  if (!kReleaseMode) {
    MCPToolkitBinding.instance
      ..initialize()
      ..initializeFlutterToolkit();
  }

  // Register features early so their routes are available when GoRouter
  // is created (before any workspace page is opened). Active-set filter
  // (#1655): the deploy's chosen features are resolved against the sibling
  // features.json + the features_enable knob from /api/config, and only the
  // active features are registered — a shipped-but-inactive feature's Dart
  // is in the monolithic bundle but inert (no app-bar icon, overlay, routes,
  // or dispatched tools).
  final activeFeatureNames = await _resolveActiveFeatures();
  final registry = ToolPluginRegistry();
  // activeFeatureNames is a Set<String>, so .contains() is exact-name
  // equality (not substring) — "git" does NOT activate "git-credential".
  for (final entry in createAllNamedFeatures()) {
    if (activeFeatureNames.contains(entry.name)) {
      registry.register(entry.feature);
    }
  }

  // Feature-contributed workspace tabs (#1975): the same active-set filter
  // as tool plugins — a feature's tab mounts only when the feature is
  // active. A feature may contribute a tab with no tool handlers
  // (tab-only), or both a tab and tool handlers. The filter lives in
  // [registerActiveWorkspaceTabs] (a top-level helper) so it is unit-testable
  // — the tool-plugin filter is the same shape but inlined.
  registerActiveWorkspaceTabs(
    createAllNamedWorkspaceTabs(),
    activeFeatureNames,
  );

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
  // app so feature callback routes can read them.
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
        // Built once: WsClient and AuthService are stable singletons (their
        // providers create one instance and mutate it in place), so an adapter
        // holding references to them is valid for the app lifetime. Provider
        // (not ProxyProvider2) avoids reallocating it on every WsClient
        // notifyListeners (#1976 review nit).
        Provider<WorkspaceServices>(
          create: (ctx) => HostWorkspaceServices(
            ctx.read<AuthService>(),
          ),
        ),
      ],
      child: KlangkApp(initialLocation: initialLocation),
    ),
  );
}

/// Resolve the deploy's active-feature set (#1655). Canonical semantics:
///
/// - `/api/config` carries `features_enable` (a comma-separated string) when
///   the deploy pinned an explicit list → that list, verbatim, split on commas.
/// - `features_enable` unset → the sibling `features.json` `defaults` list
///   (the stock known-good set baked into the wheel).
/// - Neither available (pre-build, or a wheel without the manifest) → every
///   compiled-in feature stays active (no filtering — back-compat).
///
/// Runs once at boot before feature registration. A feature shipped but not
/// active never registers — its app-bar icon, overlay, routes, and dispatched
/// tools are all gated on registration. See #1655 for the full rationale
/// (single-client features ship dormant and opt in per-deploy).
Future<Set<String>> _resolveActiveFeatures() async {
  // 1. /api/config — features_enable knob (deploy-chosen list).
  String? featuresEnable;
  try {
    // Bound the wait so a slow/hung server can't hang app boot
    // indefinitely — on timeout we fall through to the manifest
    // defaults (or all-compiled-in if no manifest).
    final response = await http
        .get(Uri.parse('$baseUrl/api/v1/config'))
        .timeout(const Duration(seconds: 5));
    if (response.statusCode == 200) {
      final data = jsonDecode(response.body);
      if (data is Map<String, dynamic>) {
        final v = data['features_enable'];
        if (v is String && v.trim().isNotEmpty) featuresEnable = v;
      }
    }
  } catch (_) {
    // Network failure reaching /api/config at boot is non-fatal — fall
    // through to the manifest defaults, or all-compiled-in if no manifest.
  }
  if (featuresEnable != null) {
    return featuresEnable
        .split(',')
        .map((s) => s.trim())
        .where((s) => s.isNotEmpty)
        .toSet();
  }

  // 2. Sibling features.json — `defaults` list (the stock set).
  final manifest = await fetchFeaturesManifest();
  if (manifest != null) {
    final defaults = manifest['defaults'];
    if (defaults is List) {
      final names = defaults.whereType<String>().toSet();
      if (names.isNotEmpty) return names;
    }
  }

  // 3. No manifest, no knob — every compiled-in feature active (back-compat).
  // Union tools + tabs: a tab-only feature has no createAllNamedFeatures()
  // entry, so deriving the active set from tools alone would silently drop
  // its tab (#1975).
  return {
    ...createAllNamedFeatures().map((e) => e.name),
    ...createAllNamedWorkspaceTabs().map((e) => e.name),
  };
}
