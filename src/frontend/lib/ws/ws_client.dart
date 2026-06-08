import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../auth/auth_service.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// A single WebSocket debug log entry.
class WsDebugEntry {
  final DateTime timestamp;
  final String direction; // 'SEND' or 'RECV'
  final String summary;
  final Map<String, dynamic>? data;

  WsDebugEntry({
    required this.direction,
    required this.summary,
    this.data,
  }) : timestamp = DateTime.now();
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
  AuthService? _auth;
  String? _currentWorkspaceId;
  String? _defaultCommand;
  bool _connected = false;
  Timer? _heartbeatTimer;

  /// Override for testing to inject a fake channel factory.
  @visibleForTesting
  static WebSocketChannel Function(Uri uri)? testChannelFactory;

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
  final _debugLogController = StreamController<WsDebugEntry>.broadcast();

  Stream<String> get errors => _errorController.stream;
  Stream<String> get terminalOutput => _terminalOutputController.stream;
  Stream<Map<String, dynamic>> get browserRequests =>
      _browserRequestController.stream;

  /// Buffered chat history — populated from chat_history and chat_message.
  final List<Map<String, dynamic>> chatHistory = [];

  /// Workspace members for @mention autocomplete.
  List<Map<String, dynamic>> workspaceMembers = [];

  /// Chat messages (individual and history) from the backend.
  Stream<Map<String, dynamic>> get chatMessages => _chatController.stream;

  /// Custom events from the backend (container_ready, container_stopped, etc.)
  Stream<Map<String, dynamic>> get customEvents =>
      _customEventController.stream;

  /// Debug log of all WebSocket messages (sent and received).
  Stream<WsDebugEntry> get debugLog => _debugLogController.stream;
  bool get connected => _connected;
  String? get currentWorkspaceId => _currentWorkspaceId;
  String? get defaultCommand => _defaultCommand;

  void updateAuth(AuthService auth) {
    _auth = auth;
    if (!auth.isLoggedIn && _connected) {
      disconnect();
    }
  }

  Future<void> connect() async {
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
            _debugLogController.add(WsDebugEntry(
              direction: 'RECV',
              summary: summary,
              data: json,
            ));
          }

          if (type == 'workspace_ready') {
            _currentWorkspaceId = json['workspaceId'] as String?;
            _defaultCommand = json['defaultCommand'] as String?;
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
          } else if (type == 'chat_updated') {
            _chatController.add(json);
          } else if (type == 'workspace_members') {
            final members = json['members'] as List? ?? [];
            workspaceMembers = members.cast<Map<String, dynamic>>();
            notifyListeners();
          } else if (type == 'event') {
            _customEventController.add(json);
          }
        } catch (e) {
          _errorController.add('Parse error: $e');
        }
      },
      onDone: () {
        _connected = false;
        _currentWorkspaceId = null;
        _defaultCommand = null;
        notifyListeners();
      },
      onError: (e) {
        _errorController.add('WebSocket error: $e');
        _connected = false;
        notifyListeners();
      },
    );
  }

  void disconnect() {
    _stopHeartbeat();
    _channel?.sink.close();
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
      _debugLogController.add(WsDebugEntry(
        direction: 'SEND',
        summary: cmd,
        data: msg,
      ));
    }
    _channel!.sink.add(jsonEncode(msg));
  }

  void connectWorkspace(String workspaceId) {
    _send({'cmd': 'workspace_connect', 'workspaceId': workspaceId});
  }

  void disconnectWorkspace() {
    _stopHeartbeat();
    _send({'cmd': 'workspace_disconnect'});
    _currentWorkspaceId = null;
    chatHistory.clear();
    workspaceMembers = [];
    notifyListeners();
  }

  void sendUiReady() {
    _send({'cmd': 'ui_ready'});
  }

  void sendRestartContainer() {
    _send({'cmd': 'restart_container'});
  }

  void sendTerminalStart({int cols = 80, int rows = 24}) {
    _send({'cmd': 'terminal_start', 'cols': cols, 'rows': rows});
  }

  void sendTerminalInput(String data) {
    _send({'cmd': 'terminal_input', 'data': data});
  }

  void sendTerminalResize(int cols, int rows) {
    _send({'cmd': 'terminal_resize', 'cols': cols, 'rows': rows});
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

  void sendChatDelete(String messageId) {
    _send({'cmd': 'chat_delete', 'message_id': messageId});
  }

  void sendBrowserResponse(String id, Map<String, dynamic> result) {
    _send({'cmd': 'browser_response', 'id': id, ...result});
  }

  /// Send an incremental streamed chunk for a browser_request (streaming
  /// bridge). Followed by a final [sendBrowserResponse].
  void sendBrowserChunk(String id, String delta) {
    _send({'cmd': 'browser_chunk', 'id': id, 'delta': delta});
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
    disconnect();
    _errorController.close();
    _terminalOutputController.close();
    _browserRequestController.close();
    _chatController.close();
    _customEventController.close();
    _debugLogController.close();
    super.dispose();
  }
}
