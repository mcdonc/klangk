import 'dart:convert';

import 'package:http/http.dart' as http;

import 'soliplex_auth_result.dart';
import 'soliplex_platform.dart';

/// Loads the `default` server's base URL (the bundled plugin asset in
/// production). Injected into [SoliplexServerRegistry] so unit tests don't need
/// a Flutter asset bundle; returns null/empty when no default is configured.
typedef DefaultUrlLoader = Future<String?> Function();

/// A configured Soliplex server the agent targets by [name] (the value it
/// passes in the `server` tool argument). [baseUrl] is the server origin with
/// any trailing slashes stripped.
class SoliplexServer {
  const SoliplexServer({required this.name, required this.baseUrl});

  final String name;
  final String baseUrl;
}

/// Per-server auth + config session: the server's base URL, a token store
/// namespaced to that server, an injectable [http.Client] (tests pass a
/// `MockClient`), and the cached OIDC token endpoint. Everything that used to
/// be module-global single-server state now lives here, one instance per
/// server, so multiple servers keep independent auth.
class SoliplexServerSession {
  SoliplexServerSession({
    required this.server,
    SoliplexTokenStore? store,
    http.Client? httpClient,
  }) : store = store ?? SoliplexTokenStore(namespace: server.name),
       httpClient = httpClient ?? http.Client();

  final SoliplexServer server;
  final SoliplexTokenStore store;
  final http.Client httpClient;

  /// Cached OIDC token endpoint from `.well-known` discovery.
  String? _tokenEndpoint;

  String get baseUrl => server.baseUrl;

  /// Discover the OIDC token endpoint from the IdP [serverUrl].
  Future<String?> _getTokenEndpoint(String serverUrl) async {
    if (_tokenEndpoint != null) return _tokenEndpoint;
    final url = serverUrl.replaceAll(RegExp(r'/+$'), '');
    final resp = await httpClient.get(
      Uri.parse('$url/.well-known/openid-configuration'),
    );
    if (resp.statusCode == 200) {
      final data = jsonDecode(resp.body) as Map<String, dynamic>;
      _tokenEndpoint = data['token_endpoint'] as String?;
    }
    return _tokenEndpoint;
  }

  /// Try to refresh the access token using the stored refresh token. Returns
  /// the new access token, or null if refresh failed.
  Future<String?> _tryRefreshToken() async {
    final refreshToken = await store.refreshToken;
    final serverUrl = await store.serverUrl;
    final clientId = await store.clientId;
    if (refreshToken == null || serverUrl == null || clientId == null) {
      return null;
    }

    final tokenEndpoint = await _getTokenEndpoint(serverUrl);
    if (tokenEndpoint == null) return null;

    final resp = await httpClient.post(
      Uri.parse(tokenEndpoint),
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: {
        'grant_type': 'refresh_token',
        'refresh_token': refreshToken,
        'client_id': clientId,
      },
    );
    if (resp.statusCode != 200) return null;

    final data = jsonDecode(resp.body) as Map<String, dynamic>;
    final newToken = data['access_token'] as String?;
    final newRefresh = data['refresh_token'] as String?;
    final expiresIn = data['expires_in'] as int?;
    if (newToken == null) return null;

    await store.writeTokens(
      accessToken: newToken,
      refreshToken: newRefresh,
      expiresAt: expiresIn != null
          ? DateTime.now().add(Duration(seconds: expiresIn))
          : null,
    );
    return newToken;
  }

  /// Clear this server's stored auth state. Called on 401s to force
  /// re-authentication via the overlay button.
  Future<void> clearStoredTokens() async {
    await store.clear();
    _tokenEndpoint = null;
  }

  /// Whether there is a valid (non-expired) access token for this server.
  Future<bool> hasValidToken() async {
    final stored = await store.accessToken;
    final expiresAt = await store.expiresAt;
    if (stored == null || stored.isEmpty || expiresAt == null) return false;
    return expiresAt.isAfter(DateTime.now().add(const Duration(seconds: 30)));
  }

  /// Whether this server is "connected" for UI purposes: it either holds a
  /// valid token, or it is an open/no-auth server the user has connected to.
  /// Open servers carry no token but are usable immediately, so the overlay
  /// shows them connected once the user clicks Connect.
  Future<bool> isConnected() async =>
      (await hasValidToken()) || (await store.openConnected);

  /// Record that this open/no-auth server has been connected to. Cleared by
  /// [clearStoredTokens] (logout), just like real tokens.
  Future<void> markOpenConnected() => store.setOpenConnected(true);

  /// Get a valid access token: cached if fresh, else silent refresh, else
  /// throw (caller directs the user to the "Connect to Soliplex" overlay).
  Future<String> getAccessToken() async {
    final stored = await store.accessToken;
    final expiresAt = await store.expiresAt;
    if (stored != null && stored.isNotEmpty && expiresAt != null) {
      if (expiresAt.isAfter(DateTime.now().add(const Duration(seconds: 30)))) {
        return stored;
      }
    }
    final refreshed = await _tryRefreshToken();
    if (refreshed != null) return refreshed;
    throw Exception(
      'Not authenticated. Click "Connect to Soliplex" to log in.',
    );
  }

  /// Request headers including the bearer token when available. A no-auth
  /// Soliplex deployment needs no bearer, so a missing token is not fatal.
  Future<Map<String, String>> headers() async {
    final headers = {
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    };
    try {
      headers['Authorization'] = 'Bearer ${await getAccessToken()}';
    } catch (_) {
      // Proceed without auth — server will return 401 if required.
    }
    return headers;
  }

  /// Fetch this server's available OIDC auth systems (id → {title, server_url,
  /// client_id, scope, ...}). An **empty** map is a valid result: it means the
  /// server is open / no-auth (`/api/login` → `{}`), NOT an error — callers
  /// must distinguish that from a fetch failure (non-200, which throws). A
  /// no-auth server is fully usable; queries just proceed without a bearer.
  Future<Map<String, dynamic>> getAuthSystems() async {
    final loginResp = await httpClient.get(Uri.parse('$baseUrl/api/login'));
    if (loginResp.statusCode != 200) {
      throw Exception('Failed to get auth systems: ${loginResp.statusCode}');
    }
    return jsonDecode(loginResp.body) as Map<String, dynamic>;
  }

  /// Interactive login for [systemId] via the platform flow (popup on web,
  /// system-browser PKCE on native); tokens persist in this server's store.
  // coverage:ignore-start — drives a real browser/IdP; not unit-testable.
  Future<SoliplexAuthResult> login(String systemId) async {
    final systems = await getAuthSystems();
    return soliplexInteractiveLogin(
      systemId: systemId,
      soliplexUrl: baseUrl,
      systems: systems,
      store: store,
    );
  }

  // coverage:ignore-end
}

/// Registry of the Soliplex servers this plugin can reach, keyed by the name
/// the agent uses in the `server` tool argument.
///
/// The `default` server's URL is resolved once from the klangk backend config
/// (`/api/v1/config` → `soliplex_url`); additional named servers are added with
/// [addServer] entirely plugin-side, so klangk never needs to know about more
/// than one Soliplex URL. The injectable [httpClient] lets tests drive the
/// config fetch (and the sessions it vends) with a `MockClient`.
class SoliplexServerRegistry {
  SoliplexServerRegistry({
    http.Client? httpClient,
    DefaultUrlLoader? defaultUrlLoader,
    SoliplexConfigStore? configStore,
  }) : _http = httpClient ?? http.Client(),
       _defaultUrlLoader = defaultUrlLoader,
       _configStore = configStore ?? SoliplexConfigStore();

  final http.Client _http;

  /// Loads the `default` server URL (bundled plugin asset in production).
  /// Injectable so tests don't need a Flutter asset bundle.
  final DefaultUrlLoader? _defaultUrlLoader;

  /// Persists the user/agent-added servers (everything except `default`).
  final SoliplexConfigStore _configStore;

  final Map<String, SoliplexServer> _servers = {};
  final Map<String, SoliplexServerSession> _sessions = {};

  /// Memoized init. A cached *future* (not a bool) so concurrent callers — e.g.
  /// the plugin constructor's auth-state refresh racing a tool handler — all
  /// await the SAME completion; `default` is registered before any returns.
  Future<void>? _initFuture;

  /// Name of the bundled default server.
  static const defaultName = 'default';

  /// Register `default` (from the bundled asset, with a legacy `/api/v1/config`
  /// fallback) and load any persisted user/agent-added servers. Idempotent and
  /// concurrency-safe. A missing/empty default still registers `default` (empty
  /// URL) so callers get a clear failure downstream rather than a hang.
  Future<void> ensureDefault() => _initFuture ??= _init();

  Future<void> _init() async {
    // Default URL: bundled asset first; fall back to the klangk
    // /api/v1/config `soliplex_url` only while klangk still ships it (transition).
    var url = '';
    try {
      url = (await _defaultUrlLoader?.call() ?? '').replaceAll(
        RegExp(r'/+$'),
        '',
      );
    } catch (_) {
      url = '';
    }
    if (url.isEmpty) {
      try {
        final resp = await _http.get(
          Uri.parse('${soliplexBackendBase()}/api/v1/config'),
        );
        if (resp.statusCode == 200) {
          final data = jsonDecode(resp.body) as Map<String, dynamic>;
          url = (data['soliplex_url'] as String? ?? '').replaceAll(
            RegExp(r'/+$'),
            '',
          );
        }
      } catch (_) {
        // Leave empty; downstream calls surface a clear "not configured" error.
      }
    }
    _servers[defaultName] = SoliplexServer(name: defaultName, baseUrl: url);

    await _loadPersistedServers();
  }

  /// Load persisted user/agent-added servers into memory (no re-persist).
  Future<void> _loadPersistedServers() async {
    String? raw;
    try {
      raw = await _configStore.readServersJson();
    } catch (_) {
      raw = null;
    }
    if (raw == null || raw.isEmpty) return;
    try {
      final list = jsonDecode(raw);
      if (list is! List) return;
      for (final entry in list) {
        if (entry is! Map) continue;
        final name = entry['name'] as String?;
        final u = entry['url'] as String?;
        if (name == null || name.isEmpty || name == defaultName) continue;
        if (u == null) continue;
        _register(name, u);
      }
    } catch (_) {
      // Corrupt persisted list — ignore rather than crash the registry.
    }
  }

  /// In-memory register (no persistence); shared by load + [addServer].
  void _register(String name, String baseUrl) {
    _servers[name] = SoliplexServer(
      name: name,
      baseUrl: baseUrl.replaceAll(RegExp(r'/+$'), ''),
    );
    _sessions.remove(name); // drop any stale session bound to the old URL
  }

  /// Persist all servers EXCEPT `default` (which comes from the asset).
  Future<void> _persist() async {
    final list = _servers.values
        .where((s) => s.name != defaultName)
        .map((s) => {'name': s.name, 'url': s.baseUrl})
        .toList();
    try {
      await _configStore.writeServersJson(jsonEncode(list));
    } catch (_) {
      // Best-effort persistence; the in-memory registry still works this session.
    }
  }

  /// Register (or replace) a named server and persist it. Reachable from the
  /// Flutter overlay (user) and the pi `soliplex_add_server` tool (agent).
  Future<void> addServer(String name, String baseUrl) async {
    await ensureDefault();
    _register(name, baseUrl);
    await _persist();
  }

  /// Remove a user/agent-added server (cannot remove `default`) and persist.
  Future<void> removeServer(String name) async {
    if (name == defaultName) return;
    await ensureDefault();
    _servers.remove(name);
    _sessions.remove(name);
    await _persist();
  }

  /// Names of all registered servers.
  List<String> get names => _servers.keys.toList(growable: false);

  /// All registered servers.
  List<SoliplexServer> get servers => _servers.values.toList(growable: false);

  /// Resolve a server by [name], ensuring the registry is loaded first.
  /// Throws [StateError] listing known names if [name] is unknown, so the
  /// agent can correct its `server` argument.
  Future<SoliplexServer> resolve(String name) async {
    await ensureDefault();
    final server = _servers[name];
    if (server == null) {
      throw StateError(
        'Unknown soliplex server "$name". Known servers: ${names.join(', ')}',
      );
    }
    return server;
  }

  /// The (cached) per-server session for [name], holding that server's
  /// namespaced auth state. Sessions share this registry's [http.Client].
  Future<SoliplexServerSession> session(String name) async {
    final server = await resolve(name);
    return _sessions[name] ??= SoliplexServerSession(
      server: server,
      httpClient: _http,
    );
  }
}
