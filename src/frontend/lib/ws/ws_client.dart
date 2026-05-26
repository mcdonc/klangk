import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../auth/auth_service.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';

/// Manages WebSocket connection to the Bark backend, sending commands
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

  Stream<String> get errors => _errorController.stream;
  Stream<String> get terminalOutput => _terminalOutputController.stream;
  Stream<Map<String, dynamic>> get browserRequests =>
      _browserRequestController.stream;

  /// Custom events from the backend (container_ready, container_stopped, etc.)
  Stream<Map<String, dynamic>> get customEvents =>
      _customEventController.stream;
  bool get connected => _connected;
  String? get currentWorkspaceId => _currentWorkspaceId;

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

          if (type == 'workspace_ready') {
            _currentWorkspaceId = json['workspaceId'] as String?;
            _startHeartbeat();
            notifyListeners();
          } else if (type == 'terminal_output') {
            _terminalOutputController.add(json['data'] as String? ?? '');
          } else if (type == 'error') {
            _errorController.add(json['message'] as String? ?? 'Unknown error');
          } else if (type == 'browser_request') {
            _browserRequestController.add(json);
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
    _channel!.sink.add(jsonEncode(msg));
  }

  void connectWorkspace(String workspaceId) {
    _send({'cmd': 'workspace_connect', 'workspaceId': workspaceId});
  }

  void disconnectWorkspace() {
    _stopHeartbeat();
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

  void sendBrowserResponse(String id, Map<String, dynamic> result) {
    _send({'cmd': 'browser_response', 'id': id, ...result});
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
    _customEventController.close();
    super.dispose();
  }
}
