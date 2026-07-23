import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/workspace_connector.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

class _MockWsClient extends WsClient {
  final StreamController<Map<String, dynamic>> _customEventsCtrl =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<String> _errorsCtrl =
      StreamController<String>.broadcast();
  final StreamController<Map<String, dynamic>> _sharedDeletedCtrl =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<String> _terminalOutput =
      StreamController<String>.broadcast();

  bool _connected = false;
  bool connectCalled = false;
  bool connectShouldSucceed = true;
  String? connectedWorkspaceId;

  @override
  bool get connected => _connected;

  @override
  Stream<Map<String, dynamic>> get customEvents => _customEventsCtrl.stream;

  @override
  Stream<String> get errors => _errorsCtrl.stream;

  @override
  Stream<Map<String, dynamic>> get sharedTerminalDeleted =>
      _sharedDeletedCtrl.stream;

  @override
  Stream<String> get terminalOutput => _terminalOutput.stream;

  @override
  String? get currentWorkspaceId => connectedWorkspaceId;

  @override
  Future<void> connect() async {
    connectCalled = true;
    _connected = connectShouldSucceed;
  }

  @override
  void connectWorkspace(String workspaceId) {
    connectedWorkspaceId = workspaceId;
  }

  void emitCustomEvent(Map<String, dynamic> event) =>
      _customEventsCtrl.add(event);

  void emitError(String error) => _errorsCtrl.add(error);

  void emitSharedDeleted(Map<String, dynamic> msg) =>
      _sharedDeletedCtrl.add(msg);

  void close() {
    _customEventsCtrl.close();
    _errorsCtrl.close();
    _sharedDeletedCtrl.close();
    _terminalOutput.close();
  }
}

void main() {
  group('WorkspaceConnector', () {
    test('connect calls wsClient.connect and connectWorkspace', () async {
      final ws = _MockWsClient();
      bool calledBack = false;

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-123',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {
          calledBack = true;
          expect(connected, isTrue);
          expect(error, isNull);
        },
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPermissionError: (_) {},
      );

      await connector.connect();

      expect(ws.connectCalled, isTrue);
      expect(ws.connectedWorkspaceId, 'ws-123');
      expect(calledBack, isTrue);
      expect(connector.isActive, isTrue);

      connector.dispose();
      ws.close();
    });

    test('connect reports failure when wsClient fails to connect', () async {
      final ws = _MockWsClient();
      ws.connectShouldSucceed = false;

      String? errorMsg;
      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-123',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {
          if (!connected) errorMsg = error;
        },
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPermissionError: (_) {},
      );

      await connector.connect();

      expect(errorMsg, 'Failed to connect to server');
      expect(connector.isActive, isFalse);

      connector.dispose();
      ws.close();
    });

    test('skips connect() when already connected', () async {
      final ws = _MockWsClient();
      ws._connected = true; // Already connected

      bool calledBack = false;
      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-456',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {
          calledBack = true;
          expect(connected, isTrue);
        },
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPermissionError: (_) {},
      );

      await connector.connect();

      // connect() was NOT called on the ws since it was already connected
      expect(ws.connectCalled, isFalse);
      expect(ws.connectedWorkspaceId, 'ws-456');
      expect(calledBack, isTrue);

      connector.dispose();
      ws.close();
    });

    test('forwards container events to callback', () async {
      final ws = _MockWsClient();
      final events = <String>[];

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (name, value) => events.add(name),
        onSharedTerminalDeleted: (_) {},
        onPermissionError: (_) {},
      );

      await connector.connect();

      ws.emitCustomEvent({
        'event': {
          'name': 'container_stopped',
          'value': {'reason': 'idle'}
        },
      });
      await Future<void>.delayed(Duration.zero);

      expect(events, contains('container_stopped'));

      connector.dispose();
      ws.close();
    });

    test('forwards shared terminal deletions to callback', () async {
      final ws = _MockWsClient();
      final deletions = <Map<String, dynamic>>[];

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (msg) => deletions.add(msg),
        onPermissionError: (_) {},
      );

      await connector.connect();

      ws.emitSharedDeleted({
        'user_id': 'u1',
        'window_id': 'w1',
        'window_name': 'bash',
      });
      await Future<void>.delayed(Duration.zero);

      expect(deletions, hasLength(1));
      expect(deletions[0]['user_id'], 'u1');

      connector.dispose();
      ws.close();
    });

    test('forwards permission errors to callback', () async {
      final ws = _MockWsClient();
      final errors = <String>[];

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPermissionError: (e) => errors.add(e),
      );

      await connector.connect();

      ws.emitError('Permission denied');
      await Future<void>.delayed(Duration.zero);

      expect(errors, ['Permission denied']);

      // Non-permission errors are ignored
      ws.emitError('Connection timeout');
      await Future<void>.delayed(Duration.zero);

      expect(errors, hasLength(1));

      connector.dispose();
      ws.close();
    });

    test('reconnect disposes old subscriptions and reconnects', () async {
      final ws = _MockWsClient();
      int connectedCount = 0;

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {
          if (connected) connectedCount++;
        },
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPermissionError: (_) {},
      );

      await connector.connect();
      expect(connectedCount, 1);
      expect(connector.isActive, isTrue);

      // Simulate disconnect
      ws._connected = false;
      ws.connectCalled = false;
      ws.connectedWorkspaceId = null;

      await connector.reconnect();
      expect(connectedCount, 2);
      expect(ws.connectCalled, isTrue);
      expect(ws.connectedWorkspaceId, 'ws-1');
      expect(connector.isActive, isTrue);

      connector.dispose();
      ws.close();
    });

    test('concurrent connect calls are deduplicated', () async {
      final ws = _MockWsClient();
      int connectedCount = 0;

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {
          if (connected) connectedCount++;
        },
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPermissionError: (_) {},
      );

      // Fire two connects concurrently
      final f1 = connector.connect();
      final f2 = connector.connect();
      await Future.wait([f1, f2]);

      // Only one should have executed
      expect(connectedCount, 1);

      connector.dispose();
      ws.close();
    });

    test('concurrent reconnect calls are deduplicated', () async {
      final ws = _MockWsClient();
      int connectedCount = 0;

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {
          if (connected) connectedCount++;
        },
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPermissionError: (_) {},
      );

      await connector.connect();
      expect(connectedCount, 1);

      ws._connected = false;
      ws.connectCalled = false;

      final f1 = connector.reconnect();
      final f2 = connector.reconnect();
      await Future.wait([f1, f2]);

      // Only one reconnect should have executed
      expect(connectedCount, 2);

      connector.dispose();
      ws.close();
    });

    test('dispose cancels subscriptions', () async {
      final ws = _MockWsClient();

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPermissionError: (_) {},
      );

      await connector.connect();
      expect(connector.isActive, isTrue);

      connector.dispose();
      expect(connector.isActive, isFalse);

      // Safe to dispose twice
      connector.dispose();

      ws.close();
    });
  });
}
