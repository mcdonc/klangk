import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:shared_preferences/shared_preferences.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import '../branding.dart';
import 'dpop.dart';
import 'password_policy.dart';
import 'pending_redirect.dart';
import 'token_store.dart';
import 'web_client.dart';

/// Override for testing — set to intercept all HTTP calls in AuthService.
http.Client? testAuthHttpClientOverride;

class AuthService extends ChangeNotifier {
  String get _baseUrl => baseUrl;

  http.Client get _client => testAuthHttpClientOverride ?? http.Client();

  // #3196: sudo-mode (step-up) support. When a privileged admin write
  // is refused with the server's machine-readable step_up_required
  // 403, this callback collects the user's password (null = cancel);
  // the service confirms it via POST /auth/step-up and retries the
  // original request once. `previousFailed` lets the prompt say
  // "incorrect password" on retries. Wired by the app shell to a
  // password dialog over the root navigator; null (tests, before the
  // shell runs) surfaces the 403 to the caller's own error handling.
  static Future<String?> Function({bool previousFailed})? stepUpPrompt;

  String? _token;
  bool _loading = false;
  bool _initialized = false;
  String _bannerTitle = '';
  String _bannerText = '';
  bool _bannerAccepted = false;
  bool _loginBannerEveryVisit = false;
  PasswordPolicy _passwordPolicy = const PasswordPolicy();
  String _instanceId = 'default';
  bool _allowAutostart = false;
  // #2710: whether the server's browser-delegate bridge is enabled
  // (KLANGKD_BROWSER_DELEGATE_ENABLED via /config's browser_delegate_enabled).
  // The workspace connector gates its BrowserDelegate on this — a deploy
  // that disabled the bridge gets no tab registered as a bridge target.
  // Defaults to true so an old server that doesn't send the field keeps
  // the current behavior.
  bool _browserDelegateEnabled = true;
  // #2721: deploy default home layout for new workspaces
  // (KLANGKD_PER_HANDLE_HOME via /config's default_per_handle_home). The
  // create dialog pre-reflects it so an untouched form submits the
  // server's default. Null = unknown (old server / fetch failure): the
  // dialog hides the toggle and omits the field, so the server applies
  // its own default — a hiccup can never silently force a layout
  // (#2737 review).
  bool? _perHandleHomeDefault;
  // #2768: deploy-wide default classification marking
  // (KLANGKD_CLASSIFICATION_BANNER via /config's
  // default_classification_banner). The workspace page falls back to it
  // when the workspace has no own marking; empty = no banner anywhere.
  String _defaultClassificationBanner = '';
  // #1365: deploy-wide netfilter default allow-list + whether the feature
  // is armed. Surfaced via /api/v1/config so the create-workspace UI can
  // pre-fill its allowed-domains editor from the default (a workspace
  // overrides, not unions) and gate the editor on netfilter_enabled.
  List<String> _netfilterDefaultDomains = const [];
  bool _netfilterEnabled = false;

  /// #2974: deploy-level capability toggles (from the authenticated-only
  /// /config fields — moved off the /images listing). The workspace
  /// create/edit UIs read these to decide whether the nix and sudo
  /// toggles render.
  bool _nixAvailable = false;
  bool _sudoAvailable = false;

  /// #3135: whether the deploy permits per-handle homes at all
  /// (per_handle_home_available on the authenticated /config payload —
  /// KLANGKD_PER_HANDLE_HOME is a ceiling, not a default). The create
  /// dialog and settings panel hide the Per-handle home toggle when
  /// this is false: every workspace then gets the shared /home/klangk
  /// regardless of the stored column.
  bool _perHandleHomeAvailable = false;
  // #3172: server signals that the session's password was admin-chosen
  // and must be changed before any other action is possible.
  bool _mustChangePassword = false;
  Timer? _permissionTimer;
  Timer? _refreshTimer;

  /// #3230: retries a transiently failed DPoP bind every 30s while a
  /// web session sits unbound. The server refuses unbound web-minted
  /// tokens after ``KLANGKD_WEB_BIND_GRACE_SECONDS`` (default 300s); the
  /// retry keeps a benign failure inside the window instead of waiting
  /// for the next token refresh (which may be hours away). Cancels on
  /// logout, successful bind, and dispose.
  Timer? _bindRetryTimer;

  /// The bind-retry cadence; overridable in tests.
  @visibleForTesting
  static Duration bindRetryInterval = const Duration(seconds: 30);

  String? get token => _token;
  bool get isLoggedIn => _token != null;

  /// True when the session token is DPoP-bound (#3218): every
  /// authenticated request must then carry a fresh proof signed by the
  /// browser's non-extractable key. False for unbound tokens (CLI/TUI
  /// minted, or the browser could not create a key).
  bool get tokenBound => _token != null && tokenIsBound(_token!);
  bool get loading => _loading;
  bool get initialized => _initialized;
  String get bannerTitle => _bannerTitle;
  String get bannerText => _bannerText;
  bool get bannerAccepted => _bannerAccepted;

  /// Whether the consent banner must be re-accepted on every fresh app load
  /// (KLANGKD_LOGIN_BANNER_EVERY_VISIT). When true, acceptance is held in
  /// memory for the session only, so the banner re-appears on each app
  /// restart / login. When false, acceptance is cached permanently against
  /// the banner text hash (#1544).
  bool get loginBannerEveryVisit => _loginBannerEveryVisit;
  bool get bannerRequired => _bannerText.isNotEmpty && !_bannerAccepted;

  /// Full password policy (length + character-class counts, #2581) parsed
  /// from /config. Pages that validate inline should use
  /// [passwordPolicy.validate] rather than the bare length getter.
  PasswordPolicy get passwordPolicy => _passwordPolicy;
  String get instanceId => _instanceId;

  /// Whether the server permits per-workspace auto-start
  /// (KLANGKD_ALLOW_AUTOSTART). The UI gates its "Auto start" checkbox
  /// on this — setting auto_start on a server that rejects it would
  /// 400 (#1115).
  bool get allowAutostart => _allowAutostart;

  /// #2710: whether the server's browser-delegate bridge is enabled
  /// (KLANGKD_BROWSER_DELEGATE_ENABLED). The workspace page gates its
  /// BrowserDelegate on this — on a disabled deploy the tab never
  /// subscribes to bridge requests (and the server refuses them anyway).
  bool get browserDelegateEnabled => _browserDelegateEnabled;

  /// #2721: the deploy default home layout for new workspaces (true =
  /// per-handle private homes, false = shared /home/klangk). Null when
  /// unknown — the create dialog then hides the toggle and omits the
  /// field so the server default applies.
  bool? get perHandleHomeDefault => _perHandleHomeDefault;

  /// #2768: the deploy-wide default classification marking (free text;
  /// empty = none configured — no banner is rendered and no screen space
  /// is reserved). A workspace's own `classification_banner` overrides
  /// this.
  String get defaultClassificationBanner => _defaultClassificationBanner;

  /// #1365: the deploy-wide netfilter default allow-list
  /// (KLANGKD_NETFILTER_DEFAULT_DOMAINS). The create-workspace dialog
  /// pre-fills its editor from this; a workspace's own list overrides
  /// (replaces) it.
  List<String> get netfilterDefaultDomains => _netfilterDefaultDomains;

  /// #1365: whether netfilter is armed on the server (hooks dir configured).
  /// The UI shows the allowed-domains editor only when the deploy can
  /// actually enforce it.
  bool get netfilterEnabled => _netfilterEnabled;

  /// #2202: whether the per-workspace nix flag can trigger the
  /// per-workspace /nix mount — only when the backend is configured AND
  /// nix_enabled on; inert otherwise.
  bool get nixAvailable => _nixAvailable;

  /// #2017: whether the deploy allows sudo at all — the ceiling the
  /// per-workspace lock-down toggle opts out below.
  bool get sudoAvailable => _sudoAvailable;

  /// #3135: whether the deploy permits per-handle homes — the ceiling
  /// the per-workspace home-layout toggle opts in below. False (also
  /// pre-auth, where the field is absent) hides the toggle everywhere.
  bool get perHandleHomeAvailable => _perHandleHomeAvailable;

  /// #3172: the session carries an admin-chosen temporary password
  /// that must be changed before any other action. The router guard
  /// forces `/change-password` when true.
  bool get mustChangePassword => _mustChangePassword;

  /// Decode the JWT payload.
  Map<String, dynamic>? get _payload {
    if (_token == null) return null;
    try {
      final parts = _token!.split('.');
      if (parts.length != 3) return null;
      final payload = parts[1];
      final padded = payload.padRight(
        payload.length + (4 - payload.length % 4) % 4,
        '=',
      );
      final decoded = utf8.decode(base64Url.decode(padded));
      return jsonDecode(decoded) as Map<String, dynamic>;
    } catch (e) {
      // coverage:ignore-start
      debugPrint('[AuthService] decode token failed: $e');
      return null;
    } // coverage:ignore-end
  }

  String? get userId => _payload?['sub'] as String?;
  String? get email => _payload?['email'] as String?;

  /// Permissions fetched from /api/v1/my-permissions.
  Map<String, List<String>> _permissions = {};
  List<Map<String, dynamic>> _groups = [];
  bool _isAdmin = false;

  Map<String, List<String>> get permissions => _permissions;
  List<Map<String, dynamic>> get groups => _groups;

  /// Instance-admin status: the explicit `is_admin` flag from
  /// /my-permissions, derived server-side from `admins`-group
  /// membership (#2995) — not an ACL permission. The old `/admin`
  /// wildcard-marker rows are retired with the rest of the tree.
  bool get isAdmin => _isAdmin;

  /// True for wildcard admins and for holders of any delegated /admin
  /// tab permission (#2923, #2940): the principals allowed into the admin
  /// section at all. The route guard and the app-bar admin icon key off
  /// this — a delegated user sees only the tabs their ACEs grant, admins
  /// see everything.
  bool get canAdminSection =>
      isAdmin ||
      hasPermission('/users', 'manage-users') ||
      hasPermission('/invitations', 'manage-invitations') ||
      hasPermission('/groups', 'manage-groups') ||
      hasPermission('/server', 'manage-server-schedule') ||
      hasPermission('/events', 'manage-events') ||
      hasPermission('/acl', 'manage-acls');

  /// Check if the user has a specific permission on a resource.
  bool hasPermission(String resource, String permission) {
    final perms = _permissions[resource];
    if (perms == null) return false;
    return perms.contains(permission) || perms.contains('*');
  }

  AuthService() {
    _loadToken();
  }

  /// Re-fetch `/api/v1/config` and apply the result (#2768 review).
  ///
  /// Deploy defaults can change under a live session (a SIGHUP settings
  /// reload swaps KLANGKD_CLASSIFICATION_BANNER, for instance) — surfaces
  /// that re-resolve deploy-derived state (the workspace page's marking
  /// banner, on mount and on every workspaces-changed push) call this so
  /// they read the current values instead of the ones cached at login.
  Future<void> refreshDeployConfig() => _loadConfig();

  /// Fetch `/api/v1/config` and apply the result. Sends the persisted
  /// token when available so the server returns authenticated-only fields
  /// (notably the netfilter deploy allow-list + armed status, #1365) — the
  /// pre-auth payload omits them. Called at startup and after a fresh
  /// login (so a just-authenticated user picks up the fields without an
  /// app restart).
  Future<void> _loadConfig() async {
    try {
      final client = testAuthHttpClientOverride ?? http.Client();
      final resp = await client.get(
        Uri.parse('$_baseUrl/api/v1/config'),
        headers: await authHeadersFor('GET', '/api/v1/config'),
      );
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body);
        _bannerTitle = (data['login_banner_title'] as String?) ?? '';
        _bannerText = (data['login_banner'] as String?) ?? '';
        _loginBannerEveryVisit =
            (data['login_banner_every_visit'] as bool?) ?? false;
        _instanceId = (data['instance_id'] as String?) ?? 'default';
        _allowAutostart = (data['allow_autostart'] as bool?) ?? false;
        _browserDelegateEnabled =
            (data['browser_delegate_enabled'] as bool?) ?? true;
        _perHandleHomeDefault = data['default_per_handle_home'] as bool?;
        _defaultClassificationBanner =
            (data['default_classification_banner'] as String? ?? '').trim();
        _netfilterDefaultDomains =
            (data['netfilter_default_domains'] as List?)?.cast<String>() ??
                const [];
        _netfilterEnabled = (data['netfilter_enabled'] as bool?) ?? false;
        _nixAvailable = (data['nix_available'] as bool?) ?? false;
        _sudoAvailable = (data['sudo_available'] as bool?) ?? false;
        _perHandleHomeAvailable =
            (data['per_handle_home_available'] as bool?) ?? false;
        _passwordPolicy = PasswordPolicy.fromConfig(data);
        // White-label values — mirrored into the Branding helper so widgets
        // that don't have an AuthService context (e.g. the app-bar logo,
        // page title) can read them synchronously. Covers the product name
        // (#1149) and the logo URL override (#1152).
        Branding.applyConfig(data);
      }
    } catch (e) {
      // coverage:ignore-start
      debugPrint('[AuthService] load config failed: $e');
    } // coverage:ignore-end
  }

  Future<void> _loadToken() async {
    // #3193: sessionStorage on the web (dies with the tab/browser),
    // SharedPreferences elsewhere.
    _token = await readToken();

    await _restoreBinding();
    await _loadConfig();

    if (_bannerText.isNotEmpty) {
      if (_loginBannerEveryVisit) {
        // Every-visit mode: acceptance never persists across app loads,
        // so the banner shows on each fresh start / login regardless of any
        // stored hash (#1544).
        _bannerAccepted = false;
      } else {
        final prefs = await SharedPreferences.getInstance();
        final acceptedHash = prefs.getString('klangk_banner_accepted');
        _bannerAccepted = acceptedHash == _bannerText.hashCode.toString();
      }
    }

    if (_token != null) {
      await _fetchPermissions();
      _scheduleTokenRefresh();
    }

    _initialized = true;
    notifyListeners();
  }

  /// Reconcile a persisted token with the DPoP binding key (#3218).
  ///
  /// A bound token whose key is gone (wiped IndexedDB, a different
  /// browser profile) is unusable — every proof would fail — so it is
  /// dropped and the user re-logs in. An unbound token (minted before
  /// #3218, or a bind that failed mid-session) is bound now, so the
  /// upgrade path heals on the first app load; a web build whose heal
  /// fails re-attempts on the #3230 retry timer.
  Future<void> _restoreBinding() async {
    if (_token == null) return;
    final hasKey = await dpopBackend.ensureKey();
    if (tokenBound && !hasKey) {
      debugPrint(
        '[AuthService] bound token without a key; forcing re-login',
      );
      await _clearToken();
      return;
    }
    if (!tokenBound && hasKey) {
      final bound = await _tryBind(_token!);
      if (bound != null && bound != _token) {
        _token = bound;
        await writeToken(bound);
      } else if (!tokenIsBound(_token!)) {
        _scheduleBindRetry();
      }
    }
  }

  /// #3230: the HTTP status of the last bind attempt (null on success
  /// or a network error). A 4xx refusal is permanent — retrying every
  /// 30s cannot fix an already-bound or invalid-key state.
  int? _lastBindStatus;

  /// Exchange [token] for a DPoP-bound replacement (#3218).
  ///
  /// Returns the bound token, or null when binding is unavailable
  /// (non-web / insecure context), the server refuses, or the call
  /// fails. The caller keeps the unbound token — on web builds the
  /// server limits how long that lasts (#3230: an unbound web-minted
  /// session is refused after the bind grace window, so the user
  /// re-logs in; the retry timer re-attempts in the meantime).
  Future<String?> _tryBind(String token) async {
    if (tokenIsBound(token)) return token;
    final jwk = await dpopBackend.publicJwk();
    if (jwk == null) return null;
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/api/v1/auth/bind'),
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer $token',
        },
        body: jsonEncode({'jwk': jwk}),
      );
      _lastBindStatus = response.statusCode;
      if (response.statusCode == 200) {
        return (jsonDecode(response.body)['access_token']) as String?;
      }
      debugPrint(
        '[AuthService] DPoP bind refused: '
        '${response.statusCode} ${response.body}',
      );
    } catch (e) {
      _lastBindStatus = null;
      debugPrint('[AuthService] DPoP bind failed: $e');
    }
    return null;
  }

  /// Fetch permissions from the server.
  Future<void> _fetchPermissions() async {
    debugPrint('[AuthService] fetching /api/v1/my-permissions');
    try {
      final resp = await _client.get(
        Uri.parse('$_baseUrl/api/v1/my-permissions'),
        headers: await authHeadersFor('GET', '/api/v1/my-permissions'),
      );
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as Map<String, dynamic>;
        final permsRaw = data['permissions'] as Map<String, dynamic>? ?? {};
        _permissions = permsRaw.map(
          (k, v) => MapEntry(k, List<String>.from(v as List)),
        );
        _groups = List<Map<String, dynamic>>.from(
          data['groups'] as List? ?? [],
        );
        _isAdmin = data['is_admin'] == true;
      } else if (resp.statusCode == 401) {
        await _clearToken();
      }
    } catch (e) {
      // coverage:ignore-start
      debugPrint('[AuthService] fetch permissions failed: $e');
    } // coverage:ignore-end
  }

  /// Refresh permissions from the server (call after group changes).
  Future<void> refreshPermissions() async {
    await _fetchPermissions();
    notifyListeners();
  }

  void _stopPermissionRefresh() {
    _permissionTimer?.cancel();
    _permissionTimer = null;
  }

  Future<void> acceptBanner() async {
    if (_bannerText.isEmpty) return;
    // In every-visit mode the acceptance is session-only (in-memory),
    // so we don't persist a hash — the banner re-prompts on the next app
    // load (#1544).
    if (!_loginBannerEveryVisit) {
      final prefs = await SharedPreferences.getInstance();
      await prefs.setString(
        'klangk_banner_accepted',
        _bannerText.hashCode.toString(),
      );
    }
    _bannerAccepted = true;
    notifyListeners();
  }

  Future<void> _saveToken(String token) async {
    _token = token;
    await writeToken(token);
    // Bind the fresh token to the browser's non-extractable key right
    // away (#3218): the unbound token exists in JS-readable form only
    // for this instant. On any bind failure the unbound token keeps
    // working — on web builds only until the server's #3230 bind
    // deadline; the retry timer below re-attempts inside the window.
    final bound = await _tryBind(token);
    // The bind await can straddle a logout: never write a token the
    // session has moved past (#3230 review — same guard as _retryBind).
    if (_token != token) return;
    if (bound != null && bound != token) {
      _stopBindRetry();
      _token = bound;
      await writeToken(bound);
    } else if (!tokenIsBound(token)) {
      _scheduleBindRetry();
    }
    // Re-fetch config now that we have a token, so authenticated-only
    // fields (e.g. the netfilter deploy allow-list, #1365) are picked up
    // without an app restart.
    await _loadConfig();
    await _fetchPermissions();
    _scheduleTokenRefresh();
    notifyListeners();
  }

  /// Save a token from email verification (public for VerifyPage).
  Future<void> saveTokenFromVerification(String token) async {
    await _saveToken(token);
  }

  /// #3230: retry a failed bind every 30s while a web session is
  /// unbound (see [_bindRetryTimer]). A successful retry persists the
  /// bound replacement; a still-unbound session re-arms the timer —
  /// the server enforces the deadline regardless, so the loop ends
  /// either bound or logged out. A session that cannot ever bind
  /// (no key — insecure-context build) stops retrying instead of
  /// spinning on a no-op.
  Future<void> _retryBind() async {
    if (_token == null || tokenBound) return;
    final token = _token!;
    final bound = await _tryBind(token);
    // The await can straddle a logout or a newer mint: never resurrect
    // a token the session has moved past (or dropped).
    if (_token != token) return;
    if (bound != null && bound != token) {
      _bindRetryTimer?.cancel();
      _token = bound;
      await writeToken(bound);
      return;
    }
    if (await dpopBackend.publicJwk() == null) return;
    final status = _lastBindStatus;
    if (status != null && status >= 400 && status < 500) return;
    _scheduleBindRetry();
  }

  void _scheduleBindRetry() {
    _bindRetryTimer?.cancel();
    if (!isWebClient || _token == null) return;
    _bindRetryTimer = Timer(bindRetryInterval, _retryBind);
  }

  void _stopBindRetry() {
    _bindRetryTimer?.cancel();
    _bindRetryTimer = null;
  }

  Future<void> _clearToken() async {
    _refreshTimer?.cancel();
    _refreshTimer = null;
    _token = null;
    _permissions = {};
    _groups = [];
    _isAdmin = false;
    _mustChangePassword = false;
    // The pending redirect belongs to the session being cleared; drop it
    // so the next login can never inherit the old session's destination
    // (#2670). If the user was on a protected page, guardAuth re-stashes
    // the current URI on the redirect that follows, preserving the
    // same-user "resume where you were" behavior after a token expiry.
    pendingRedirect = null;
    _stopPermissionRefresh();
    _stopBindRetry();
    await clearToken();
    notifyListeners();
  }

  Map<String, String> get _authHeaders => {
        'Content-Type': 'application/json',
        if (_token != null) 'Authorization': 'Bearer $_token',
      };

  /// Auth headers for one request, carrying a fresh DPoP proof when
  /// the token is bound (#3218). [method] is the HTTP verb and [path]
  /// the request path (as in `authGet`/`authPost`); callers issuing
  /// their own requests outside the `http` package (streaming export,
  /// uploads, file fetches) use this instead of assembling Bearer
  /// headers by hand, so bound sessions keep proving possession.
  /// [mint] adds the web-client marker for session-minting calls
  /// (#3230) — the server then bakes the DPoP bind deadline into the
  /// token it returns.
  Future<Map<String, String>> authHeadersFor(
    String method,
    String path, {
    bool mint = false,
  }) async {
    final headers = Map<String, String>.of(_authHeaders);
    if (mint) headers.addAll(await mintHeaders());
    if (_token == null) return headers;
    final proof = await dpopBackend.createProof(
      method: method,
      uri: backendVisiblePath('$_baseUrl$path'),
      accessToken: _token!,
    );
    if (proof != null) headers['DPoP'] = proof;
    return headers;
  }

  /// Stable user-facing text for a transport failure (#3203): the raw
  /// exception (URLs, endpoint paths, transport messages) goes to the
  /// log only — never into error UI. Mirrors the fixed wording the
  /// standalone auth pages already use.
  String _networkError(String action, Object error) {
    debugPrint('[AuthService] $action request failed: $error');
    return 'Network error. Please try again.';
  }

  Future<String?> register(String email, String password) async {
    _loading = true;
    notifyListeners();
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/api/v1/auth/register'),
        headers: {
          'Content-Type': 'application/json',
          ...await mintHeaders(),
        },
        body: jsonEncode({'email': email, 'password': password}),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        if (data['access_token'] != null) {
          // Test mode: auto-verified, log in immediately
          await _saveToken(data['access_token']);
          return null;
        }
        // Production: verification email sent
        return 'Check your email to verify your account.';
      }
      final error = jsonDecode(response.body);
      return error['detail'] ?? 'Registration failed';
    } catch (e) {
      return _networkError('register', e);
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  Future<String?> login(String email, String password) async {
    _loading = true;
    notifyListeners();
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/api/v1/auth/login'),
        headers: {
          'Content-Type': 'application/json',
          ...await mintHeaders(),
        },
        body: jsonEncode({'identifier': email, 'password': password}),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        _mustChangePassword = (data['must_change_password'] as bool?) ?? false;
        await _saveToken(data['access_token']);
        return null;
      }
      final error = jsonDecode(response.body);
      return error['detail'] ?? 'Login failed';
    } catch (e) {
      return _networkError('login', e);
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  /// No-auth single-user mode: fetch a free token for the seeded default
  /// user with no credentials (#1374). Returns null on success (the token
  /// is saved and AuthService notifies listeners), or an error string.
  Future<String?> localLogin() async {
    _loading = true;
    notifyListeners();
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/api/v1/auth/local'),
        headers: await mintHeaders(),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        await _saveToken(data['access_token']);
        return null;
      }
      final error = jsonDecode(response.body);
      return error['detail'] ?? 'Local login failed';
    } catch (e) {
      return _networkError('local login', e);
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  /// Clear the forced-change flag after a successful password change
  /// (#3172). Called by the change-password UI after the server returns
  /// 200.
  void clearMustChangePassword() {
    _mustChangePassword = false;
    notifyListeners();
  }

  Future<String?> resendVerification(String email, String password) async {
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/api/v1/auth/resend-verification'),
        headers: {'Content-Type': 'application/json'},
        body: jsonEncode({'email': email, 'password': password}),
      );
      if (response.statusCode == 200) {
        return null;
      }
      final error = jsonDecode(response.body);
      return error['detail'] ?? 'Failed to resend';
    } catch (e) {
      return _networkError('resend verification', e);
    }
  }

  /// Make an authenticated HTTP request. If the response is 401,
  /// clear the token (router will redirect to login).
  /// Shared post-flight handling for authenticated requests.
  ///
  /// A 401 ends the session. A 403 "Password change required" (#3172)
  /// flips the local must-change flag so guardForcedPasswordChange
  /// routes to /change-password on the next notifyListeners — this is
  /// how a mid-session admin password reset reaches the UI without
  /// waiting for the next token refresh.
  Future<void> _handleAuthFailure(http.Response response) async {
    if (response.statusCode == 401) {
      await _clearToken();
      return;
    }
    if (response.statusCode != 403 || _token == null) return;
    try {
      final detail = jsonDecode(response.body)['detail'];
      if (detail == 'Password change required' && !_mustChangePassword) {
        _mustChangePassword = true;
        notifyListeners();
      }
    } catch (_) {
      // Not JSON (or no detail) — leave the flag alone.
    }
  }

  /// True when [response] is the server's machine-readable step-up
  /// 403 (#3196) — `detail` is an object with `error:
  /// 'step_up_required'`.
  static bool _isStepUpRequired(http.Response response) {
    if (response.statusCode != 403) return false;
    try {
      final detail = jsonDecode(response.body)['detail'];
      return detail is Map && detail['error'] == 'step_up_required';
    } catch (_) {
      return false;
    }
  }

  /// Confirm [password] with the server (POST /auth/step-up, #3196).
  ///
  /// Returns the response status code — 200 when the confirmation
  /// was stamped, 401 for a wrong password, null on a network error.
  /// Deliberately does NOT route through the auth* wrappers (no retry
  /// recursion) and does not touch the token — the elevated state
  /// lives on the server's session row, not in a client-held
  /// credential.
  Future<int?> stepUp(String password) async {
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/api/v1/auth/step-up'),
        // DPoP proof, like every authed call: the web client binds its
        // token post-login (#3218), and a bound token without a proof
        // is rejected — sudo-mode would 401 on every attempt. Found by
        // the fmtk e2e auth suite (#3233).
        headers: await authHeadersFor('POST', '/api/v1/auth/step-up'),
        body: jsonEncode({'password': password}),
      );
      return response.statusCode;
    } catch (_) {
      return null;
    }
  }

  /// Send a write request with step-up retry (#3196).
  ///
  /// On the step_up_required 403: prompt (when a prompt is wired),
  /// confirm the password, and re-send the same request — up to three
  /// prompts, so a typo re-prompts (flagged) instead of dead-ending.
  /// A cancelled prompt or an exhausted retry returns the refusing
  /// response for the caller's error handling.
  Future<http.Response> _withStepUp(
    Future<http.Response> Function() send,
  ) async {
    final response = await send();
    if (!_isStepUpRequired(response)) return response;
    final prompt = stepUpPrompt;
    if (prompt == null) return response;
    for (var attempt = 0; attempt < 3; attempt++) {
      final password = await prompt(previousFailed: attempt > 0);
      if (password == null || password.isEmpty) return response;
      final status = await stepUp(password);
      if (status == 200) return await send();
      // Only a wrong password (401) re-prompts; a disabled window,
      // lockout, or network error surfaces the original 403.
      if (status != 401) return response;
    }
    return response;
  }

  Future<http.Response> authGet(String path) async {
    final response = await _client.get(
      Uri.parse('$_baseUrl$path'),
      headers: await authHeadersFor('GET', path),
    );
    await _handleAuthFailure(response);
    return response;
  }

  Future<http.Response> authPost(String path,
      {String? body, bool mint = false}) async {
    final response = await _withStepUp(
      () async => await _client.post(
        Uri.parse('$_baseUrl$path'),
        headers: await authHeadersFor('POST', path, mint: mint),
        body: body,
      ),
    );
    await _handleAuthFailure(response);
    return response;
  }

  Future<http.Response> authPatch(String path, {String? body}) async {
    final response = await _withStepUp(
      () async => await _client.patch(
        Uri.parse('$_baseUrl$path'),
        headers: await authHeadersFor('PATCH', path),
        body: body,
      ),
    );
    await _handleAuthFailure(response);
    return response;
  }

  Future<http.Response> authPut(String path, {String? body}) async {
    final response = await _withStepUp(
      () async => await _client.put(
        Uri.parse('$_baseUrl$path'),
        headers: await authHeadersFor('PUT', path),
        body: body,
      ),
    );
    await _handleAuthFailure(response);
    return response;
  }

  Future<http.Response> authDelete(String path) async {
    final response = await _withStepUp(
      () async => await _client.delete(
        Uri.parse('$_baseUrl$path'),
        headers: await authHeadersFor('DELETE', path),
      ),
    );
    await _handleAuthFailure(response);
    return response;
  }

  /// Schedule a token refresh at 80% of the token's remaining lifetime.
  void _scheduleTokenRefresh() {
    _refreshTimer?.cancel();
    _refreshTimer = null;
    final exp = _payload?['exp'] as int?;
    if (exp == null) return;
    final expiryMs = exp * 1000;
    final nowMs = DateTime.now().millisecondsSinceEpoch;
    final remainingMs = expiryMs - nowMs;
    if (remainingMs <= 0) return;
    final refreshInMs = (remainingMs * 0.8).round();
    debugPrint(
      '[AuthService] scheduling token refresh in ${refreshInMs ~/ 1000}s',
    );
    _refreshTimer = Timer(Duration(milliseconds: refreshInMs), _refreshToken);
  }

  /// Call POST /api/v1/auth/refresh to get a new token.
  Future<void> _refreshToken() async {
    if (_token == null) return;
    debugPrint('[AuthService] refreshing token');
    try {
      final response = await _client.post(
        Uri.parse('$_baseUrl/api/v1/auth/refresh'),
        headers: await authHeadersFor('POST', '/api/v1/auth/refresh'),
      );
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body);
        final newToken = data['access_token'] as String?;
        if (newToken != null) {
          // #3172: the refresh response carries the live
          // must-change flag (e.g. an admin reset the password
          // mid-session). Set it BEFORE _saveToken so its
          // notifyListeners — which re-runs the router guards —
          // sees the new value.
          _mustChangePassword =
              (data['must_change_password'] as bool?) ?? false;
          await _saveToken(newToken);
        }
      } else if (response.statusCode == 401) {
        await _clearToken();
      }
    } catch (e) {
      // Network error — retry in 60 seconds
      debugPrint('[AuthService] refresh token failed: $e, retrying in 60s');
      _refreshTimer = Timer(const Duration(seconds: 60), _refreshToken);
    }
  }

  /// Expose refresh for testing.
  @visibleForTesting
  Future<void> testRefreshToken() => _refreshToken();

  /// Log out. Returns the IdP logout URL if the provider requires
  /// a redirect, or null for local-only logout.
  Future<String?> logout() async {
    String? oidcLogoutUrl;
    // Skip the server call when there is no token left to revoke (#2687):
    // WS auth failures (4001/4002) call logout() repeatedly, and a
    // tokenless POST would only add noise to the access log.
    if (_token != null) {
      try {
        final resp = await _client.post(
          Uri.parse('$_baseUrl/api/v1/auth/logout'),
          headers: await authHeadersFor('POST', '/api/v1/auth/logout'),
        );
        if (resp.statusCode == 200) {
          final data = jsonDecode(resp.body);
          oidcLogoutUrl = data['oidc_logout_url'] as String?;
        }
      } catch (e) {
        debugPrint('[AuthService] logout request failed: $e');
      }
    }
    await _clearToken();
    return oidcLogoutUrl;
  }

  @override
  void dispose() {
    _refreshTimer?.cancel();
    _stopBindRetry();
    _stopPermissionRefresh();
    super.dispose();
  }
}
