/// Tests for the Restart-button delivery logic (#2674): a press with the
/// WebSocket up sends `restart_container`; a press with the socket down
/// (host restarted, auto-reconnect exhausted) reconnects instead of
/// dropping the send; a failed reconnect restores the retryable state.
import 'package:flutter_test/flutter_test.dart';
import 'package:klangk_frontend/workspace/restart_flow.dart';
import 'package:klangk_frontend/ws/ws_client.dart';

class _MockWsClient extends WsClient {
  bool _connected = false;
  bool connectCalled = false;
  bool connectShouldSucceed = true;
  bool restartSent = false;
  String? connectedWorkspaceId;

  @override
  bool get connected => _connected;

  @override
  Future<void> connect() async {
    connectCalled = true;
    _connected = connectShouldSucceed;
  }

  @override
  void connectWorkspace(String workspaceId) {
    connectedWorkspaceId = workspaceId;
  }

  @override
  void sendRestartContainer() {
    restartSent = true;
  }
}

void main() {
  test('socket up: sends restart_container directly', () async {
    final client = _MockWsClient().._connected = true;

    final result = await requestContainerRestart(
      wsClient: client,
      workspaceId: 'ws-1',
    );

    expect(result, RestartRequestResult.sent);
    expect(client.restartSent, isTrue);
    expect(client.connectCalled, isFalse);
    expect(client.connectedWorkspaceId, isNull);
  });

  test('socket down: reconnects and rejoins the workspace', () async {
    final client = _MockWsClient();

    final result = await requestContainerRestart(
      wsClient: client,
      workspaceId: 'ws-1',
    );

    expect(result, RestartRequestResult.reconnecting);
    expect(client.connectCalled, isTrue);
    expect(client.connectedWorkspaceId, 'ws-1');
    // The restart command itself is not sent — the workspace_connect on
    // reconnect auto-starts the container.
    expect(client.restartSent, isFalse);
  });

  test('socket down and server unreachable: reports failure', () async {
    final client = _MockWsClient()..connectShouldSucceed = false;

    final result = await requestContainerRestart(
      wsClient: client,
      workspaceId: 'ws-1',
    );

    expect(result, RestartRequestResult.failed);
    expect(client.connectCalled, isTrue);
    expect(client.connectedWorkspaceId, isNull);
    expect(client.restartSent, isFalse);
  });
}
