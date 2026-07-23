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
