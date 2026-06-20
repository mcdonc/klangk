import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_frontend/auth/auth_service.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Minimal fake WebSocketChannel for testing.
class _FakeWebSocketChannel extends Fake implements WebSocketChannel {
  final _incoming = StreamController<dynamic>.broadcast();
  final _sink = _FakeSink();
  bool failReady = false;
  int? _closeCode;

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _sink;

  @override
  int? get closeCode => _closeCode;

  @override
  Future<void> get ready =>
      failReady ? Future.error('Connection refused') : Future.value();

  void serverSend(Map<String, dynamic> msg) => _incoming.add(jsonEncode(msg));

  void serverClose([int? code]) {
    _closeCode = code;
    _incoming.close();
  }

  void serverError(Object error) => _incoming.addError(error);

  List<dynamic> get sentMessages => _sink.sent;

  void dispose() => _incoming.close();
}

class _FakeSink extends Fake implements WebSocketSink {
  final List<dynamic> sent = [];

  @override
  void add(dynamic data) => sent.add(data);

  @override
  Future close([int? closeCode, String? closeReason]) async {}
}

void main() {
  setUp(() {
    testBaseUrlOverride = 'http://localhost:8997';
    SharedPreferences.setMockInitialValues({});
  });

  tearDown(() {
    testBaseUrlOverride = null;
  });

  group('WsClient initial state', () {
    test('not connected initially', () {
      final client = WsClient();
      expect(client.connected, isFalse);
      expect(client.currentWorkspaceId, isNull);
      client.dispose();
    });
  });

  group('WsClient.updateAuth', () {
    test('no-op when not connected', () {
      final client = WsClient();
      final auth = AuthService();

      client.updateAuth(auth);
      expect(client.connected, isFalse);
      client.dispose();
    });

    test('disconnects when connected and auth not logged in', () {
      final client = WsClient();
      final channel = _FakeWebSocketChannel();
      client.connectForTest(channel);
      expect(client.connected, isTrue);

      final auth = AuthService();
      // auth.isLoggedIn is false (no token)
      client.updateAuth(auth);
      expect(client.connected, isFalse);
      client.dispose();
    });
  });

  group('WsClient.disconnect', () {
    test('disconnect resets state', () {
      final client = WsClient();
      client.disconnect();
      expect(client.connected, isFalse);
      expect(client.currentWorkspaceId, isNull);
      client.dispose();
    });

    test('disconnect notifies listeners', () {
      final client = WsClient();
      bool notified = false;
      client.addListener(() => notified = true);

      client.disconnect();

      expect(notified, isTrue);
      client.dispose();
    });
  });

  group('WsClient send methods (no channel)', () {
    test('send methods do not throw without connection', () {
      final client = WsClient();

      // All send methods should silently no-op without a channel
      client.connectWorkspace('ws-1');
      client.disconnectWorkspace();
      client.sendUiReady();
      client.sendRestartContainer();
      client.sendShutdownContainer();
      client.sendTerminalStart();
      client.sendBrowserReattach();
      client.sendTerminalInput('ls\n');
      client.sendTerminalResize(120, 40);
      client.sendTerminalNewWindow();
      client.sendTerminalNewWindow(name: 'build');
      client.sendTerminalSelectWindow(1);
      client.sendTerminalCloseWindow(1);
      client.sendTerminalRenameWindow(0, 'test');
      client.sendTerminalListWindows();
      client.sendShareWindow('@0');
      client.sendUnshareWindow('@0');
      client.sendJoinSharedTerminal('uid', '@0');
      client.sendDeleteSharedTerminal('uid', '@0');
      client.sendListSharedTerminals();
      client.sendTerminalStop();
      client.sendHeartbeat();
      client.sendBrowserResponse('req-1', {'status': 'ok'});
      client.sendBrowserChunk('req-1', 'delta');

      expect(client.connected, isFalse);
      client.dispose();
    });

    test('disconnectWorkspace clears workspace id', () {
      final client = WsClient();
      bool notified = false;
      client.addListener(() => notified = true);

      client.disconnectWorkspace();

      expect(client.currentWorkspaceId, isNull);
      expect(notified, isTrue);
      client.dispose();
    });
  });

  group('WsClient.connect', () {
    setUp(() {
      WsClient.testChannelFactory = null;
    });

    tearDown(() {
      WsClient.testChannelFactory = null;
    });

    test('connect without auth returns early', () async {
      final client = WsClient();
      await client.connect();
      expect(client.connected, isFalse);
      client.dispose();
    });

    test('connect when already connected returns early', () async {
      final client = WsClient();
      final channel = _FakeWebSocketChannel();
      client.connectForTest(channel);
      expect(client.connected, isTrue);

      // Second connect should be a no-op
      await client.connect();
      expect(client.connected, isTrue);
      client.disconnect();
      client.dispose();
    });

    test('connect success via testChannelFactory', () async {
      SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
      final channel = _FakeWebSocketChannel();
      WsClient.testChannelFactory = (_) => channel;

      final auth = AuthService();
      await Future.delayed(Duration.zero);
      expect(auth.isLoggedIn, isTrue);

      final client = WsClient();
      client.updateAuth(auth);

      await client.connect();
      expect(client.connected, isTrue);
      client.disconnect();
      client.dispose();
    });

    test('connect failure emits error', () async {
      SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
      final failChannel = _FakeWebSocketChannel();
      failChannel.failReady = true;
      WsClient.testChannelFactory = (_) => failChannel;

      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);

      final errors = <String>[];
      client.errors.listen(errors.add);

      await client.connect();
      await Future.delayed(Duration.zero);
      expect(client.connected, isFalse);
      expect(errors.length, 1);
      expect(errors[0], startsWith('Connection failed:'));
      client.dispose();
    });

    test('connect failure with auth close code triggers logout', () async {
      SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
      final failChannel = _FakeWebSocketChannel();
      failChannel.failReady = true;
      failChannel._closeCode = 4001;
      WsClient.testChannelFactory = (_) => failChannel;

      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);

      final errors = <String>[];
      client.errors.listen(errors.add);

      await client.connect();
      await Future.delayed(Duration.zero);
      expect(client.connected, isFalse);
      expect(errors.length, 1);
      expect(errors[0], 'Session expired, please log in again');
      client.dispose();
    });

    test('unclean close sets flag and pre-checks HTTP on reconnect', () async {
      SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
      final channel = _FakeWebSocketChannel();
      WsClient.testChannelFactory = (_) => channel;
      WsClient.testBackoffOverride = (_) => Duration.zero;
      var httpCheckCalled = false;
      WsClient.testHttpPreCheck = () async {
        httpCheckCalled = true;
        return true;
      };

      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);

      await client.connect();
      expect(client.connected, isTrue);

      // Connect to workspace so auto-reconnect is armed
      client.connectWorkspace('ws1');
      channel.serverSend({'type': 'workspace_ready', 'workspaceId': 'ws1'});
      await Future.delayed(Duration.zero);

      // Simulate unclean server close (no code 1000)
      channel.serverClose(1006);
      await Future.delayed(Duration.zero);
      expect(client.connected, isFalse);

      // Wait for reconnect attempt
      await Future.delayed(const Duration(milliseconds: 50));
      expect(httpCheckCalled, isTrue);

      WsClient.testBackoffOverride = null;
      WsClient.testHttpPreCheck = null;
      client.dispose();
    });

    test('clean close does not trigger HTTP pre-check', () async {
      SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
      final channel = _FakeWebSocketChannel();
      WsClient.testChannelFactory = (_) => channel;
      WsClient.testBackoffOverride = (_) => Duration.zero;
      var httpCheckCalled = false;
      WsClient.testHttpPreCheck = () async {
        httpCheckCalled = true;
        return true;
      };

      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);

      await client.connect();
      expect(client.connected, isTrue);

      // Connect to workspace so auto-reconnect is armed
      client.connectWorkspace('ws1');
      channel.serverSend({'type': 'workspace_ready', 'workspaceId': 'ws1'});
      await Future.delayed(Duration.zero);

      // Simulate clean server close (code 1000)
      channel.serverClose(1000);
      await Future.delayed(Duration.zero);

      // Wait for reconnect attempt
      await Future.delayed(const Duration(milliseconds: 50));
      expect(httpCheckCalled, isFalse);

      WsClient.testBackoffOverride = null;
      WsClient.testHttpPreCheck = null;
      client.dispose();
    });

    test('HTTP pre-check failure schedules another reconnect', () async {
      SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
      final channel = _FakeWebSocketChannel();
      WsClient.testChannelFactory = (_) => channel;
      WsClient.testBackoffOverride = (_) => Duration.zero;
      var httpCheckCount = 0;
      WsClient.testHttpPreCheck = () async {
        httpCheckCount++;
        return false; // server not reachable
      };

      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);

      await client.connect();
      expect(client.connected, isTrue);

      // Connect to workspace so auto-reconnect is armed
      client.connectWorkspace('ws1');
      channel.serverSend({'type': 'workspace_ready', 'workspaceId': 'ws1'});
      await Future.delayed(Duration.zero);

      // Simulate unclean close
      channel.serverClose(1006);
      await Future.delayed(Duration.zero);

      // First reconnect attempt: HTTP check fails, schedules another
      await Future.delayed(const Duration(milliseconds: 50));
      expect(httpCheckCount, greaterThanOrEqualTo(1));
      expect(client.reconnecting, isTrue);

      WsClient.testBackoffOverride = null;
      WsClient.testHttpPreCheck = null;
      client.dispose();
    });
  });

  group('WsClient.dispose', () {
    test('dispose cleans up streams', () {
      final client = WsClient();
      client.dispose();
      // After dispose, adding listeners should fail or streams should be closed
      expect(client.connected, isFalse);
    });
  });

  group('WsClient streams', () {
    test('errors stream is broadcast', () {
      final client = WsClient();
      expect(client.errors.isBroadcast, isTrue);
      client.dispose();
    });

    test('terminalOutput stream is broadcast', () {
      final client = WsClient();
      expect(client.terminalOutput.isBroadcast, isTrue);
      client.dispose();
    });

    test('browserRequests stream is broadcast', () {
      final client = WsClient();
      expect(client.browserRequests.isBroadcast, isTrue);
      client.dispose();
    });

    test('debugLog stream is broadcast', () {
      final client = WsClient();
      expect(client.debugLog.isBroadcast, isTrue);
      client.dispose();
    });

    test('customEvents stream is broadcast', () {
      final client = WsClient();
      expect(client.customEvents.isBroadcast, isTrue);
      client.dispose();
    });
  });

  group('WsClient auto-reconnect', () {
    late List<_FakeWebSocketChannel> channels;

    setUp(() {
      SharedPreferences.setMockInitialValues({'klangk_jwt': 'test-token'});
      channels = [];
      WsClient.testChannelFactory = (_) {
        final ch = _FakeWebSocketChannel();
        channels.add(ch);
        return ch;
      };
      WsClient.testBackoffOverride = (_) => Duration.zero;
    });

    tearDown(() {
      WsClient.testChannelFactory = null;
      WsClient.testBackoffOverride = null;
    });

    test('server close triggers auto-reconnect when workspace was connected',
        () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      expect(client.connected, isTrue);

      // Simulate workspace connection
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);
      expect(client.currentWorkspaceId, 'ws-1');

      // Server closes connection
      channels[0].serverClose();
      await Future.delayed(Duration.zero);
      expect(client.connected, isFalse);
      expect(client.reconnecting, isTrue);
      expect(client.reconnectAttempt, 1);

      // Let the reconnect timer fire (Duration.zero)
      await Future.delayed(Duration.zero);
      await Future.delayed(Duration.zero);
      expect(channels.length, 2);
      expect(client.connected, isTrue);

      // workspace_connect should have been re-sent
      final msgs = channels[1]
          .sentMessages
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      expect(msgs.any((m) => m['cmd'] == 'workspace_connect'), isTrue);

      client.disconnect();
      client.dispose();
    });

    test('server error triggers auto-reconnect', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      // Consume error stream to prevent unhandled errors
      final errors = <String>[];
      client.errors.listen(errors.add);

      channels[0].serverError(Exception('network failure'));
      await Future.delayed(Duration.zero);

      expect(client.reconnecting, isTrue);
      expect(client.reconnectAttempt, 1);

      client.disconnect();
      client.dispose();
    });

    test('server error with auth close code triggers logout', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      final errors = <String>[];
      client.errors.listen(errors.add);

      // Set close code before emitting error
      channels[0]._closeCode = 4002;
      channels[0].serverError(Exception('auth failure'));
      await Future.delayed(Duration.zero);

      expect(client.reconnecting, isFalse);
      expect(client.reconnectAttempt, 0);

      client.disconnect();
      client.dispose();
    });

    test('successful reconnect clears reconnect state', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      // Disconnect
      channels[0].serverClose();
      await Future.delayed(Duration.zero);
      expect(client.reconnecting, isTrue);

      // Reconnect fires
      await Future.delayed(Duration.zero);
      await Future.delayed(Duration.zero);
      expect(channels.length, 2);

      // Backend responds with workspace_ready
      channels[1]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      expect(client.reconnecting, isFalse);
      expect(client.reconnectAttempt, 0);
      expect(client.connected, isTrue);
      expect(client.currentWorkspaceId, 'ws-1');

      client.disconnect();
      client.dispose();
    });

    test('intentional disconnect does NOT trigger auto-reconnect', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      client.disconnect();
      await Future.delayed(Duration.zero);

      expect(client.reconnecting, isFalse);
      expect(client.reconnectAttempt, 0);
      expect(channels.length, 1); // no second channel created

      client.dispose();
    });

    test('disconnectWorkspace does NOT trigger auto-reconnect', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      client.disconnectWorkspace();
      await Future.delayed(Duration.zero);

      expect(client.reconnecting, isFalse);
      expect(client.reconnectAttempt, 0);

      client.disconnect();
      client.dispose();
    });

    test('reconnect attempt increments on repeated failures', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      // First disconnect
      channels[0].serverClose();
      await Future.delayed(Duration.zero);
      expect(client.reconnectAttempt, 1);

      // Reconnect fires, but new channel fails
      channels.add(_FakeWebSocketChannel()..failReady = true);
      // Override factory to return the failing channel
      var callCount = 0;
      WsClient.testChannelFactory = (_) {
        callCount++;
        if (channels.length > callCount) return channels[callCount];
        final ch = _FakeWebSocketChannel()..failReady = true;
        channels.add(ch);
        return ch;
      };

      // Let first reconnect attempt fire and fail
      await Future.delayed(Duration.zero);
      await Future.delayed(Duration.zero);
      await Future.delayed(Duration.zero);

      expect(client.reconnectAttempt, greaterThan(1));

      client.disconnect();
      client.dispose();
    });

    test('no auto-reconnect before workspace connected', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      // Don't call connectWorkspace — _autoReconnect stays false

      channels[0].serverClose();
      await Future.delayed(Duration.zero);

      expect(client.reconnecting, isFalse);
      expect(channels.length, 1);

      client.dispose();
    });

    test('manual connect cancels pending reconnect timer', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      // Use a long backoff so the timer doesn't fire before our manual connect
      WsClient.testBackoffOverride = (_) => const Duration(seconds: 60);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      // Server closes
      channels[0].serverClose();
      await Future.delayed(Duration.zero);
      expect(client.reconnecting, isTrue);

      // Manual connect before timer fires
      await client.connect();
      expect(client.connected, isTrue);
      expect(channels.length, 2);

      client.disconnect();
      client.dispose();
    });

    test('dispose cancels reconnect timer', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      WsClient.testBackoffOverride = (_) => const Duration(seconds: 60);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      channels[0].serverClose();
      await Future.delayed(Duration.zero);
      expect(client.reconnecting, isTrue);

      // Dispose should cancel reconnect and not throw
      client.dispose();
      expect(client.reconnecting, isFalse);
      expect(channels.length, 1); // no reconnect attempt was made
    });

    test('gives up reconnecting after timeout', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      // Zero backoff so reconnects fire immediately; zero timeout so
      // the second attempt exceeds the deadline.
      WsClient.testBackoffOverride = (_) => Duration.zero;
      WsClient.testReconnectTimeout = Duration.zero;

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      // Make all future channels fail.
      WsClient.testChannelFactory = (_) {
        final ch = _FakeWebSocketChannel()..failReady = true;
        channels.add(ch);
        return ch;
      };

      // Disconnect → first reconnect attempt fires.
      channels[0].serverClose();
      await Future.delayed(Duration.zero);

      // Let the first attempt fire and fail, then the second schedule
      // check triggers the timeout.
      await Future.delayed(Duration.zero);
      await Future.delayed(Duration.zero);
      await Future.delayed(Duration.zero);

      // Auto-reconnect should have given up.
      expect(client.reconnecting, isFalse);

      WsClient.testReconnectTimeout = null;
      client.disconnect();
      client.dispose();
    });

    test('backoff delay override is called with correct attempt', () async {
      final attempts = <int>[];
      WsClient.testBackoffOverride = (attempt) {
        attempts.add(attempt);
        return Duration.zero;
      };

      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      channels[0].serverClose();
      await Future.delayed(Duration.zero);

      expect(attempts, [1]);

      client.disconnect();
      client.dispose();
    });

    test('auth close code 4001 triggers logout instead of reconnect', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      final errors = <String>[];
      client.errors.listen(errors.add);

      // Server closes with auth failure code
      channels[0].serverClose(4001);
      await Future.delayed(Duration.zero);

      expect(client.reconnecting, isFalse);
      expect(client.reconnectAttempt, 0);
      expect(errors, contains('Session expired, please log in again'));

      client.disconnect();
      client.dispose();
    });

    test('auth close code 4002 triggers logout instead of reconnect', () async {
      final auth = AuthService();
      await Future.delayed(Duration.zero);

      final client = WsClient();
      client.updateAuth(auth);
      await client.connect();
      client.connectWorkspace('ws-1');
      channels[0]
          .serverSend({'type': 'workspace_ready', 'workspaceId': 'ws-1'});
      await Future.delayed(Duration.zero);

      final errors = <String>[];
      client.errors.listen(errors.add);

      // Server closes with token expired code
      channels[0].serverClose(4002);
      await Future.delayed(Duration.zero);

      expect(client.reconnecting, isFalse);
      expect(client.reconnectAttempt, 0);
      expect(errors, contains('Session expired, please log in again'));

      client.disconnect();
      client.dispose();
    });
  });

  group('WsClient with fake channel', () {
    late WsClient client;
    late _FakeWebSocketChannel channel;

    setUp(() {
      client = WsClient();
      channel = _FakeWebSocketChannel();
      client.connectForTest(channel);
    });

    tearDown(() {
      // Disconnect first to remove the stream listener, then dispose.
      // This prevents onDone from firing after the client is disposed.
      client.disconnect();
      client.dispose();
    });

    test('connectForTest sets connected', () {
      expect(client.connected, isTrue);
    });

    test('send methods produce correct JSON', () {
      client.sendRestartContainer();
      client.sendShutdownContainer();
      client.sendTerminalStart(cols: 100, rows: 30);
      client.sendBrowserReattach();
      client.sendTerminalInput('ls\n');
      client.sendTerminalResize(120, 40);
      client.sendTerminalNewWindow(name: 'build');
      client.sendTerminalSelectWindow(2);
      client.sendTerminalCloseWindow(1);
      client.sendTerminalRenameWindow(0, 'main');
      client.sendTerminalListWindows();
      client.sendShareWindow('@0');
      client.sendUnshareWindow('@0');
      client.sendJoinSharedTerminal('uid', '@0');
      client.sendDeleteSharedTerminal('uid', '@0');
      client.sendListSharedTerminals();
      client.sendTerminalStop();
      client.sendUiReady();
      client.connectWorkspace('ws-1');
      client.disconnectWorkspace();

      final msgs = channel.sentMessages
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      expect(msgs[0], {'cmd': 'restart_container'});
      expect(msgs[1], {'cmd': 'shutdown_container'});
      expect(msgs[2], {'cmd': 'terminal_start', 'cols': 100, 'rows': 30});
      expect(msgs[3], {'cmd': 'terminal_input', 'data': 'ls\n'});
      expect(msgs[4], {'cmd': 'terminal_resize', 'cols': 120, 'rows': 40});
      expect(msgs[5], {'cmd': 'terminal_new_window', 'name': 'build'});
      expect(msgs[6], {'cmd': 'terminal_select_window', 'index': 2});
      expect(msgs[7], {'cmd': 'terminal_close_window', 'index': 1});
      expect(msgs[8],
          {'cmd': 'terminal_rename_window', 'index': 0, 'name': 'main'});
      expect(msgs[9], {'cmd': 'terminal_list_windows'});
      expect(msgs[10], {'cmd': 'share_window', 'window_id': '@0'});
      expect(msgs[11], {'cmd': 'unshare_window', 'window_id': '@0'});
      expect(msgs[12], {
        'cmd': 'join_shared_terminal',
        'user_id': 'uid',
        'window_id': '@0',
      });
      expect(msgs[13], {
        'cmd': 'delete_shared_terminal',
        'user_id': 'uid',
        'window_id': '@0',
      });
      expect(msgs[14], {'cmd': 'list_shared_terminals'});
      expect(msgs[15], {'cmd': 'terminal_stop'});
      expect(msgs[16], {'cmd': 'ui_ready'});
      expect(msgs[17], {'cmd': 'workspace_connect', 'workspaceId': 'ws-1'});
      expect(msgs[18], {'cmd': 'workspace_disconnect'});
    });

    test('sendTerminalNewWindow without name omits name field', () {
      client.sendTerminalNewWindow();
      final msg = jsonDecode(channel.sentMessages.last as String);
      expect(msg['cmd'], 'terminal_new_window');
      expect(msg.containsKey('name'), isFalse);
    });

    test('terminal_windows message updates terminalWindows', () async {
      channel.serverSend({
        'type': 'terminal_windows',
        'windows': [
          {'index': 0, 'name': 'bash', 'active': true},
          {'index': 1, 'name': 'build', 'active': false},
        ],
      });
      await Future.delayed(Duration.zero);
      expect(client.terminalWindows.length, 2);
      expect(client.terminalWindows[0]['name'], 'bash');
      expect(client.terminalWindows[1]['name'], 'build');
    });

    test('shared_terminals message updates sharedTerminals', () async {
      channel.serverSend({
        'type': 'shared_terminals',
        'terminals': [
          {
            'name': 'dev',
            'sessions': ['alice']
          },
        ],
      });
      await Future.delayed(Duration.zero);
      expect(client.sharedTerminals.length, 1);
      expect(client.sharedTerminals[0]['name'], 'dev');
    });

    test('shared_terminal_deleted fires stream', () async {
      final deleted = <Map<String, dynamic>>[];
      client.sharedTerminalDeleted.listen(deleted.add);
      channel.serverSend({
        'type': 'shared_terminal_deleted',
        'user_id': 'uid',
        'window_name': 'dev',
        'window_id': '@0',
      });
      await Future.delayed(Duration.zero);
      expect(deleted.length, 1);
      expect(deleted[0]['window_name'], 'dev');
    });

    test('sendTerminalStart omits cols/rows when not provided', () {
      client.sendTerminalStart();
      final msg = jsonDecode(channel.sentMessages.last as String);
      expect(msg.containsKey('cols'), isFalse);
      expect(msg.containsKey('rows'), isFalse);
    });

    test('receives workspace_ready from server', () async {
      bool notified = false;
      client.addListener(() => notified = true);

      channel.serverSend({
        'type': 'workspace_ready',
        'workspaceId': 'ws-42',
        'userId': 'user-42',
        'defaultCommand': 'pi',
      });
      await Future.delayed(Duration.zero);

      expect(client.currentWorkspaceId, 'ws-42');
      expect(client.currentUserId, 'user-42');
      expect(client.defaultCommand, 'pi');
      expect(notified, isTrue);
    });

    test('workspace_ready starts heartbeat timer', () async {
      channel.serverSend({
        'type': 'workspace_ready',
        'workspaceId': 'ws-hb',
      });
      await Future.delayed(Duration.zero);

      // sendHeartbeat should work without error
      client.sendHeartbeat();
      final msg = jsonDecode(channel.sentMessages.last as String);
      expect(msg, {'cmd': 'heartbeat'});
    });

    test('disconnect stops heartbeat timer', () async {
      channel.serverSend({
        'type': 'workspace_ready',
        'workspaceId': 'ws-hb2',
      });
      await Future.delayed(Duration.zero);

      final msgCountBefore = channel.sentMessages.length;
      client.disconnect();

      // No more heartbeats should be sent after disconnect
      await Future.delayed(Duration.zero);
      // Can't easily test timer cancellation directly, but disconnect
      // should not throw and sentMessages should not grow
      expect(channel.sentMessages.length, msgCountBefore);
    });

    test('disconnectWorkspace stops heartbeat timer', () async {
      channel.serverSend({
        'type': 'workspace_ready',
        'workspaceId': 'ws-hb3',
      });
      await Future.delayed(Duration.zero);

      client.disconnectWorkspace();
      final msgs = channel.sentMessages
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      // Last message should be workspace_disconnect, not heartbeat
      expect(msgs.last['cmd'], 'workspace_disconnect');
    });

    test('receives terminal_output from server', () async {
      final outputs = <String>[];
      client.terminalOutput.listen(outputs.add);

      channel.serverSend({'type': 'terminal_output', 'data': 'hello'});
      await Future.delayed(Duration.zero);

      expect(outputs, ['hello']);
    });

    test('terminal_output with null data sends empty string', () async {
      final outputs = <String>[];
      client.terminalOutput.listen(outputs.add);

      channel.serverSend({'type': 'terminal_output'});
      await Future.delayed(Duration.zero);

      expect(outputs, ['']);
    });

    test('receives error from server', () async {
      final errors = <String>[];
      client.errors.listen(errors.add);

      channel.serverSend({'type': 'error', 'message': 'bad thing'});
      await Future.delayed(Duration.zero);

      expect(errors, ['bad thing']);
    });

    test('error with null message sends Unknown error', () async {
      final errors = <String>[];
      client.errors.listen(errors.add);

      channel.serverSend({'type': 'error'});
      await Future.delayed(Duration.zero);

      expect(errors, ['Unknown error']);
    });

    test('invalid JSON produces parse error', () async {
      final errors = <String>[];
      client.errors.listen(errors.add);

      channel._incoming.add('not json');
      await Future.delayed(Duration.zero);

      expect(errors.length, 1);
      expect(errors[0], startsWith('Parse error:'));
    });

    test('server close resets connected state', () async {
      channel.serverClose();
      await Future.delayed(Duration.zero);

      expect(client.connected, isFalse);
      expect(client.currentWorkspaceId, isNull);
    });

    test('server error emits to error stream', () async {
      final errors = <String>[];
      client.errors.listen(errors.add);

      channel.serverError(Exception('boom'));
      await Future.delayed(Duration.zero);

      expect(errors.length, 1);
      expect(errors[0], contains('WebSocket error'));
      expect(client.connected, isFalse);
    });

    test('chat_message routed to chatMessages stream', () async {
      final messages = <Map<String, dynamic>>[];
      client.chatMessages.listen(messages.add);

      channel.serverSend({
        'type': 'chat_message',
        'id': 'msg-1',
        'user_email': 'alice@test.com',
        'message': 'hello',
        'created_at': '2026-01-01 00:00:00',
      });
      await Future.delayed(Duration.zero);

      expect(messages.length, 1);
      expect(messages[0]['message'], 'hello');
      expect(messages[0]['user_email'], 'alice@test.com');
    });

    test('chat_history messages routed individually', () async {
      final messages = <Map<String, dynamic>>[];
      client.chatMessages.listen(messages.add);

      channel.serverSend({
        'type': 'chat_history',
        'messages': [
          {
            'id': 'msg-1',
            'user_email': 'a@test.com',
            'message': 'first',
            'created_at': '2026-01-01 00:00:00',
          },
          {
            'id': 'msg-2',
            'user_email': 'b@test.com',
            'message': 'second',
            'created_at': '2026-01-01 00:01:00',
          },
        ],
      });
      await Future.delayed(Duration.zero);

      expect(messages.length, 2);
      expect(messages[0]['message'], 'first');
      expect(messages[1]['message'], 'second');
    });

    test('chat_updated routed to chatMessages stream', () async {
      final messages = <Map<String, dynamic>>[];
      client.chatMessages.listen(messages.add);

      channel.serverSend({
        'type': 'chat_updated',
        'message_id': 'msg-1',
        'message': '<message deleted by author>',
      });
      await Future.delayed(Duration.zero);

      expect(messages.length, 1);
      expect(messages[0]['type'], 'chat_updated');
      expect(messages[0]['message_id'], 'msg-1');
    });

    test('sendChatMessage sends correct command', () {
      client.sendChatMessage('hello world');
      final msg = jsonDecode(channel.sentMessages.last as String);
      expect(msg, {'cmd': 'chat_send', 'message': 'hello world'});
    });

    test('sendChatDelete sends correct command', () {
      client.sendChatDelete('msg-42');
      final msg = jsonDecode(channel.sentMessages.last as String);
      expect(msg, {'cmd': 'chat_delete', 'message_id': 'msg-42'});
    });

    test('sendChatAgentAbort sends correct command', () {
      client.sendChatAgentAbort();
      final msg = jsonDecode(channel.sentMessages.last as String);
      expect(msg, {'cmd': 'chat_agent_abort'});
    });

    test('chatMessages stream is broadcast', () {
      expect(client.chatMessages.isBroadcast, isTrue);
    });

    test('workspace_members populates workspaceMembers', () async {
      channel.serverSend({
        'type': 'workspace_members',
        'members': [
          {'id': 'u1', 'email': 'alice@test.com'},
          {'id': 'u2', 'email': 'bob@test.com'},
        ],
      });
      await Future.delayed(Duration.zero);

      expect(client.workspaceMembers.length, 2);
      expect(client.workspaceMembers[0]['email'], 'alice@test.com');
      expect(client.workspaceMembers[1]['email'], 'bob@test.com');
    });

    test('workspace_members notifies listeners', () async {
      bool notified = false;
      client.addListener(() => notified = true);

      channel.serverSend({
        'type': 'workspace_members',
        'members': [
          {'id': 'u1', 'email': 'alice@test.com'},
        ],
      });
      await Future.delayed(Duration.zero);

      expect(notified, isTrue);
    });

    test('disconnectWorkspace clears workspaceMembers', () async {
      channel.serverSend({
        'type': 'workspace_members',
        'members': [
          {'id': 'u1', 'email': 'alice@test.com'},
        ],
      });
      await Future.delayed(Duration.zero);
      expect(client.workspaceMembers.length, 1);

      client.disconnectWorkspace();
      expect(client.workspaceMembers, isEmpty);
    });

    test('presence_list populates presenceUsers', () async {
      channel.serverSend({
        'type': 'presence_list',
        'users': [
          {'user_id': 'u1', 'user_email': 'alice@test.com'},
          {'user_id': 'u2', 'user_email': 'bob@test.com'},
        ],
      });
      await Future.delayed(Duration.zero);

      expect(client.presenceUsers.length, 2);
      expect(client.presenceUsers[0]['user_email'], 'alice@test.com');
    });

    test('presence_join adds user', () async {
      channel.serverSend({
        'type': 'presence_list',
        'users': [
          {'user_id': 'u1', 'user_email': 'alice@test.com'},
        ],
      });
      await Future.delayed(Duration.zero);

      channel.serverSend({
        'type': 'presence_join',
        'user_id': 'u2',
        'user_email': 'bob@test.com',
      });
      await Future.delayed(Duration.zero);

      expect(client.presenceUsers.length, 2);
      expect(client.presenceUsers.any((u) => u['user_id'] == 'u2'), isTrue);
    });

    test('presence_join ignores duplicate', () async {
      channel.serverSend({
        'type': 'presence_list',
        'users': [
          {'user_id': 'u1', 'user_email': 'alice@test.com'},
        ],
      });
      await Future.delayed(Duration.zero);

      channel.serverSend({
        'type': 'presence_join',
        'user_id': 'u1',
        'user_email': 'alice@test.com',
      });
      await Future.delayed(Duration.zero);

      expect(client.presenceUsers.length, 1);
    });

    test('presence_leave removes user', () async {
      channel.serverSend({
        'type': 'presence_list',
        'users': [
          {'user_id': 'u1', 'user_email': 'alice@test.com'},
          {'user_id': 'u2', 'user_email': 'bob@test.com'},
        ],
      });
      await Future.delayed(Duration.zero);

      channel.serverSend({
        'type': 'presence_leave',
        'user_id': 'u1',
      });
      await Future.delayed(Duration.zero);

      expect(client.presenceUsers.length, 1);
      expect(client.presenceUsers[0]['user_id'], 'u2');
    });

    test('disconnectWorkspace clears presenceUsers', () async {
      channel.serverSend({
        'type': 'presence_list',
        'users': [
          {'user_id': 'u1', 'user_email': 'alice@test.com'},
        ],
      });
      await Future.delayed(Duration.zero);
      expect(client.presenceUsers.length, 1);

      client.disconnectWorkspace();
      expect(client.presenceUsers, isEmpty);
    });
  });
}
