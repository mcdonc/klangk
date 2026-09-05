import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart' show baseUrl;
import '../auth/auth_service.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';

/// A single WebSocket debug log entry.
class WsDebugEntry {
  final DateTime timestamp;
  final String direction; // 'SEND' or 'RECV'
  final String summary;
  final Map<String, dynamic>? data;

  WsDebugEntry({required this.direction, required this.summary, this.data})
      : timestamp = DateTime.now();
}

/// An error frame from the server (or a local connection/auth failure)
/// surfaced on [WsClient.errors].
///
/// [code] is the server's machine-readable error class (#2525 capacity,
/// #2891 forbidden / not_found) — null on legacy servers and local
/// failures. [accessRevoked] is deliberately **code-driven only**: the
/// connect/restart refusal codes the server stamps for this purpose.
/// Code-less "Permission denied" texts are NOT classified as revoked —
/// sub-action denials (join/share/exec) and podman restart failures also
/// carry that wording, and mislabeling them as revocations would swap the
/// page for a view whose copy is then a lie (#2891 review).
class WsError {
  final String message;
  final String? code;

  const WsError({required this.message, this.code});

  /// Whether this error is a stamped workspace-connect / restart refusal:
  /// the user can no longer open this workspace, and retrying can never
  /// succeed. Only the machine-readable codes qualify — never the message
  /// text (see the class doc).
  bool get accessRevoked => code == 'forbidden' || code == 'not_found';

  @override
  bool operator ==(Object other) =>
      other is WsError && other.message == message && other.code == code;

  @override
  int get hashCode => Object.hash(message, code);

  @override
  String toString() => code == null ? message : '$message (code: $code)';
}

/// Manages WebSocket connection to the Klangk backend, sending commands
/// and streaming terminal output and browser bridge requests.
class WsClient extends ChangeNotifier {
  // coverage:ignore-start
  static String get _wsBaseUrl {
    final loc = Uri.base;
    final wsScheme = loc.scheme == 'https' ? 'wss' : 'ws';
    return '$wsScheme://${loc.host}:${loc.port}$baseUrl/ws';
  }
  // coverage:ignore-end

  WebSocketChannel? _channel;
  void Function()? _removeBeforeUnload;
  AuthService? _auth;
  String? _currentWorkspaceId;
  String? _currentUserId;
  String? _serviceCommand;
  String? _userHome;
  bool _connected = false;
  bool _connecting = false;
  Timer? _heartbeatTimer;

  /// Whether an automatic reconnection is in progress.
  bool _reconnecting = false;
  bool get reconnecting => _reconnecting;

  /// Current reconnect attempt number (0 when not reconnecting).
  int _reconnectAttempt = 0;
  int get reconnectAttempt => _reconnectAttempt;

  /// Whether the last disconnect was caused by an auth failure (WebSocket
  /// close codes 4001/4002/4004 — invalid/expired token, or a session
  /// under the must-change-password flag, #3172). The UI reads this to
  /// suppress the "Server unreachable" reconnect overlay and surface only
  /// the re-login path, so the two never overlap (#2227). Reset on a
  /// successful connect.
  bool _authFailed = false;
  bool get authFailed => _authFailed;

  /// Close codes that must stop reconnection and end the session (#2227,
  /// #3172). 4004 ("Password change required") logs the user out; the
  /// next login replays the forced-change flow.
  static bool isAuthCloseCode(int? code) =>
      code == 4001 || code == 4002 || code == 4004;

  Timer? _reconnectTimer;

  /// Whether auto-reconnect should be attempted on disconnect.
  /// Set to false during intentional disconnects.
  bool _autoReconnect = false;

  /// In-flight connect() future, so concurrent connect calls coalesce onto a
  /// single attempt rather than the second no-op-ing while the first is
  /// still pending (which used to race with updateAuth's auto-connect).
  Future<void>? _connectFuture;

  /// Max backoff duration in seconds.  Kept low because the HTTP pre-check
  /// is cheap and fast — we just need to detect when the server is back.
  static const int _maxBackoffSeconds = 5;

  /// Workspace ID to rejoin after reconnecting.
  String? _pendingWorkspaceId;

  /// Override for testing to inject a fake channel factory.
  @visibleForTesting
  static WebSocketChannel Function(Uri uri)? testChannelFactory;

  /// Override for testing to control reconnect backoff delay.
  @visibleForTesting
  static Duration Function(int attempt)? testBackoffOverride;

  /// Whether [userAgent] identifies Firefox. Pure (no DOM) so it is
  /// unit-tested directly; the live browser UA is read via [getUserAgent]
  /// (see [_waitForServer]).
  ///
  /// Firefox's UA contains "Firefox"; Chrome, Edge and Safari do not
  /// (Safari carries "Safari" but not "Firefox", Chrome carries
  /// "Chrome" but not "Firefox").
  @visibleForTesting
  static bool isFirefoxUserAgent(String userAgent) =>
      userAgent.contains('Firefox');

  /// Inject a pre-connected channel for testing.
  @visibleForTesting
  void connectForTest(WebSocketChannel channel) {
    _channel = channel;
    _connected = true;
    notifyListeners();
    _listenToChannel();
  }

  final _errorController = StreamController<WsError>.broadcast();
  final _terminalOutputController = StreamController<String>.broadcast();
  // #2527: host lifecycle notices (host_shutdown / server_recycle phases /
  // host_started) as a broadcast stream AND a listenable field for status
  // lines. Notifications only — never gating the reconnect machinery.
  final _hostNoticeController = StreamController<String>.broadcast();
  Stream<String> get hostNotices => _hostNoticeController.stream;
  String? _hostNotice;

  // #2661: pending server stop/recycle schedules (the `server_schedule`
  // snapshot). Listenable + broadcast stream; the banner widget renders a
  // live countdown locally from each schedule's `fire_at`, so the server
  // only needs to push this on change + periodically.
  final _serverScheduleController =
      StreamController<List<Map<String, dynamic>>>.broadcast();
  Stream<List<Map<String, dynamic>>> get serverSchedules =>
      _serverScheduleController.stream;
  List<Map<String, dynamic>>? _serverSchedules;

  /// Pending server schedules, if any (`[{id, action, fire_at, ...}]`).
  List<Map<String, dynamic>>? get serverSchedulesNow => _serverSchedules;

  /// The current host lifecycle notice, if any ('Server recycling…',
  /// 'Server shutting down'). Non-blocking: the UI surfaces it in a
  /// transient banner/status; auto-reconnect proceeds unaffected (#2527).
  String? get hostNotice => _hostNotice;
  final _browserRequestController =
      StreamController<Map<String, dynamic>>.broadcast();
  final _customEventController =
      StreamController<Map<String, dynamic>>.broadcast();
  final _sharedTerminalDeletedController =
      StreamController<Map<String, dynamic>>.broadcast();
  final _workspacesChangedController = StreamController<void>.broadcast();
  final _containerStatusController =
      StreamController<Map<String, dynamic>>.broadcast();
  final _serviceHealthController =
      StreamController<Map<String, dynamic>>.broadcast();
  final _debugLogController = StreamController<WsDebugEntry>.broadcast();

  Stream<WsError> get errors => _errorController.stream;
  Stream<String> get terminalOutput => _terminalOutputController.stream;
  Stream<Map<String, dynamic>> get browserRequests =>
      _browserRequestController.stream;

  /// Terminal windows in the current tmux session.
  List<Map<String, dynamic>> terminalWindows = [];

  /// Shared terminals available in the workspace.
  List<Map<String, dynamic>> sharedTerminals = [];

  /// Custom events from the backend (container_ready, container_stopped, etc.)
  Stream<Map<String, dynamic>> get customEvents =>
      _customEventController.stream;

  /// Whether the workspace container is currently ready, tracked from the
  /// typed `container_ready` frame and the CUSTOM `container_ready` /
  /// `container_stopped` events. The CUSTOM event is one-shot (no replay:
  /// the server holds it until `ui_ready`, then emits it once), so a
  /// terminal widget that mounts after it fired — the #2988 permission-
  /// gated Terminal tab can build after `ui_ready` when the permissions
  /// fetch loses the race to an already-running container (#3000) —
  /// catches up by reading this instead of waiting for an event that will
  /// never re-fire.
  bool _containerReady = false;

  /// See [_containerReady].
  bool get containerReady => _containerReady;

  /// Fires when a shared terminal is deleted.
  Stream<Map<String, dynamic>> get sharedTerminalDeleted =>
      _sharedTerminalDeletedController.stream;

  /// Fires when the backend signals the user's workspace set changed
  /// (created/deleted/shared/unshared), so the list page can re-fetch.
  Stream<void> get workspacesChanged => _workspacesChangedController.stream;

  /// Fires when a container starts or stops.
  Stream<Map<String, dynamic>> get containerStatus =>
      _containerStatusController.stream;
  Stream<Map<String, dynamic>> get serviceHealth =>
      _serviceHealthController.stream;

  /// Debug log of all WebSocket messages (sent and received).
  Stream<WsDebugEntry> get debugLog => _debugLogController.stream;
  bool get connected => _connected;
  String? get currentWorkspaceId => _currentWorkspaceId;
  String? get currentUserId => _currentUserId;
  String? get serviceCommand => _serviceCommand;
  String? get userHome => _userHome;

  void updateAuth(AuthService auth) {
    final wasLoggedIn = _auth?.isLoggedIn ?? false;
    _auth = auth;
    if (!auth.isLoggedIn && _connected) {
      disconnect();
      return;
    }
    // Hoist the WebSocket to open on login (not on workspace entry) so
    // the workspace list can receive `workspaces_changed` events. Only
    // kick off a connect on the logged-out -> logged-in transition to
    // avoid reconnecting on every auth-state rebuild.
    if (auth.isLoggedIn && !wasLoggedIn) {
      _autoReconnect = true;
      connect();
    }
  }

  /// HTTP base URL for pre-connect checks, derived from the page location.
  // coverage:ignore-start
  static String get _httpBaseUrl {
    final loc = Uri.base;
    return '${loc.scheme}://${loc.host}:${loc.port}$baseUrl';
  }
  // coverage:ignore-end

  /// Override for testing to inject a custom HTTP pre-check function.
  @visibleForTesting
  static Future<bool> Function()? testHttpPreCheck;

  /// Wait for the server to respond via HTTP before opening a WebSocket.
  /// This drains Firefox's FailDelayManager throttle (which only affects
  /// WebSocket connections, not HTTP) so the subsequent WS connect succeeds
  /// without a 30-60s delay.
  Future<bool> _waitForServer() async {
    if (testHttpPreCheck != null) return testHttpPreCheck!();
    if (testChannelFactory != null) return true; // coverage:ignore-line
    // coverage:ignore-start
    // The HTTP pre-check exists only to drain Firefox's FailDelayManager
    // throttle (which can delay a WebSocket reconnect by 30-60s after an
    // unclean close). Other browsers connect immediately, so skip the
    // extra round-trip (~250ms) and open the WebSocket straight away.
    if (!isFirefoxUserAgent(getUserAgent())) return true;
    try {
      final resp = await http.get(Uri.parse('$_httpBaseUrl/api/v1/config'));
      return resp.statusCode == 200;
    } catch (e) {
      debugPrint('[WsClient] server health check failed: $e');
      return false;
    }
    // coverage:ignore-end
  }

  Future<void> connect() async {
    debugPrint('[WsClient] connect() enter: ${DateTime.now()}');
    // Coalesce: if a connect is already in flight, await it rather than
    // no-op-ing (which would race callers that fire connect() back-to-back,
    // e.g. updateAuth's auto-connect followed by an explicit connect()).
    if (_connectFuture != null) {
      return _connectFuture;
    }
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    if (_connected || _connecting || _auth?.token == null) {
      debugPrint(
        '[WsClient] connect() early return: connected=$_connected '
        'connecting=$_connecting token=${_auth?.token != null}',
      );
      return;
    }

    _connecting = true;
    _connectFuture = _doConnect();
    try {
      return await _connectFuture;
    } finally {
      _connecting = false;
      _connectFuture = null;
    }
  }

  Future<void> _doConnect() async {
    debugPrint('[WsClient] _waitForServer() start: ${DateTime.now()}');
    final serverUp = await _waitForServer();
    debugPrint(
      '[WsClient] _waitForServer() done: serverUp=$serverUp ${DateTime.now()}',
    );
    if (!serverUp) {
      return;
    }

    return await _connectWs();
  }

  /// Open a WebSocket and wait for it to be ready.  Firefox's
  /// FailDelayManager may delay the connection by up to 60s after an unclean
  /// close — we just wait it out since retrying creates zombie connections.
  Future<void> _connectWs() async {
    if (testChannelFactory != null) {
      _channel = testChannelFactory!(Uri());
    } else {
      // coverage:ignore-start
      final uri = Uri.parse('$_wsBaseUrl?token=${_auth!.token}');
      debugPrint(
        '[WsClient] WebSocketChannel.connect() start: ${DateTime.now()}',
      );
      _channel = WebSocketChannel.connect(uri);
      debugPrint(
        '[WsClient] WebSocketChannel.connect() returned: ${DateTime.now()}',
      );
      // coverage:ignore-end
    }

    try {
      debugPrint('[WsClient] await channel.ready start: ${DateTime.now()}');
      await _channel!.ready;
      debugPrint('[WsClient] await channel.ready done: ${DateTime.now()}');
    } catch (e) {
      debugPrint('[WsClient] channel.ready failed: $e ${DateTime.now()}');
      final code = _channel?.closeCode;
      if (isAuthCloseCode(code)) {
        _authFailed = true;
        _errorController.add(
          const WsError(message: 'Session expired, please log in again'),
        );
        _auth?.logout();
      } else {
        debugPrint('WebSocket connection failed: $e');
        _errorController.add(
          const WsError(message: 'Connection failed. Please try again.'),
        );
      }
      return;
    }

    // A fresh successful connection clears any prior auth-failure flag
    // (#2227); reconnect overlays may show again if this connection drops.
    _authFailed = false;
    _connected = true;
    // Close cleanly on page unload so Firefox's FailDelayManager doesn't
    // treat it as a failure and throttle the next connection by up to 60s.
    _removeBeforeUnload?.call();
    _removeBeforeUnload = onBeforeUnload(() {
      _channel?.sink.close(1000, 'page unload'); // coverage:ignore-line
    });
    notifyListeners();
    _listenToChannel();
  }

  /// Dispatch table for incoming WebSocket message types.
  ///
  /// Simple pass-throughs are one-line lambdas/tear-offs; stateful
  /// branches live in named `_on…` methods so the `notifyListeners()`
  /// calls and mutations stay auditable. See #952.
  late final Map<String, void Function(Map<String, dynamic>)> _handlers = {
    'container_ready': _onContainerReady,
    'terminal_output': (json) =>
        _terminalOutputController.add(json['data'] as String? ?? ''),
    'error': (json) => _errorController.add(WsError(
          message: json['message'] as String? ?? 'Unknown error',
          code: json['code'] as String?,
        )),
    'browser_request': _browserRequestController.add,
    'terminal_windows': _onTerminalWindows,
    'shared_terminals': _onSharedTerminals,
    'shared_terminal_deleted': _onSharedTerminalDeleted,
    'workspaces_changed': (json) => _workspacesChangedController.add(null),
    'container_status': _containerStatusController.add,
    'service_health': _serviceHealthController.add,
    'event': _onCustomEvent,
    'host_shutdown': (json) => _onHostNotice('Server shutting down'),
    'host_started': (json) => _onHostNotice(null),
    'server_schedule': (json) {
      final raw = json['schedules'];
      _serverSchedules = (raw is List)
          ? raw
              .whereType<Map<String, dynamic>>()
              .map((s) => Map<String, dynamic>.from(s))
              .toList()
          : <Map<String, dynamic>>[];
      _serverScheduleController.add(_serverSchedules!);
      notifyListeners();
    },
    'server_schedule_fired': (json) {
      final action = json['action'] as String? ?? 'action';
      final what = action == 'recycle' ? 'recycle' : 'stop';
      _onHostNotice('Scheduled server $what is running…');
    },
    'server_recycle': (json) {
      final phase = json['phase'] as String? ?? '';
      if (phase == 'recycling') {
        _onHostNotice('Server recycling…');
      } else if (phase == 'draining') {
        _onHostNotice('Server preparing to recycle…');
      }
    },
  };

  /// Forward a CUSTOM event to [customEvents], tracking the container's
  /// readiness so late-mounting widgets can query it (#3000). Matches the
  /// widget-side `_handleEvent` strictness: only CUSTOM container events
  /// move the flag.
  void _onCustomEvent(Map<String, dynamic> json) {
    final event = json['event'] as Map<String, dynamic>?;
    if (event?['type'] == 'CUSTOM') {
      final name = event!['name'] as String?;
      if (name == 'container_ready') {
        _containerReady = true;
      } else if (name == 'container_stopped') {
        _containerReady = false;
      }
    }
    _customEventController.add(json);
  }

  /// Set/clear the host lifecycle notice and notify listeners (#2527).
  /// Notification only — the reconnect loop is untouched, so a restart/
  /// shutdown never visually impedes reconnection (overlays stay with the
  /// existing disconnected logic).
  void _onHostNotice(String? notice) {
    if (_hostNotice == notice) return;
    _hostNotice = notice;
    if (notice != null) _hostNoticeController.add(notice);
    notifyListeners();
  }

  void _listenToChannel() {
    _channel!.stream.listen(
      (data) {
        try {
          final json = jsonDecode(data as String) as Map<String, dynamic>;
          final type = json['type'] as String?;

          // Skip noisy terminal_output from debug log
          if (type != 'terminal_output') {
            final summary = type == 'event'
                ? 'event:${(json['event'] as Map?)?['name'] ?? '?'}'
                : type ?? '?';
            _debugLogController.add(
              WsDebugEntry(direction: 'RECV', summary: summary, data: json),
            );
          }

          _handlers[type]?.call(json);
        } catch (e) {
          _errorController.add(WsError(message: 'Parse error: $e'));
        }
      },
      onDone: () {
        _stopHeartbeat();
        _connected = false;
        // #3000: readiness tracked with the socket that reported it — a
        // remounting terminal must not start a PTY into a container this
        // connection can no longer reach.
        _containerReady = false;
        _pendingWorkspaceId ??= _currentWorkspaceId;
        _currentWorkspaceId = null;
        _serviceCommand = null;
        _userHome = null;
        terminalWindows = [];
        sharedTerminals = [];
        // #2661/#2684: a stale schedule snapshot must not survive the
        // socket that delivered it — the banner hides and the admin
        // Server tab falls back to its REST list (a live countdown that
        // can no longer be refreshed is worse than none).
        _serverSchedules = null;
        final code = _channel?.closeCode;
        final authFailure = isAuthCloseCode(code);
        // Set the auth-failure flag BEFORE notifyListeners so the UI (which
        // rebuilds on this notification) suppresses the reconnect overlay
        // immediately and shows only the re-login path (#2227).
        if (authFailure) {
          _authFailed = true;
          _reconnecting = false;
          _reconnectAttempt = 0;
        }
        notifyListeners();
        if (authFailure) {
          _errorController.add(
            const WsError(message: 'Session expired, please log in again'),
          );
          _auth?.logout();
        } else {
          _scheduleReconnect();
        }
      },
      onError: (e) {
        _errorController.add(WsError(message: 'WebSocket error: $e'));
        _stopHeartbeat();
        _connected = false;
        // #3000: same invariant as onDone — readiness does not survive the
        // socket that reported it.
        _containerReady = false;
        _pendingWorkspaceId ??= _currentWorkspaceId;
        _currentWorkspaceId = null;
        terminalWindows = [];
        sharedTerminals = [];
        _serverSchedules = null;
        final code = _channel?.closeCode;
        final authFailure = isAuthCloseCode(code);
        if (authFailure) {
          _authFailed = true;
          _reconnecting = false;
          _reconnectAttempt = 0;
        }
        notifyListeners();
        if (authFailure) {
          _auth?.logout();
        } else {
          _scheduleReconnect();
        }
      },
    );
  }

  void _onContainerReady(Map<String, dynamic> json) {
    _containerReady = true;
    _currentWorkspaceId = json['workspaceId'] as String?;
    _currentUserId = json['userId'] as String?;
    _serviceCommand = json['serviceCommand'] as String?;
    _userHome = json['userHome'] as String?;
    _reconnecting = false;
    _reconnectAttempt = 0;
    _pendingWorkspaceId = null;
    // #2527: a reconnect means the server is back — if the client missed
    // the host_started broadcast (sent before it re-established the
    // socket), clear the stale restart/shutdown notice now instead of
    // leaving it stuck (and suppressing the next cycle's snackbar via
    // the equality guard).
    _hostNotice = null;
    _startHeartbeat();
    notifyListeners();
  }

  void _onTerminalWindows(Map<String, dynamic> json) {
    debugPrint('[WsClient] terminal_windows received: ${DateTime.now()}');
    final windows = json['windows'] as List? ?? [];
    terminalWindows = List<Map<String, dynamic>>.from(windows);
    notifyListeners();
  }

  void _onSharedTerminals(Map<String, dynamic> json) {
    final terminals = json['terminals'] as List? ?? [];
    sharedTerminals = List<Map<String, dynamic>>.from(terminals);
    notifyListeners();
  }

  void _onSharedTerminalDeleted(Map<String, dynamic> json) {
    _sharedTerminalDeletedController.add(json);
    notifyListeners();
  }

  void disconnect() {
    _cancelReconnect();
    _stopHeartbeat();
    _removeBeforeUnload?.call();
    _removeBeforeUnload = null;
    // Close with 1000 (normal closure) so Firefox's FailDelayManager
    // doesn't treat it as a failure and throttle the next connection.
    _channel?.sink.close(1000, 'client disconnect');
    _channel = null;
    _connected = false;
    _connecting = false;
    _currentWorkspaceId = null;
    _containerReady = false; // see [containerReady] (#3000)
    _serverSchedules = null;
    notifyListeners();
  }

  void _send(Map<String, dynamic> msg) {
    if (_channel == null) return;
    final cmd = msg['cmd'] as String? ?? '?';
    // Skip noisy terminal_input from debug log
    if (cmd != 'terminal_input') {
      _debugLogController.add(
        WsDebugEntry(direction: 'SEND', summary: cmd, data: msg),
      );
    }
    _channel!.sink.add(jsonEncode(msg));
  }

  void connectWorkspace(String workspaceId) {
    _autoReconnect = true;
    _pendingWorkspaceId = workspaceId;
    _send({'cmd': 'workspace_connect', 'workspaceId': workspaceId});
  }

  void disconnectWorkspace() {
    // Stop any pending reconnect attempt and clear the workspace we were
    // in, but keep auto-reconnect enabled: after hoisting the WS to
    // login it must survive leaving a workspace so the list page keeps
    // receiving `workspaces_changed` events.
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _reconnecting = false;
    _reconnectAttempt = 0;
    _pendingWorkspaceId = null;
    _stopHeartbeat();
    _containerReady = false; // see [#containerReady] (#3000)
    _send({'cmd': 'workspace_disconnect'});
    _currentWorkspaceId = null;
    notifyListeners();
  }

  void sendUiReady() {
    _send({'cmd': 'ui_ready'});
  }

  void sendRestartContainer() {
    _send({'cmd': 'restart_container'});
  }

  void sendTerminalStart({int? cols, int? rows}) {
    final msg = <String, dynamic>{'cmd': 'terminal_start'};
    if (cols != null) msg['cols'] = cols;
    if (rows != null) msg['rows'] = rows;
    final bid = getBrowserId(_auth?.instanceId ?? 'default');
    if (bid.isNotEmpty) msg['browser_id'] = bid;
    _send(msg);
  }

  void sendBrowserReattach() {
    final bid = getBrowserId(_auth?.instanceId ?? 'default');
    if (bid.isNotEmpty) {
      debugPrint('[WsClient] browser_reattach: $bid'); // coverage:ignore-start
      _send({
        'cmd': 'browser_reattach',
        'browser_id': bid,
      }); // coverage:ignore-end
    }
  }

  void sendTerminalInput(String data) {
    _send({'cmd': 'terminal_input', 'data': data});
  }

  void sendTerminalResize(int cols, int rows) {
    _send({'cmd': 'terminal_resize', 'cols': cols, 'rows': rows});
  }

  void sendTerminalNewWindow({String? name}) {
    debugPrint('[WsClient] sendTerminalNewWindow: ${DateTime.now()}');
    final msg = <String, dynamic>{'cmd': 'terminal_new_window'};
    if (name != null) msg['name'] = name;
    _send(msg);
  }

  void sendTerminalSelectWindow(String windowId) {
    _send({'cmd': 'terminal_select_window', 'window_id': windowId});
  }

  void sendTerminalCloseWindow(int index) {
    _send({'cmd': 'terminal_close_window', 'index': index});
  }

  void sendTerminalRenameWindow(int index, String name) {
    _send({'cmd': 'terminal_rename_window', 'index': index, 'name': name});
  }

  void sendTerminalListWindows() {
    _send({'cmd': 'terminal_list_windows'});
  }

  void sendShareWindow(String windowId) {
    _send({'cmd': 'share_window', 'window_id': windowId});
  }

  void sendUnshareWindow(String windowId) {
    _send({'cmd': 'unshare_window', 'window_id': windowId});
  }

  void sendJoinSharedTerminal(String userId, String windowId) {
    _send({
      'cmd': 'join_shared_terminal',
      'user_id': userId,
      'window_id': windowId,
    });
  }

  /// Identity of the terminal we just requested deletion for, so the UI
  /// can skip the "deleted" snackbar for the user who initiated it.
  Map<String, String>? lastDeletedSharedTerminal;

  void sendDeleteSharedTerminal(String userId, String windowId) {
    lastDeletedSharedTerminal = {'user_id': userId, 'window_id': windowId};
    _send({
      'cmd': 'delete_shared_terminal',
      'user_id': userId,
      'window_id': windowId,
    });
  }

  void sendListSharedTerminals() {
    _send({'cmd': 'list_shared_terminals'});
  }

  void sendTerminalStop() {
    _send({'cmd': 'terminal_stop'});
  }

  void sendHeartbeat() {
    _send({'cmd': 'heartbeat'});
  }

  void sendBrowserResponse(String id, Map<String, dynamic> result) {
    _send({'cmd': 'browser_response', 'id': id, ...result});
  }

  /// Send an incremental streamed chunk for a browser_request (streaming
  /// bridge). Followed by a final [sendBrowserResponse].
  void sendBrowserChunk(String id, String delta) {
    _send({'cmd': 'browser_chunk', 'id': id, 'delta': delta});
  }

  void _scheduleReconnect() {
    if (!_autoReconnect || _reconnecting || _reconnectTimer != null) return;

    _reconnectAttempt++;
    if (_reconnectAttempt > 25) {
      _autoReconnect = false;
      _reconnecting = false;
      notifyListeners();
      return;
    }

    _reconnecting = true;
    notifyListeners();
    // coverage:ignore-start
    final delay = testBackoffOverride != null
        ? testBackoffOverride!(_reconnectAttempt)
        : _backoffDelay(_reconnectAttempt);
    // coverage:ignore-end
    _reconnectTimer = Timer(delay, _attemptReconnect);
  }

  // coverage:ignore-start
  static Duration _backoffDelay(int attempt) {
    final baseSeconds = min(1 << attempt, _maxBackoffSeconds);
    final jitter = Random().nextDouble() * baseSeconds;
    return Duration(milliseconds: ((baseSeconds + jitter) / 2 * 1000).round());
  }
  // coverage:ignore-end

  Future<void> _attemptReconnect() async {
    _reconnectTimer = null;
    // Reset before connect() so that if the WebSocket drops during
    // connect() or the subsequent connectWorkspace(), the onDone
    // handler can trigger a fresh _scheduleReconnect() cycle.
    _reconnecting = false;
    await connect();
    if (_connected && _pendingWorkspaceId != null) {
      connectWorkspace(_pendingWorkspaceId!);
    } else if (!_connected) {
      _scheduleReconnect();
    }
  }

  void _cancelReconnect() {
    _autoReconnect = false;
    _reconnecting = false;
    _reconnectAttempt = 0;
    _pendingWorkspaceId = null;
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
  }

  void _startHeartbeat() {
    _stopHeartbeat();
    _heartbeatTimer = Timer.periodic(
      const Duration(seconds: 60),
      (_) => sendHeartbeat(), // coverage:ignore-line
    );
  }

  void _stopHeartbeat() {
    _heartbeatTimer?.cancel();
    _heartbeatTimer = null;
  }

  @override
  void dispose() {
    _cancelReconnect();
    disconnect();
    _errorController.close();
    _hostNoticeController.close();
    _terminalOutputController.close();
    _browserRequestController.close();
    _customEventController.close();
    _sharedTerminalDeletedController.close();
    _workspacesChangedController.close();
    _containerStatusController.close();
    _serviceHealthController.close();
    _debugLogController.close();
    super.dispose();
  }
}
