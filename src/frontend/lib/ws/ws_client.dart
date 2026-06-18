import 'dart:async';
import 'dart:convert';
import 'dart:math';
import 'package:flutter/foundation.dart';
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
  bool _connected = false;
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

  /// Max backoff duration in seconds.
  static const int _maxBackoffSeconds = 30;

  /// Give up auto-reconnecting after this duration.
  static const Duration _reconnectTimeout = Duration(minutes: 5);

  /// When the current reconnect cycle started.
  DateTime? _reconnectStartedAt;

  /// Workspace ID to rejoin after reconnecting.
  String? _pendingWorkspaceId;

  /// Override for testing to inject a fake channel factory.
  @visibleForTesting
  static WebSocketChannel Function(Uri uri)? testChannelFactory;

  /// Override for testing to control reconnect backoff delay.
  @visibleForTesting
  static Duration Function(int attempt)? testBackoffOverride;

  /// Override for testing to shorten the reconnect timeout.
  @visibleForTesting
  static Duration? testReconnectTimeout;

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

  /// Debug log of all WebSocket messages (sent and received).
  Stream<WsDebugEntry> get debugLog => _debugLogController.stream;
  bool get connected => _connected;
  String? get currentWorkspaceId => _currentWorkspaceId;
  String? get currentUserId => _currentUserId;
  String? get defaultCommand => _defaultCommand;

  void updateAuth(AuthService auth) {
    _auth = auth;
    if (!auth.isLoggedIn && _connected) {
      disconnect();
    }
  }

  Future<void> connect() async {
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    if (_connected || _auth?.token == null) return;

    if (testChannelFactory != null) {
      _channel = testChannelFactory!(Uri());
    } else {
      // coverage:ignore-start
      final uri = Uri.parse('$_wsBaseUrl?token=${_auth!.token}');
      _channel = WebSocketChannel.connect(uri);
      // coverage:ignore-end
    }

    try {
      await _channel!.ready;
    } catch (e) {
      _errorController.add('Connection failed: $e');
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

          if (type == 'workspace_ready') {
            _currentWorkspaceId = json['workspaceId'] as String?;
            _currentUserId = json['userId'] as String?;
            _defaultCommand = json['defaultCommand'] as String?;
            _reconnecting = false;
            _reconnectAttempt = 0;
            _reconnectStartedAt = null;
            _pendingWorkspaceId = null;
            _startHeartbeat();
            notifyListeners();
          } else if (type == 'terminal_output') {
            _terminalOutputController.add(json['data'] as String? ?? '');
          } else if (type == 'error') {
            _errorController.add(json['message'] as String? ?? 'Unknown error');
          } else if (type == 'browser_request') {
            _browserRequestController.add(json);
          } else if (type == 'chat_message') {
            chatHistory.add(json);
            _chatController.add(json);
          } else if (type == 'chat_history') {
            final messages = json['messages'] as List? ?? [];
            for (final m in messages) {
              final msg = m as Map<String, dynamic>;
              chatHistory.add(msg);
              _chatController.add(msg);
            }
          } else if (type == 'chat_history_page') {
            _chatHistoryPageController.add(json);
          } else if (type == 'chat_updated') {
            _chatController.add(json);
          } else if (type == 'agent_thinking') {
            _chatController.add(json);
          } else if (type == 'workspace_members') {
            final members = json['members'] as List? ?? [];
            workspaceMembers = members.cast<Map<String, dynamic>>();
            notifyListeners();
          } else if (type == 'presence_list') {
            final users = json['users'] as List? ?? [];
            presenceUsers = users.cast<Map<String, dynamic>>();
            notifyListeners();
          } else if (type == 'presence_join') {
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
          } else if (type == 'presence_leave') {
            final uid = json['user_id'] as String?;
            if (uid != null) {
              presenceUsers =
                  presenceUsers.where((u) => u['user_id'] != uid).toList();
              notifyListeners();
            }
          } else if (type == 'terminal_windows') {
            debugPrint(
              '[WsClient] terminal_windows received: ${DateTime.now()}',
            );
            final windows = json['windows'] as List? ?? [];
            terminalWindows = windows.cast<Map<String, dynamic>>();
            notifyListeners();
          } else if (type == 'shared_terminals') {
            final terminals = json['terminals'] as List? ?? [];
            sharedTerminals = terminals.cast<Map<String, dynamic>>();
            notifyListeners();
          } else if (type == 'shared_terminal_deleted') {
            _sharedTerminalDeletedController.add(json);
            notifyListeners();
          } else if (type == 'event') {
            _customEventController.add(json);
          }
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
        notifyListeners();
        _scheduleReconnect();
      },
      onError: (e) {
        _errorController.add('WebSocket error: $e');
        _stopHeartbeat();
        _connected = false;
        _pendingWorkspaceId ??= _currentWorkspaceId;
        _currentWorkspaceId = null;
        notifyListeners();
        _scheduleReconnect();
      },
    );
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
    _cancelReconnect();
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
    final bid = getBrowserId();
    if (bid.isNotEmpty) msg['browser_id'] = bid;
    _send(msg);
  }

  void sendBrowserReattach() {
    final bid = getBrowserId();
    if (bid.isNotEmpty) {
      debugPrint('[WsClient] browser_reattach: $bid'); // coverage:ignore-start
      _send({
        'cmd': 'browser_reattach',
        'browser_id': bid
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

  void sendTerminalSelectWindow(int index) {
    _send({'cmd': 'terminal_select_window', 'index': index});
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
    if (!_autoReconnect || _reconnecting) return;

    // Record when the reconnect cycle started.
    _reconnectStartedAt ??= DateTime.now();

    // Give up after the timeout — stop auto-reconnect and let the UI
    // show "Disconnected" with a manual reconnect button.
    final timeout = testReconnectTimeout ?? _reconnectTimeout;
    if (DateTime.now().difference(_reconnectStartedAt!) >= timeout) {
      _autoReconnect = false;
      _reconnecting = false;
      _reconnectStartedAt = null;
      notifyListeners();
      return;
    }

    _reconnecting = true;
    _reconnectAttempt++;
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
    await connect();
    if (_connected && _pendingWorkspaceId != null) {
      connectWorkspace(_pendingWorkspaceId!);
    } else if (!_connected) {
      // connect() failed — schedule next attempt
      _reconnecting = false;
      _scheduleReconnect();
    }
  }

  void _cancelReconnect() {
    _autoReconnect = false;
    _reconnecting = false;
    _reconnectAttempt = 0;
    _reconnectStartedAt = null;
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
    _debugLogController.close();
    super.dispose();
  }
}
