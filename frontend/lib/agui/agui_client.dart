import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/web_socket_channel.dart';
import '../auth/auth_service.dart';
import 'agui_events.dart';

/// Manages WebSocket connection to the Bark backend, sending commands
/// and streaming AG-UI events.
class AguiClient extends ChangeNotifier {
  static const _wsBaseUrl = 'ws://localhost:8996/ws';

  WebSocketChannel? _channel;
  AuthService? _auth;
  String? _currentWorkspaceId;
  bool _connected = false;

  final _eventController = StreamController<AguiEvent>.broadcast();
  final _errorController = StreamController<String>.broadcast();

  Stream<AguiEvent> get events => _eventController.stream;
  Stream<String> get errors => _errorController.stream;
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

  @override
  void dispose() {
    disconnect();
    _eventController.close();
    _errorController.close();
    super.dispose();
  }
}
