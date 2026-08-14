/// Interactive egress-consent banner for the workspace page (#2246).
///
/// A compact banner shown above the workspace body when [ConsentDeciderService]
/// has pending held requests (interactive `egress_mode` only). Each row shows
/// the destination host:port (+ process + a countdown) and an Allow/Deny
/// **split button**: a bare click sends the `verdict` frame with the default
/// duration (`tilrestart`), and the attached `▾` segment opens a menu of
/// durations -- picking one sends the verdict with that duration immediately.
/// The duration is chosen with the click, never armed beforehand: the earlier
/// global duration pill row (#2499) put the selector far from the row actions
/// and left a selection armed that silently applied to whichever row was
/// clicked next, so it was replaced by this per-row pattern (the
/// pointer-first web analogue of the keyboard-first `klangk consent-decide`
/// TUI's global selector).
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

/// Human labels for the verdict durations (menu display only -- the wire
/// tokens from [kConsentDurations] stay raw). The default gets its "(default)"
/// suffix in the menu item, not here.
const Map<String, String> _durationLabels = {
  'once': 'Just once',
  '5m': '5 minutes',
  '15m': '15 minutes',
  '1h': '1 hour',
  '1d': '1 day',
  '1w': '1 week',
  'tilrestart': 'Until restart',
  'forever': 'Forever',
};

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
          _VerdictButton(
            label: 'Allow',
            color: Colors.green,
            menuKey: ValueKey('allow-dur-${req.id}'),
            service: service,
            requestId: req.id,
            onVerdict: (d) => service.sendVerdict(req.id, kDecisionAllowed, d),
          ),
          const SizedBox(width: 6),
          _VerdictButton(
            label: 'Deny',
            color: Colors.red,
            menuKey: ValueKey('deny-dur-${req.id}'),
            service: service,
            requestId: req.id,
            onVerdict: (d) => service.sendVerdict(req.id, kDecisionDenied, d),
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

/// A split action button (Allow/Deny) for one request row: the main segment
/// submits with the default duration, the attached `▾` segment opens a menu
/// and a pick submits with that duration right away. Attaching the menu to
/// the action keeps the duration choice next to the Allow/Deny it applies to
/// (the #2499 global pill row was replaced by this per-row pattern).
class _VerdictButton extends StatelessWidget {
  const _VerdictButton({
    required this.label,
    required this.color,
    required this.menuKey,
    required this.service,
    required this.requestId,
    required this.onVerdict,
  });

  final String label;
  final Color color;

  /// Key on the `▾` segment (unique per row, e.g. `allow-dur-<requestId>`).
  final Key menuKey;

  /// The banner's service -- watched while the menu is open so the menu can
  /// close itself if its row's request resolves mid-menu (see [_openMenu]).
  final ConsentDeciderService service;

  /// The row's request id (the verdict target).
  final String requestId;

  /// Sends this button's decision with `duration` (a [kConsentDurations]
  /// token).
  final void Function(String duration) onVerdict;

  /// Display label for a duration token; falls back to the raw token if the
  /// map ever lags behind [kConsentDurations] (degrades, never crashes).
  static String _label(String d) => _durationLabels[d] ?? d;

  ButtonStyle _style(Color color, BorderRadius radius) {
    return FilledButton.styleFrom(
      backgroundColor: color,
      foregroundColor: Colors.white,
      minimumSize: const Size(0, 28),
      padding: const EdgeInsets.symmetric(horizontal: 10),
      textStyle: const TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
      visualDensity: VisualDensity.compact,
      tapTargetSize: MaterialTapTargetSize.shrinkWrap,
      shape: RoundedRectangleBorder(borderRadius: radius),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Tooltip(
          message: '$label ${_label(kConsentDurationDefault).toLowerCase()}',
          child: FilledButton(
            onPressed: () => onVerdict(kConsentDurationDefault),
            style: _style(
              color,
              const BorderRadius.horizontal(left: Radius.circular(6)),
            ),
            child: Text(label),
          ),
        ),
        // Builder so the menu anchors to the ▾ segment's own render box.
        Builder(
          builder: (ctx) => Tooltip(
            message: 'Choose duration',
            child: FilledButton(
              key: menuKey,
              onPressed: () => _openMenu(ctx),
              style: _style(
                color,
                const BorderRadius.horizontal(right: Radius.circular(6)),
              ),
              child: const Icon(Icons.arrow_drop_down, size: 18),
            ),
          ),
        ),
      ],
    );
  }

  /// Open the duration menu below the `▾` segment; a pick submits the verdict
  /// with the chosen duration, dismissing without a pick sends nothing.
  ///
  /// The position is the button's bottom edge expressed relative to the
  /// overlay (`RelativeRect.fromRect`), so the menu's placement delegate
  /// clamps it inside the viewport -- a plain `RelativeRect.fromLTRB(dx, dy,
  /// dx, dy)` inverts its right edge once the button sits right of the
  /// viewport's midpoint and pushes menu items off-screen at phone widths.
  ///
  /// If the row's request resolves while the menu is open, the menu closes
  /// itself: the verdict target is gone, and a pick would otherwise send a
  /// verdict the server silently drops (the banner's no-silent-loss promise
  /// covers rejected sends, not ones aimed at a dead id).
  void _openMenu(BuildContext context) {
    final button = context.findRenderObject() as RenderBox;
    final overlay = Overlay.of(context).context.findRenderObject() as RenderBox;
    final topLeft = button.localToGlobal(
      Offset(0, button.size.height + 4),
      ancestor: overlay,
    );
    final position = RelativeRect.fromRect(
      Rect.fromLTWH(topLeft.dx, topLeft.dy, button.size.width, 0),
      Offset.zero & overlay.size,
    );
    final nav = Navigator.of(context);
    void closeIfGone() {
      if (service.pending.every((r) => r.id != requestId)) {
        service.removeListener(closeIfGone);
        if (nav.mounted) nav.pop(); // dismiss the menu; no verdict is sent
      }
    }

    service.addListener(closeIfGone);
    final menu = showMenu<String>(
      context: context,
      position: position,
      items: [
        for (final d in kConsentDurations)
          PopupMenuItem(
            value: d,
            child: Text(
              d == kConsentDurationDefault
                  ? '${_label(d)} (default)'
                  : _label(d),
            ),
          ),
      ],
    );
    menu.whenComplete(() => service.removeListener(closeIfGone));
    menu.then((d) {
      if (d != null) onVerdict(d);
    });
  }
}
