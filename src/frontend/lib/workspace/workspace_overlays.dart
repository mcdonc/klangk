/// Full-screen status overlays for the workspace page (container-stopped and
/// WebSocket-disconnected).
///
/// Extracted from `WorkspacePage.build` into standalone, parameterised
/// builders so they can be tested directly without mounting the full
/// `WorkspacePage` (which depends on klangk_features / dart:js_interop and a
/// live WsClient/AuthService). Previously the test suite duplicated these
/// widgets as standalone copies — testing the copies, not the real code.
import 'package:flutter/material.dart';

import '../theme/colors.dart';

/// Immutable next-state for the container-stopped / restarting flags that
/// drive [buildContainerStoppedOverlay]. Returned by
/// [containerEventTransition]; `null` from it means "no change".
typedef ContainerOverlayState = ({
  bool containerStopped,
  bool restarting,
  String stopReason,
});

/// Pure state transition for the container lifecycle events that drive the
/// container-stopped overlay. Kept beside the overlay builder it feeds so it
/// is unit-testable without mounting the full `WorkspacePage` (same rationale
/// as the builders below).
///
/// - `container_stopped` raises the overlay with the event's reason, except
///   #2661: a stop whose reason is the server's own recycle is NOT
///   user-actionable — the server stays up, the WebSocket drops with 1012
///   and auto-reconnects, and auto-start brings the workspace back. Raising
///   the blocking "stopped — click Restart" overlay would demand a click for
///   a cycle that needs none; the reconnect overlay owns the gap. An event
///   while the overlay is already up is likewise a no-op.
/// - `container_ready` clears the overlay AND any in-flight restart spinner.
///   #2701: the container can come back without this client pressing the
///   overlay's Restart button (socket reconnect after a server cycle, the
///   workspace auto-starting, a restart initiated from this client's
///   settings panel) — the overlay must clear on ANY `container_ready` on
///   this socket, not just one that follows an overlay-initiated restart,
///   or it stays up demanding a manual Restart click over a live terminal.
///   A routine `container_ready` (nothing raised, nothing in flight) is a
///   no-op so it does not trigger a rebuild.
ContainerOverlayState? containerEventTransition({
  required String name,
  required ContainerOverlayState current,
  Map<String, dynamic>? value,
}) {
  if (name == 'container_stopped') {
    if (current.containerStopped) return null;
    final reason = value?['reason']?.toString() ?? '';
    if (reason == 'server recycle') return null;
    return (
      containerStopped: true,
      restarting: current.restarting,
      stopReason:
          reason.isEmpty ? 'Container stopped' : 'Container stopped ($reason)',
    );
  }
  if (name == 'container_ready' &&
      (current.restarting || current.containerStopped)) {
    return (
      containerStopped: false,
      restarting: false,
      stopReason: current.stopReason,
    );
  }
  return null;
}

/// Overlay shown when the workspace container has stopped (idle timeout,
/// manual stop, or crash). Pass [restarting] to swap the action area for a
/// spinner; [stopReason] is shown verbatim when not restarting.
Widget buildContainerStoppedOverlay({
  required bool restarting,
  required String stopReason,
  required VoidCallback onRestart,
  required VoidCallback onBack,
}) {
  return Container(
    color: Colors.black54,
    child: Center(
      child: restarting
          ? const Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                CircularProgressIndicator(color: Colors.white),
                SizedBox(height: 12),
                Text(
                  'Restarting...',
                  style: TextStyle(color: Colors.white),
                ),
              ],
            )
          : Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  stopReason,
                  style: const TextStyle(color: Colors.white, fontSize: 16),
                ),
                const SizedBox(height: 16),
                ElevatedButton.icon(
                  onPressed: onRestart,
                  icon: const Icon(Icons.refresh, size: 18),
                  label: const Text('Restart'),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: KColors.accentGreen,
                    foregroundColor: Colors.white,
                  ),
                ),
                const SizedBox(height: 12),
                TextButton(
                  onPressed: onBack,
                  child: const Text(
                    'Back to workspaces',
                    style: TextStyle(color: Colors.white54),
                  ),
                ),
              ],
            ),
    ),
  );
}

/// Overlay shown when the WebSocket drops but the container is still running.
/// Pass [reconnecting] to show the attempt counter + "Reconnect now" instead
/// of the plain "Reconnect" action.
Widget buildDisconnectedOverlay({
  required bool reconnecting,
  required int reconnectAttempt,
  required VoidCallback onReconnect,
  required VoidCallback onBack,
}) {
  return Container(
    color: Colors.black54,
    child: Center(
      child: reconnecting
          ? Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const CircularProgressIndicator(color: Colors.white),
                const SizedBox(height: 12),
                Text(
                  'Reconnecting (attempt $reconnectAttempt)...',
                  style: const TextStyle(color: Colors.white),
                ),
                const SizedBox(height: 16),
                ElevatedButton.icon(
                  onPressed: onReconnect,
                  icon: const Icon(Icons.refresh, size: 18),
                  label: const Text('Reconnect now'),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: KColors.accentGreen,
                    foregroundColor: Colors.white,
                  ),
                ),
                const SizedBox(height: 12),
                TextButton(
                  onPressed: onBack,
                  child: const Text(
                    'Back to workspaces',
                    style: TextStyle(color: Colors.white54),
                  ),
                ),
              ],
            )
          : Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                const Text(
                  'Connection lost',
                  style: TextStyle(color: Colors.white, fontSize: 16),
                ),
                const SizedBox(height: 16),
                ElevatedButton.icon(
                  onPressed: onReconnect,
                  icon: const Icon(Icons.refresh, size: 18),
                  label: const Text('Reconnect'),
                  style: ElevatedButton.styleFrom(
                    backgroundColor: KColors.accentGreen,
                    foregroundColor: Colors.white,
                  ),
                ),
                const SizedBox(height: 12),
                TextButton(
                  onPressed: onBack,
                  child: const Text(
                    'Back to workspaces',
                    style: TextStyle(color: Colors.white54),
                  ),
                ),
              ],
            ),
    ),
  );
}
