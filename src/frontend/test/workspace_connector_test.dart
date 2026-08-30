import 'dart:async';

import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/workspace_connector.dart';
import 'package:klangk_frontend/ws/ws_client.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

class _MockWsClient extends WsClient {
  final StreamController<Map<String, dynamic>> _customEventsCtrl =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<WsError> _errorsCtrl =
      StreamController<WsError>.broadcast();
  final StreamController<Map<String, dynamic>> _sharedDeletedCtrl =
      StreamController<Map<String, dynamic>>.broadcast();
  final StreamController<Map<String, dynamic>> _browserRequestsCtrl =
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
  Stream<WsError> get errors => _errorsCtrl.stream;

  @override
  Stream<Map<String, dynamic>> get sharedTerminalDeleted =>
      _sharedDeletedCtrl.stream;

  @override
  Stream<Map<String, dynamic>> get browserRequests =>
      _browserRequestsCtrl.stream;

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

  void emitError(WsError error) => _errorsCtrl.add(error);

  void emitSharedDeleted(Map<String, dynamic> msg) =>
      _sharedDeletedCtrl.add(msg);

  void close() {
    _customEventsCtrl.close();
    _errorsCtrl.close();
    _sharedDeletedCtrl.close();
    _browserRequestsCtrl.close();
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
        onPageError: (_) {},
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
        onPageError: (_) {},
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
        onPageError: (_) {},
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
        onPageError: (_) {},
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
        onPageError: (_) {},
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
      final errors = <WsError>[];

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPageError: (e) => errors.add(e),
      );

      await connector.connect();

      // Message-text fallback for older servers without the
      // machine-readable code (#2891).
      ws.emitError(const WsError(message: 'Permission denied'));
      await Future<void>.delayed(Duration.zero);

      expect(errors, [const WsError(message: 'Permission denied')]);
      expect(errors.first.accessRevoked, isTrue);

      // Non-access-revoked errors are ignored
      ws.emitError(const WsError(message: 'Connection timeout'));
      await Future<void>.delayed(Duration.zero);

      expect(errors, hasLength(1));

      connector.dispose();
      ws.close();
    });

    test('forwards non-permission errors to onRestartError (#2676)', () async {
      final ws = _MockWsClient();
      final restartErrors = <WsError>[];

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPageError: (_) {},
        onRestartError: (e) => restartErrors.add(e),
      );

      await connector.connect();

      // A refused restart (server sends an error frame instead of dropping
      // the socket) reaches the hook…
      ws.emitError(
        const WsError(
            message: 'Container restart failed: dependent containers'),
      );
      await Future<void>.delayed(Duration.zero);

      expect(
        restartErrors.map((e) => e.message),
        ['Container restart failed: dependent containers'],
      );

      // …but permission errors do not (they stay on onPageError).
      ws.emitError(const WsError(message: 'Permission denied'));
      await Future<void>.delayed(Duration.zero);

      expect(restartErrors, hasLength(1));

      connector.dispose();
      ws.close();
    });

    test('capacity refusals surface as page errors (#2525)', () async {
      final ws = _MockWsClient();
      final pageErrors = <WsError>[];
      final restartErrors = <WsError>[];

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPageError: (e) => pageErrors.add(e),
        onRestartError: (e) => restartErrors.add(e),
      );

      await connector.connect();

      // A host-capacity refusal must reach the page-error hook —
      // including on an initial connect where no restart is in flight —
      // instead of being silently dropped. Message-text fallback for
      // older servers first, then the machine-readable code.
      ws.emitError(
        const WsError(
          message: 'host at capacity: 1.2 GB available, workspace wants '
              '9.0 GB (memory limit 8.0 GB + 1.0 GB reserve)',
        ),
      );
      await Future<void>.delayed(Duration.zero);

      expect(pageErrors, hasLength(1));
      expect(pageErrors.first.message, contains('host at capacity'));
      expect(restartErrors, isEmpty);

      ws.emitError(
        const WsError(message: 'host at capacity', code: 'capacity'),
      );
      await Future<void>.delayed(Duration.zero);

      expect(pageErrors, hasLength(2));
      expect(restartErrors, isEmpty);

      // Same for a per-user quota refusal.
      ws.emitError(
        const WsError(
          message: "workspace quota reached: 2 of this user's workspaces "
              'are already running',
        ),
      );
      await Future<void>.delayed(Duration.zero);

      expect(pageErrors, hasLength(3));
      expect(pageErrors.last.message, contains('quota reached'));
      expect(restartErrors, isEmpty);

      connector.dispose();
      ws.close();
    });

    test('onRestartError absent: non-permission errors are dropped', () async {
      final ws = _MockWsClient();

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPageError: (_) {},
      );

      await connector.connect();

      ws.emitError(const WsError(message: 'Connection timeout'));
      await Future<void>.delayed(Duration.zero);

      connector.dispose();
      ws.close();
    });

    test('access-revoked refusals reach onPageError by code (#2891)', () async {
      final ws = _MockWsClient();
      final pageErrors = <WsError>[];
      final restartErrors = <WsError>[];

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-1',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPageError: (e) => pageErrors.add(e),
        onRestartError: (e) => restartErrors.add(e),
      );

      await connector.connect();

      // A revoked share / changed ACL refuses workspace_connect and
      // restart_container with the machine-readable `forbidden` code.
      // It must reach the page-error hook (which swaps the restart
      // overlay for the access-revoked view), never the restart hook —
      // otherwise the container-stopped overlay with its Restart
      // button re-appears and every press fails identically, forever.
      ws.emitError(
          const WsError(message: 'Permission denied', code: 'forbidden'));
      await Future<void>.delayed(Duration.zero);

      // A workspace deleted while the user was away (`not_found`) is
      // the same dead-end.
      ws.emitError(
        const WsError(message: 'Workspace not found', code: 'not_found'),
      );
      await Future<void>.delayed(Duration.zero);

      expect(pageErrors, hasLength(2));
      expect(pageErrors.first.code, 'forbidden');
      expect(pageErrors.first.accessRevoked, isTrue);
      expect(pageErrors.last.code, 'not_found');
      expect(pageErrors.last.accessRevoked, isTrue);
      expect(restartErrors, isEmpty);

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
        onPageError: (_) {},
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
        onPageError: (_) {},
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
        onPageError: (_) {},
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

    test('connect starts the browser delegate by default', () async {
      final ws = _MockWsClient();

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-123',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPageError: (_) {},
      );

      await connector.connect();

      // The delegate subscribed to bridge requests.
      expect(ws._browserRequestsCtrl.hasListener, isTrue);

      connector.dispose();
      ws.close();
    });

    test('skips the browser delegate when disabled (#2710)', () async {
      final ws = _MockWsClient();

      final connector = WorkspaceConnector(
        wsClient: ws,
        workspaceId: 'ws-123',
        featureRegistry: ToolPluginRegistry(),
        onConnected: ({required connected, error}) {},
        onContainerEvent: (_, __) {},
        onSharedTerminalDeleted: (_) {},
        onPageError: (_) {},
        browserDelegateEnabled: false,
      );

      await connector.connect();

      // No delegate: this tab never subscribes to bridge requests...
      expect(ws._browserRequestsCtrl.hasListener, isFalse);
      // ...but the connector itself is still active (subscriptions live).
      expect(connector.isActive, isTrue);

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
        onPageError: (_) {},
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
