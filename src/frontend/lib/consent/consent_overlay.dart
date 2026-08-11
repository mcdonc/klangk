/// Egress-consent verdict overlay for the workspace page (#2333).
///
/// The in-app mirror of the TUI `consent-decide` client. Anchored in
/// [WorkspacePage]'s body `Stack` (not the terminal view), so it surfaces
/// held egress requests across **every** workspace-page tab — terminal,
/// files, settings, sharing, debug, and any feature tab.
///
/// Behavior (matches the issue's acceptance criteria):
///
/// - **Collapsed by default**: a compact bottom-corner chip showing the
///   held-request count (or an idle indicator) plus a pause-state button.
///   Obscures minimal screen area.
/// - **Auto-expands** when a held request needs a decision: shows each
///   request's destination host:port, process, and auto-deny countdown with
///   Allow / Deny actions. Collapses back when no requests are pending.
/// - **Fail-closed**: while the decider socket is disconnected the chip shows
///   a warning — in-flight holds auto-deny on their own server timeout
///   (mirrors the TUI, #2320).
///
/// The widget owns a [ConsentDeciderClient] (its own `/ws/consent-decider`
/// socket); pass `client` to inject a fake for tests.
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../auth/auth_service.dart';
import '../theme/colors.dart';
import 'consent_decider_client.dart';
import 'consent_request.dart';

/// A self-contained overlay that connects to the consent-decider stream for
/// [workspaceId] and renders the verdict UI.
class ConsentOverlay extends StatefulWidget {
  const ConsentOverlay({
    super.key,
    required this.workspaceId,
    this.client,
  });

  final String workspaceId;

  /// Optional injected client (tests). When null the widget constructs one
  /// from the ambient [AuthService] (via Provider) on mount.
  final ConsentDeciderClient? client;

  @override
  State<ConsentOverlay> createState() => _ConsentOverlayState();
}

class _ConsentOverlayState extends State<ConsentOverlay> {
  ConsentDeciderClient? _client;
  bool _ownsClient = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _client ??= () {
      if (widget.client != null) return widget.client;
      final c = ConsentDeciderClient(
        workspaceId: widget.workspaceId,
        auth: context.read<AuthService>(),
      );
      _ownsClient = true;
      // Fire-and-forget: the connection completes asynchronously; the widget
      // rebuilds via ChangeNotifier when state changes.
      c.connect();
      return c;
    }();
  }

  @override
  void dispose() {
    if (_ownsClient) {
      _client?.dispose();
    }
    super.dispose();
  }

  void _showPauseUnavailable() {
    // #2332: pause control has no backend today (the coordinator's
    // egress_rules frame always reports paused=null). The button is present
    // for UI completeness (#2333 acceptance criterion) but signals it isn't
    // wired rather than silently no-op-ing.
    final messenger = ScaffoldMessenger.maybeOf(context);
    if (messenger == null) return;
    messenger
      ..hideCurrentSnackBar()
      ..showSnackBar(
        const SnackBar(
          content: Text('Egress pause control is not yet available.'),
          duration: Duration(seconds: 3),
          behavior: SnackBarBehavior.floating,
        ),
      );
  }

  @override
  Widget build(BuildContext context) {
    final client = _client;
    if (client == null) return const SizedBox.shrink();
    return Positioned(
      bottom: 16,
      right: 16,
      child: ListenableBuilder(
        listenable: client,
        builder: (context, _) {
          return client.hasPending
              ? _VerdictPanel(
                  client: client,
                  onPause: _showPauseUnavailable,
                )
              : _CollapsedChip(
                  heldCount: 0,
                  connected: client.connected,
                  connecting: client.connecting,
                  onPause: _showPauseUnavailable,
                );
        },
      ),
    );
  }
}

/// The compact collapsed chip: held count + pause button. Shown when no
/// request is pending. Tints to a warning style while disconnected to convey
/// that any in-flight holds auto-deny (fail-closed).
class _CollapsedChip extends StatelessWidget {
  const _CollapsedChip({
    required this.heldCount,
    required this.connected,
    required this.connecting,
    required this.onPause,
  });

  final int heldCount;
  final bool connected;
  final bool connecting;
  final VoidCallback onPause;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isWarning = !connected && !connecting;
    return Material(
      elevation: 6,
      borderRadius: BorderRadius.circular(24),
      color: isWarning
          ? Colors.orange.shade900
          : theme.colorScheme.surfaceContainerHighest,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(
              isWarning ? Icons.warning_amber_rounded : Icons.shield_outlined,
              size: 18,
              color: isWarning ? Colors.white : theme.colorScheme.onSurface,
            ),
            const SizedBox(width: 6),
            Text(
              _label(),
              style: TextStyle(
                color: isWarning ? Colors.white : theme.colorScheme.onSurface,
                fontSize: 13,
                fontWeight: FontWeight.w600,
              ),
            ),
            const SizedBox(width: 4),
            _PauseButton(onPause: onPause, light: isWarning),
          ],
        ),
      ),
    );
  }

  String _label() {
    if (connecting) return 'Consent…';
    if (!connected) return 'Auto-deny';
    return 'Egress: $heldCount held';
  }
}

/// The expanded verdict panel: a header (count + pause + collapse hint) and a
/// scrollable list of held requests, each with host:port, process, countdown,
/// and Allow / Deny actions.
class _VerdictPanel extends StatelessWidget {
  const _VerdictPanel({required this.client, required this.onPause});

  final ConsentDeciderClient client;
  final VoidCallback onPause;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final pending = client.pending;
    return Material(
      elevation: 8,
      borderRadius: BorderRadius.circular(12),
      color: theme.colorScheme.surface,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 380, maxHeight: 400),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(12, 8, 8, 4),
              child: Row(
                children: [
                  Icon(
                    Icons.shield,
                    size: 18,
                    color: theme.colorScheme.primary,
                  ),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      '${pending.length} egress request'
                      '${pending.length == 1 ? '' : 's'} held',
                      style: theme.textTheme.titleSmall,
                    ),
                  ),
                  if (!client.connected)
                    Tooltip(
                      message: 'Disconnected — held requests auto-deny',
                      child: Icon(
                        Icons.warning_amber_rounded,
                        size: 18,
                        color: Colors.orange.shade700,
                      ),
                    ),
                  _PauseButton(onPause: onPause, light: false),
                ],
              ),
            ),
            const Divider(height: 1),
            Flexible(
              child: ListView.separated(
                shrinkWrap: true,
                padding: const EdgeInsets.symmetric(vertical: 4),
                itemCount: pending.length,
                separatorBuilder: (_, __) => const Divider(height: 1),
                itemBuilder: (context, i) =>
                    _RequestRow(client: client, req: pending[i]),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _RequestRow extends StatelessWidget {
  const _RequestRow({required this.client, required this.req});

  final ConsentDeciderClient client;
  final ConsentRequest req;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final secs = client.remaining(req).ceil();
    final host =
        req.destPort != null ? '${req.destHost}:${req.destPort}' : req.destHost;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      child: Row(
        children: [
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  host,
                  style: theme.textTheme.bodyMedium?.copyWith(
                    fontWeight: FontWeight.w600,
                  ),
                ),
                if (req.processName != null && req.processName!.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      req.processName!,
                      style: theme.textTheme.bodySmall?.copyWith(
                        color: theme.colorScheme.onSurfaceVariant,
                      ),
                    ),
                  ),
              ],
            ),
          ),
          const SizedBox(width: 8),
          _CountdownBadge(seconds: secs),
          const SizedBox(width: 8),
          _VerdictButtons(client: client, requestId: req.id),
        ],
      ),
    );
  }
}

class _CountdownBadge extends StatelessWidget {
  const _CountdownBadge({required this.seconds});
  final int seconds;

  @override
  Widget build(BuildContext context) {
    final urgent = seconds <= 10;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
      decoration: BoxDecoration(
        color: urgent ? Colors.red.shade700 : Colors.grey.shade300,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Text(
        '${seconds}s',
        style: TextStyle(
          color: urgent ? Colors.white : Colors.black87,
          fontSize: 12,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

class _VerdictButtons extends StatelessWidget {
  const _VerdictButtons({required this.client, required this.requestId});
  final ConsentDeciderClient client;
  final String requestId;

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        _CompactButton(
          label: 'Allow',
          color: KColors.accentGreen,
          onPressed: () => client.allow(requestId),
        ),
        const SizedBox(width: 4),
        _CompactButton(
          label: 'Deny',
          color: Colors.red.shade700,
          onPressed: () => client.deny(requestId),
        ),
      ],
    );
  }
}

class _CompactButton extends StatelessWidget {
  const _CompactButton({
    required this.label,
    required this.color,
    required this.onPressed,
  });

  final String label;
  final Color color;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      height: 32,
      child: FilledButton(
        onPressed: onPressed,
        style: FilledButton.styleFrom(
          backgroundColor: color,
          foregroundColor: Colors.white,
          padding: const EdgeInsets.symmetric(horizontal: 12),
          textStyle: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
        ),
        child: Text(label),
      ),
    );
  }
}

class _PauseButton extends StatelessWidget {
  const _PauseButton({required this.onPause, required this.light});
  final VoidCallback onPause;
  final bool light;

  @override
  Widget build(BuildContext context) {
    return IconButton(
      onPressed: onPause,
      icon: const Icon(Icons.pause_circle_outline, size: 20),
      tooltip: 'Pause egress filtering (unavailable)',
      visualDensity: VisualDensity.compact,
      color: light ? Colors.white : null,
      constraints: const BoxConstraints(minWidth: 32, minHeight: 32),
      padding: EdgeInsets.zero,
    );
  }
}
