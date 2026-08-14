/// Interactive egress-consent banner for the workspace page (#2246).
///
/// A compact banner shown above the workspace body when [ConsentDeciderService]
/// has pending held requests (interactive `egress_mode` only). Below the
/// header it carries a single *global* duration selector (#2499) -- one
/// compact button per selectable duration (default `tilrestart`), the active
/// one filled, mirroring the TUI's `#duration-selector` button row -- and
/// each row shows the destination host:port (+ process + a countdown) and
/// Allow/Deny buttons that send a `verdict` frame carrying the selected
/// duration. One selector for the whole list, applied at allow/deny time --
/// not per row -- matching the standalone `klangk consent-decide` TUI,
/// adapted to an inline Flutter banner.
///
/// Server error frames, verdict send failures, and verdicts attempted while
/// disconnected surface as a transient flash row (the service's
/// [ConsentDeciderService.flashMessage]) -- matching the TUI's status-line
/// flash, so a rejected/lost verdict is never silent.
///
/// The row is NOT optimistically removed on a click: the server's
/// `egress_resolved` frame removes it once the verdict is actually applied to
/// the held connection (a duplicate/no-op verdict must not hide a still-held
/// request) -- matching the TUI.

library;

import 'dart:async';

import 'package:flutter/material.dart';

import 'consent_decider_service.dart';
import '../widgets/option_button.dart';

/// A banner over the workspace body showing held egress requests + actions.
///
/// Renders nothing when there are no pending requests (and the service isn't
/// in an auth-failed state), so a static-mode or idle workspace sees no UI.
class ConsentBanner extends StatefulWidget {
  const ConsentBanner({super.key, required this.service});

  final ConsentDeciderService service;

  @override
  State<ConsentBanner> createState() => _ConsentBannerState();
}

class _ConsentBannerState extends State<ConsentBanner> {
  /// The chosen verdict duration, applied to whichever row's Allow/Deny is
  /// tapped. One selector for the whole banner (default `tilrestart`), mirroring
  /// the TUI's single global `#duration-selector` -- not a per-row choice.
  String _duration = kConsentDurationDefault;
  Timer? _tick;

  @override
  void initState() {
    super.initState();
    widget.service.addListener(_onChange);
    // coverage:ignore-start
    // 1s countdown refresh while requests are held (the server is the source
    // of truth; this only repaints the remaining-seconds hint).
    _tick = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted && widget.service.pending.isNotEmpty) _onChange();
    });
    // coverage:ignore-end
  }

  @override
  void dispose() {
    widget.service.removeListener(_onChange);
    _tick?.cancel();
    super.dispose();
  }

  void _onChange() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final service = widget.service;
    if (service.authFailed) {
      return const _BannerSurface(
        child: ListTile(
          dense: true,
          leading: Icon(Icons.lock_outline, size: 20),
          title: Text('Consent session expired — please log in again'),
        ),
      );
    }
    final pending = service.pending;
    if (pending.isEmpty) {
      return const SizedBox.shrink(); // nothing held -> no banner
    }
    final flash = service.flashMessage;
    return _BannerSurface(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 0),
            child: Row(
              children: [
                const Icon(Icons.shield_outlined, size: 18),
                const SizedBox(width: 8),
                Text(
                  'Pending egress consent',
                  style: Theme.of(context).textTheme.labelLarge,
                ),
                const Spacer(),
                if (!service.connected)
                  const Text(
                    'reconnecting…',
                    style: TextStyle(fontStyle: FontStyle.italic),
                  ),
              ],
            ),
          ),
          // One global duration selector (TUI parity, #2499): applies to the
          // next Allow/Deny tapped on any row. Not per row. Selecting does NOT
          // submit -- only a row's Allow/Deny submits with this duration.
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 0),
            child: _DurationSelector(
              value: _duration,
              onChanged: (v) => setState(() => _duration = v),
            ),
          ),
          if (flash != null)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 4, 16, 2),
              child: Row(
                children: [
                  const Icon(
                    Icons.error_outline,
                    size: 16,
                    color: Colors.redAccent,
                  ),
                  const SizedBox(width: 6),
                  Expanded(
                    child: Text(
                      flash,
                      style: const TextStyle(
                        color: Colors.redAccent,
                        fontSize: 13,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ...pending.map(_buildRow),
          const SizedBox(height: 4),
        ],
      ),
    );
  }

  Widget _buildRow(PendingRequest req) {
    final service = widget.service;
    final host = req.destHost;
    final hostDisplay = req.destPort != null ? '$host:${req.destPort}' : host;
    final proc = req.processName;
    final secs = service.remainingSeconds(req);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      child: Row(
        children: [
          Expanded(
            child: Text(
              proc == null || proc.isEmpty
                  ? '$hostDisplay  (${secs}s)'
                  : '$hostDisplay  ·  $proc  (${secs}s)',
              overflow: TextOverflow.ellipsis,
            ),
          ),
          const SizedBox(width: 8),
          _ActionChip(
            label: 'Allow',
            color: Colors.green,
            onPressed: () =>
                service.sendVerdict(req.id, kDecisionAllowed, _duration),
          ),
          const SizedBox(width: 4),
          _ActionChip(
            label: 'Deny',
            color: Colors.red,
            onPressed: () =>
                service.sendVerdict(req.id, kDecisionDenied, _duration),
          ),
        ],
      ),
    );
  }
}

/// The banner's surface (a flat warning-toned Material strip).
class _BannerSurface extends StatelessWidget {
  const _BannerSurface({required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Material(
      elevation: 0,
      color: Theme.of(context).colorScheme.secondaryContainer,
      child: child,
    );
  }
}

/// The global duration selector (#2499): one compact button per selectable
/// duration, the active one filled -- TUI parity (the `#duration-selector`
/// row with its accent `dur-sel` class, cli/tui/consent.py), styled like the
/// Net Rules pause buttons (#2497). Selecting does NOT submit -- only a
/// row's Allow/Deny submits with the chosen duration.
class _DurationSelector extends StatelessWidget {
  const _DurationSelector({required this.value, required this.onChanged});
  final String value;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context) {
    return Wrap(
      spacing: 6,
      runSpacing: 4,
      children: [for (final d in kConsentDurations) _button(d)],
    );
  }

  Widget _button(String d) {
    // Shared option-button look (#2502): amber fill for the active choice,
    // pill shape, aligned minimum size -- same control language as the Net
    // Rules pause buttons.
    return KOptionButton(
      buttonKey: ValueKey('dur-$d'),
      label: d,
      active: d == value,
      onPressed: () => onChanged(d),
    );
  }
}

/// A small colored action button (Allow/Deny) for a request row.
class _ActionChip extends StatelessWidget {
  const _ActionChip({
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
      height: 28,
      child: FilledButton.icon(
        onPressed: onPressed,
        style: FilledButton.styleFrom(
          backgroundColor: color,
          foregroundColor: Colors.white,
          padding: const EdgeInsets.symmetric(horizontal: 10),
          textStyle: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
        ),
        icon: const SizedBox.shrink(),
        label: Text(label),
      ),
    );
  }
}
