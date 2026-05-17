import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../auth/auth_service.dart';
import 'agui_events.dart';
import '../utils/backend_url.dart';

/// Manages WebSocket connection to the Bark backend, sending commands
/// and streaming AG-UI events.
class AguiClient extends ChangeNotifier {
  static String get _wsBaseUrl {
    final loc = Uri.base;
    final wsScheme = loc.scheme == 'https' ? 'wss' : 'ws';
    return '$wsScheme://${loc.host}:${loc.port}$baseUrl/ws';
  }

  WebSocketChannel? _channel;
  AuthService? _auth;
  String? _currentWorkspaceId;
  bool _connected = false;

  final _eventController = StreamController<AguiEvent>.broadcast();
  final _errorController = StreamController<String>.broadcast();
  final _terminalOutputController = StreamController<String>.broadcast();

  Stream<AguiEvent> get events => _eventController.stream;
  Stream<String> get errors => _errorController.stream;
  Stream<String> get terminalOutput => _terminalOutputController.stream;
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

    final uri = Uri.parse('$_wsBaseUrl?token=${_auth!.token}');
    _channel = WebSocketChannel.connect(uri);

    try {
      await _channel!.ready;
    } catch (e) {
      _errorController.add('Connection failed: $e');
      return;
    }

    _connected = true;
    notifyListeners();

    _channel!.stream.listen(
      (data) {
        try {
          final json = jsonDecode(data as String) as Map<String, dynamic>;
          final type = json['type'] as String?;

          if (type == 'event') {
            _eventController.add(AguiEvent.fromJson(json));
          } else if (type == 'workspace_ready') {
            _currentWorkspaceId = json['workspaceId'] as String?;
            notifyListeners();
          } else if (type == 'terminal_output') {
            _terminalOutputController.add(json['data'] as String? ?? '');
          } else if (type == 'error') {
            _errorController.add(json['message'] as String? ?? 'Unknown error');
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
    _send({'cmd': 'workspace_disconnect'});
    _currentWorkspaceId = null;
    notifyListeners();
  }

  void sendUiReady() {
    _send({'cmd': 'ui_ready'});
  }

  void sendExtensionUiResponse(String id,
      {String? value, bool? cancelled, bool? confirmed}) {
    final msg = <String, dynamic>{'cmd': 'extension_ui_response', 'id': id};
    if (value != null) msg['value'] = value;
    if (cancelled == true) msg['cancelled'] = true;
    if (confirmed != null) msg['confirmed'] = confirmed;
    _send(msg);
  }

  void sendPrompt(String text) {
    _send({'cmd': 'prompt', 'text': text});
  }

  void sendSteer(String text) {
    _send({'cmd': 'steer', 'text': text});
  }

  void sendFollowUp(String text) {
    _send({'cmd': 'follow_up', 'text': text});
  }

  void sendAbort() {
    _send({'cmd': 'abort'});
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

  @override
  void dispose() {
    disconnect();
    _eventController.close();
    _errorController.close();
    _terminalOutputController.close();
    super.dispose();
  }
}
