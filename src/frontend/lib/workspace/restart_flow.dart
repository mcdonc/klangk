/// Delivery logic for the workspace Restart button (#2674).
///
/// When the host shuts down gracefully, the drain broadcast leaves the
/// container-stopped overlay on screen with its Restart button — and the
/// WebSocket drops moments later when the server exits. After a bounded
/// number of auto-reconnect attempts (far fewer than a host reboot takes)
/// [WsClient] stops retrying, so a Restart press with the socket down
/// would be silently dropped by `WsClient._send` and the "Restarting..."
/// spinner would never clear.
import '../ws/ws_client.dart';

/// Outcome of a Restart-button press.
enum RestartRequestResult {
  /// `restart_container` was sent over the live WebSocket. The caller's
  /// spinner clears on the `container_ready` event.
  sent,

  /// The socket was down, so a reconnect was performed instead. The
  /// `workspace_connect` sent on reconnect auto-starts the container;
  /// the `container_ready` event that follows clears the spinner and
  /// reattaches the terminal.
  reconnecting,

  /// The socket was down and the reconnect attempt failed (server still
  /// unreachable). Nothing was delivered; the caller should restore the
  /// Restart button so the user can retry.
  failed,
}

/// Deliver a container-restart request for [workspaceId] over [wsClient].
Future<RestartRequestResult> requestContainerRestart({
  required WsClient wsClient,
  required String workspaceId,
}) async {
  if (wsClient.connected) {
    wsClient.sendRestartContainer();
    return RestartRequestResult.sent;
  }
  // Socket down (host restart): reconnect instead of sending into the
  // void. Reconnecting also re-arms auto-reconnect (connectWorkspace
  // sets the pending-workspace + auto-reconnect flags), so future drops
  // retry again instead of staying given-up.
  await wsClient.connect();
  if (!wsClient.connected) {
    return RestartRequestResult.failed;
  }
  wsClient.connectWorkspace(workspaceId);
  return RestartRequestResult.reconnecting;
}
