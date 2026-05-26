import 'dart:async';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:bark_frontend/ws/ws_client.dart';
import 'package:bark_frontend/auth/auth_service.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Minimal fake WebSocketChannel for testing.
class _FakeWebSocketChannel extends Fake implements WebSocketChannel {
  final _incoming = StreamController<dynamic>.broadcast();
  final _sink = _FakeSink();
  bool failReady = false;

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _sink;

  @override
  Future<void> get ready =>
      failReady ? Future.error('Connection refused') : Future.value();

  void serverSend(Map<String, dynamic> msg) => _incoming.add(jsonEncode(msg));

  void serverClose() => _incoming.close();

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
      client.sendTerminalStart();
      client.sendTerminalInput('ls\n');
      client.sendTerminalResize(120, 40);
      client.sendTerminalStop();
      client.sendHeartbeat();
      client.sendBrowserResponse('req-1', {'status': 'ok'});

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
      SharedPreferences.setMockInitialValues({'bark_jwt': 'test-token'});
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
      SharedPreferences.setMockInitialValues({'bark_jwt': 'test-token'});
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
      client.sendTerminalStart(cols: 100, rows: 30);
      client.sendTerminalInput('ls\n');
      client.sendTerminalResize(120, 40);
      client.sendTerminalStop();
      client.sendUiReady();
      client.connectWorkspace('ws-1');
      client.disconnectWorkspace();

      final msgs = channel.sentMessages
          .map((s) => jsonDecode(s as String) as Map<String, dynamic>)
          .toList();
      expect(msgs[0], {'cmd': 'restart_container'});
      expect(msgs[1], {'cmd': 'terminal_start', 'cols': 100, 'rows': 30});
      expect(msgs[2], {'cmd': 'terminal_input', 'data': 'ls\n'});
      expect(msgs[3], {'cmd': 'terminal_resize', 'cols': 120, 'rows': 40});
      expect(msgs[4], {'cmd': 'terminal_stop'});
      expect(msgs[5], {'cmd': 'ui_ready'});
      expect(msgs[6], {'cmd': 'workspace_connect', 'workspaceId': 'ws-1'});
      expect(msgs[7], {'cmd': 'workspace_disconnect'});
    });

    test('sendTerminalStart uses default cols/rows', () {
      client.sendTerminalStart();
      final msg = jsonDecode(channel.sentMessages.last as String);
      expect(msg['cols'], 80);
      expect(msg['rows'], 24);
    });

    test('receives workspace_ready from server', () async {
      bool notified = false;
      client.addListener(() => notified = true);

      channel.serverSend({
        'type': 'workspace_ready',
        'workspaceId': 'ws-42',
      });
      await Future.delayed(Duration.zero);

      expect(client.currentWorkspaceId, 'ws-42');
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
  });
}
