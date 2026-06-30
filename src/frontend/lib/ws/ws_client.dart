import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;
import 'package:web_socket_channel/web_socket_channel.dart';
import '../auth/auth_service.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// A single WebSocket debug log entry.
class WsDebugEntry {
  final DateTime timestamp;
  final String direction; // 'SEND' or 'RECV'
  final String summary;
  final Map<String, dynamic>? data;

  WsDebugEntry({required this.direction, required this.summary, this.data})
    : timestamp = DateTime.now();
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
  String? _defaultCommand;
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

  final _errorController = StreamController<String>.broadcast();
  final _terminalOutputController = StreamController<String>.broadcast();
  final _browserRequestController =
      StreamController<Map<String, dynamic>>.broadcast();
  final _customEventController =
      StreamController<Map<String, dynamic>>.broadcast();
  final _chatController = StreamController<Map<String, dynamic>>.broadcast();
  final _sharedTerminalDeletedController =
      StreamController<Map<String, dynamic>>.broadcast();
  final _workspacesChangedController = StreamController<void>.broadcast();
  final _containerStatusController =
      StreamController<Map<String, dynamic>>.broadcast();
  final _serviceHealthController =
      StreamController<Map<String, dynamic>>.broadcast();
  final _debugLogController = StreamController<WsDebugEntry>.broadcast();

  Stream<String> get errors => _errorController.stream;
  Stream<String> get terminalOutput => _terminalOutputController.stream;
  Stream<Map<String, dynamic>> get browserRequests =>
      _browserRequestController.stream;

  /// Buffered chat history — populated from chat_history and chat_message.
  final List<Map<String, dynamic>> chatHistory = [];

  /// Workspace members for @mention autocomplete.
  List<Map<String, dynamic>> workspaceMembers = [];

  /// Users currently connected to the workspace.
  List<Map<String, dynamic>> presenceUsers = [];

  /// Terminal windows in the current tmux session.
  List<Map<String, dynamic>> terminalWindows = [];

  /// Shared terminals available in the workspace.
  List<Map<String, dynamic>> sharedTerminals = [];

  /// Chat messages (individual and history) from the backend.
  Stream<Map<String, dynamic>> get chatMessages => _chatController.stream;

  /// Older chat history pages loaded on demand.
  final _chatHistoryPageController =
      StreamController<Map<String, dynamic>>.broadcast();
  Stream<Map<String, dynamic>> get chatHistoryPages =>
      _chatHistoryPageController.stream;

  /// Custom events from the backend (container_ready, container_stopped, etc.)
  Stream<Map<String, dynamic>> get customEvents =>
      _customEventController.stream;

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
  String? get defaultCommand => _defaultCommand;
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
      if (code == 4001 || code == 4002) {
        _errorController.add('Session expired, please log in again');
        _auth?.logout();
      } else {
        _errorController.add('Connection failed: $e');
      }
      return;
    }

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
    'error': (json) =>
        _errorController.add(json['message'] as String? ?? 'Unknown error'),
    'browser_request': _browserRequestController.add,
    'chat_message': (json) {
      chatHistory.add(json);
      _chatController.add(json);
    },
    'chat_history': _onChatHistory,
    'chat_history_page': _chatHistoryPageController.add,
    'chat_updated': _chatController.add,
    'agent_thinking': _chatController.add,
    'workspace_members': _onWorkspaceMembers,
    'presence_list': _onPresenceList,
    'presence_join': _onPresenceJoin,
    'presence_leave': _onPresenceLeave,
    'terminal_windows': _onTerminalWindows,
    'shared_terminals': _onSharedTerminals,
    'shared_terminal_deleted': _onSharedTerminalDeleted,
    'workspaces_changed': (json) => _workspacesChangedController.add(null),
    'container_status': _containerStatusController.add,
    'service_health': _serviceHealthController.add,
    'event': _customEventController.add,
  };

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
          _errorController.add('Parse error: $e');
        }
      },
      onDone: () {
        _stopHeartbeat();
        _connected = false;
        _pendingWorkspaceId ??= _currentWorkspaceId;
        _currentWorkspaceId = null;
        _defaultCommand = null;
        _userHome = null;
        presenceUsers = [];
        terminalWindows = [];
        sharedTerminals = [];
        workspaceMembers = [];
        final code = _channel?.closeCode;
        notifyListeners();
        if (code == 4001 || code == 4002) {
          _errorController.add('Session expired, please log in again');
          _auth?.logout();
        } else {
          _scheduleReconnect();
        }
      },
      onError: (e) {
        _errorController.add('WebSocket error: $e');
        _stopHeartbeat();
        _connected = false;
        _pendingWorkspaceId ??= _currentWorkspaceId;
        _currentWorkspaceId = null;
        presenceUsers = [];
        terminalWindows = [];
        sharedTerminals = [];
        workspaceMembers = [];
        notifyListeners();
        final code = _channel?.closeCode;
        if (code == 4001 || code == 4002) {
          _auth?.logout();
        } else {
          _scheduleReconnect();
        }
      },
    );
  }

  void _onContainerReady(Map<String, dynamic> json) {
    _currentWorkspaceId = json['workspaceId'] as String?;
    _currentUserId = json['userId'] as String?;
    _defaultCommand = json['defaultCommand'] as String?;
    _userHome = json['userHome'] as String?;
    _reconnecting = false;
    _reconnectAttempt = 0;
    _pendingWorkspaceId = null;
    _startHeartbeat();
    notifyListeners();
  }

  void _onChatHistory(Map<String, dynamic> json) {
    chatHistory.clear();
    final messages = json['messages'] as List? ?? [];
    for (final m in messages) {
      final msg = m as Map<String, dynamic>;
      chatHistory.add(msg);
      _chatController.add(msg);
    }
  }

  void _onWorkspaceMembers(Map<String, dynamic> json) {
    final members = json['members'] as List? ?? [];
    workspaceMembers = members.cast<Map<String, dynamic>>();
    notifyListeners();
  }

  void _onPresenceList(Map<String, dynamic> json) {
    final users = json['users'] as List? ?? [];
    presenceUsers = users.cast<Map<String, dynamic>>();
    notifyListeners();
  }

  void _onPresenceJoin(Map<String, dynamic> json) {
    final uid = json['user_id'] as String?;
    if (uid != null && !presenceUsers.any((u) => u['user_id'] == uid)) {
      presenceUsers = [
        ...presenceUsers,
        {
          'user_id': uid,
          'user_email': json['user_email'],
          'user_handle': json['user_handle'] ?? '',
        },
      ];
      notifyListeners();
    }
  }

  void _onPresenceLeave(Map<String, dynamic> json) {
    final uid = json['user_id'] as String?;
    if (uid != null) {
      presenceUsers = presenceUsers.where((u) => u['user_id'] != uid).toList();
      notifyListeners();
    }
  }

  void _onTerminalWindows(Map<String, dynamic> json) {
    debugPrint('[WsClient] terminal_windows received: ${DateTime.now()}');
    final windows = json['windows'] as List? ?? [];
    terminalWindows = windows.cast<Map<String, dynamic>>();
    notifyListeners();
  }

  void _onSharedTerminals(Map<String, dynamic> json) {
    final terminals = json['terminals'] as List? ?? [];
    sharedTerminals = terminals.cast<Map<String, dynamic>>();
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
    _send({'cmd': 'workspace_disconnect'});
    _currentWorkspaceId = null;
    chatHistory.clear();
    workspaceMembers = [];
    presenceUsers = [];
    notifyListeners();
  }

  void sendUiReady() {
    _send({'cmd': 'ui_ready'});
  }

  void sendRestartContainer() {
    _send({'cmd': 'restart_container'});
  }

  void sendShutdownContainer() {
    _send({'cmd': 'shutdown_container'});
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

  void sendChatMessage(String text) {
    _send({'cmd': 'chat_send', 'message': text});
  }

  void sendChatLoadMore(String beforeId, {int limit = 50}) {
    _send({'cmd': 'chat_load_more', 'before_id': beforeId, 'limit': limit});
  }

  void sendChatDelete(String messageId) {
    _send({'cmd': 'chat_delete', 'message_id': messageId});
  }

  void sendChatAgentAbort() {
    _send({'cmd': 'chat_agent_abort'});
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
    _terminalOutputController.close();
    _browserRequestController.close();
    _chatController.close();
    _chatHistoryPageController.close();
    _customEventController.close();
    _sharedTerminalDeletedController.close();
    _workspacesChangedController.close();
    _containerStatusController.close();
    _serviceHealthController.close();
    _debugLogController.close();
    super.dispose();
  }
}
