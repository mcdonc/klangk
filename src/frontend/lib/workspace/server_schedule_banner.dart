/// Scheduled server stop/recycle banner (#2661).
///
/// A persistent banner shown above the workspace body while the server
/// has a pending server stop or recycle schedule. It renders the next
/// (soonest) action with a **live countdown** (a 1s `Timer` — the server
/// pushes the `server_schedule` snapshot only on change and periodically;
/// the countdown ticks locally from the schedule's `fire_at`), so every
/// connected user knows the server is going down and can save work.
///
/// Non-blocking, like the #2527 host notices: it never gates the
/// reconnect machinery.

library;

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../ws/ws_client.dart';

/// Parses an ISO-8601 `fire_at` into a local [DateTime]; null when absent
/// or malformed (callers fall back to a static line).
DateTime? parseFireAt(dynamic raw) {
  if (raw is! String || raw.isEmpty) return null;
  return DateTime.tryParse(raw)?.toLocal();
}

/// "1h 12m" / "12m" / "45s" — coarse, human, stable-width-enough.
String remainingLabel(Duration d) {
  final s = d.inSeconds < 0 ? 0 : d.inSeconds;
  if (s >= 3600) return '${s ~/ 3600}h ${(s % 3600) ~/ 60}m';
  if (s >= 60) return '${s ~/ 60}m';
  return '${s}s';
}

/// Banner widget: listens to [WsClient] and shows the next scheduled
/// server action (stop / recycle) with a live countdown. Renders
/// nothing when no schedule is pending.
class ServerScheduleBanner extends StatefulWidget {
  // Non-const on purpose: a const constructor's declaration line never
  // executes (canonical const instance), which makes the 100% coverage
  // gate flag it nondeterministically across toolchains.
  ServerScheduleBanner({super.key});

  @override
  State<ServerScheduleBanner> createState() => _ServerScheduleBannerState();
}

class _ServerScheduleBannerState extends State<ServerScheduleBanner> {
  Timer? _ticker;

  @override
  void initState() {
    super.initState();
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });
  }

  @override
  void dispose() {
    _ticker?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final schedules = context.read<WsClient>().serverSchedulesNow;
    if (schedules == null || schedules.isEmpty) return const SizedBox.shrink();

    // Soonest fire_at first (the server sends them ordered, but be safe).
    final parsed = <(String, DateTime)>[
      for (final s in schedules)
        if (parseFireAt(s['fire_at']) != null)
          (s['action'] as String? ?? 'action', parseFireAt(s['fire_at'])!),
    ]..sort((a, b) => a.$2.compareTo(b.$2));
    if (parsed.isEmpty) {
      final action = schedules.first['action'] as String? ?? 'action';
      return _row('⏻ Scheduled server $action');
    }

    final (action, fireAt) = parsed.first;
    final remaining = fireAt.difference(DateTime.now());
    final verb = action == 'recycle' ? 'recycles' : 'stops';
    final label = remaining.isNegative
        ? 'server $action happening now'
        : 'Server $verb at ${MaterialLocalizations.of(context).formatTimeOfDay(TimeOfDay.fromDateTime(fireAt))} '
            '(in ${remainingLabel(remaining)} — workspaces stop)';
    return _row('⏻ $label');
  }

  Widget _row(String text) {
    return Material(
      color: Theme.of(context).colorScheme.errorContainer,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
        child: Row(
          children: [
            Expanded(
              child: Text(
                text,
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onErrorContainer,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
