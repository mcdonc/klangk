// coverage:ignore-file
/// Admin → Server tab: schedule a server stop/recycle and manage the
/// pending schedules (#2684).
///
/// The pending list is snapshot-driven: the `server_schedule` WS frame
/// (broadcast on every change, see [WsClient]) is the live truth, with a
/// REST `GET /api/v1/server/schedule` load as the source before the
/// first snapshot arrives (and as a post-mutation refresh). Countdowns
/// tick locally from each schedule's `fire_at` — same approach as the
/// client banner (#2661), whose parse/format helpers are reused here.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../auth/auth_service.dart';
import '../theme/colors.dart';
import '../workspace/server_schedule_banner.dart'
    show parseFireAt, remainingLabel;
import '../ws/ws_client.dart';

/// Parse a human delay ("2h", "90m", "2h 30m", "45s") into a [Duration].
///
/// A bare number means minutes ("120" == "120m" == "2h"). Returns null
/// for anything unparseable, non-positive, non-finite, or beyond the
/// server's max delay (`in_seconds` <= 1e10 — the API 422s anything
/// longer) — the dialog treats null as an invalid form. The finiteness
/// and bound checks also keep `Duration(microseconds: ...)` from ever
/// seeing an infinity (`.round()` would throw mid-build).
Duration? parseServerDelay(String raw) {
  // Server-side cap for in_seconds (server_schedule._MAX_IN_SECONDS).
  const maxMicros = 1e10 * 1e6;
  final text = raw.trim().toLowerCase();
  if (text.isEmpty) return null;
  final bare = double.tryParse(text);
  if (bare != null) {
    if (!bare.isFinite || bare <= 0) return null;
    final micros = bare * 60 * 1e6;
    if (!micros.isFinite || micros > maxMicros) return null;
    return Duration(microseconds: micros.round());
  }
  final segment = RegExp(r'^(\d+(?:\.\d+)?)(h|m|s)');
  var rest = text.replaceAll(' ', '');
  var micros = 0.0;
  while (rest.isNotEmpty) {
    final m = segment.firstMatch(rest);
    if (m == null) return null;
    final value = double.parse(m.group(1)!);
    final factor = switch (m.group(2)!) {
      'h' => 3600e6,
      'm' => 60e6,
      _ => 1e6,
    };
    micros += value * factor;
    if (!micros.isFinite || micros > maxMicros) return null;
    rest = rest.substring(m.end);
  }
  if (micros <= 0) return null;
  return Duration(microseconds: micros.round());
}

/// The Admin → Server tab body: pending schedule list plus the
/// "schedule an action" FAB.
class ServerSchedulePanel extends StatefulWidget {
  const ServerSchedulePanel({super.key});

  @override
  State<ServerSchedulePanel> createState() => _ServerSchedulePanelState();
}

class _ServerSchedulePanelState extends State<ServerSchedulePanel> {
  List<Map<String, dynamic>> _restSchedules = [];
  bool _loading = true;
  String? _error;
  Timer? _ticker;

  @override
  void initState() {
    super.initState();
    // Live countdowns tick locally from fire_at (the WS snapshot pushes
    // only on change + periodically — same rationale as the banner).
    _ticker = Timer.periodic(const Duration(seconds: 1), (_) {
      if (!mounted) return;
      // Only a non-empty list has a countdown worth re-rendering — an
      // idle tab (nothing scheduled) must not rebuild every second.
      final ws = context.read<WsClient>();
      final hasRows = (ws.serverSchedulesNow ?? _restSchedules).isNotEmpty;
      if (hasRows) setState(() {});
    });
    _loadSchedules();
  }

  @override
  void dispose() {
    _ticker?.cancel();
    super.dispose();
  }

  Future<void> _loadSchedules() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final auth = context.read<AuthService>();
      final resp = await auth.authGet('/api/v1/server/schedule');
      if (!mounted) return;
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as Map<String, dynamic>;
        setState(() {
          _restSchedules =
              (data['schedules'] as List).cast<Map<String, dynamic>>();
          _loading = false;
        });
      } else {
        setState(() {
          _error = 'Failed to load schedules (${resp.statusCode})';
          _loading = false;
        });
      }
    } catch (e) {
      debugPrint('[ServerSchedulePanel] load schedules failed: $e');
      if (mounted) {
        setState(() {
          _error = 'Could not load schedules. Please try again.';
          _loading = false;
        });
      }
    }
  }

  Future<void> _scheduleAction() async {
    final created = await showDialog<bool>(
      context: context,
      builder: (ctx) => const ScheduleServerActionDialog(),
    );
    if (!mounted) return;
    // Refresh the REST fallback list regardless; the WS snapshot (the
    // preferred source) is broadcast by the server on create.
    if (created == true) await _loadSchedules();
  }

  Future<void> _cancelSchedule(Map<String, dynamic> schedule) async {
    final action = schedule['action'] as String? ?? 'action';
    final fireAt = parseFireAt(schedule['fire_at']);
    final when = fireAt == null
        ? ''
        : ' at ${MaterialLocalizations.of(context).formatTimeOfDay(TimeOfDay.fromDateTime(fireAt))}';
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Cancel Scheduled $action'),
        content: Text(
          'Cancel the scheduled server $action$when? It will not fire.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
            child: const Text('Keep'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: FilledButton.styleFrom(
              backgroundColor: KColors.accentRed,
              foregroundColor: Colors.white,
            ),
            child: const Text('Cancel Schedule'),
          ),
        ],
      ),
    );
    if (confirm != true) return;
    if (!mounted) return;
    final auth = context.read<AuthService>();
    final resp =
        await auth.authDelete('/api/v1/server/schedule/${schedule['id']}');
    if (!mounted) return;
    if (resp.statusCode == 200) {
      // The authoritative update is the next `server_schedule` WS
      // snapshot; this REST refresh only covers a client whose socket
      // is not connected.
      await _loadSchedules();
    } else {
      String detail = 'Failed to cancel (${resp.statusCode})';
      try {
        detail = jsonDecode(resp.body)['detail'] ?? detail;
      } catch (_) {}
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(SnackBar(content: Text('$detail')));
    }
  }

  @override
  Widget build(BuildContext context) {
    // The WS snapshot (when connected) is the live truth; the REST list
    // is the pre-snapshot / disconnected fallback.
    final ws = context.watch<WsClient>();
    final live = ws.serverSchedulesNow;
    final schedules = live ?? _restSchedules;
    final sorted = [...schedules]..sort(
        (a, b) =>
            (parseFireAt(a['fire_at'])?.millisecondsSinceEpoch ?? 0).compareTo(
          parseFireAt(b['fire_at'])?.millisecondsSinceEpoch ?? 0,
        ),
      );

    return Scaffold(
      floatingActionButton: FloatingActionButton(
        heroTag: 'schedule-server',
        onPressed: _scheduleAction,
        tooltip: 'Schedule server action',
        child: const Icon(Icons.add_alarm),
      ),
      body: _buildList(sorted, hasLive: live != null),
    );
  }

  Widget _buildList(
    List<Map<String, dynamic>> schedules, {
    required bool hasLive,
  }) {
    // The spinner/error states describe the REST fetch; when a live WS
    // snapshot is on screen it stays rendered through any REST
    // refresh/failure — no flash-to-spinner on every mutation, and a
    // transient REST failure never masks live data.
    if (!hasLive && _loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (!hasLive && _error != null) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(_error!),
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: _loadSchedules,
              child: const Text('Retry'),
            ),
          ],
        ),
      );
    }
    if (schedules.isEmpty) {
      return const Center(
        child: Text('No scheduled server actions'),
      );
    }
    return ListView.builder(
      padding: const EdgeInsets.all(16),
      itemCount: schedules.length,
      itemBuilder: (ctx, i) {
        final schedule = schedules[i];
        final action = schedule['action'] as String? ?? 'action';
        final fireAt = parseFireAt(schedule['fire_at']);
        final isStop = action == 'stop';
        final remaining =
            fireAt == null ? null : fireAt.difference(DateTime.now());
        final localizations = MaterialLocalizations.of(context);
        final title = fireAt == null
            ? 'Scheduled server $action'
            : '${isStop ? 'Stop' : 'Recycle'} at '
                '${localizations.formatMediumDate(fireAt)} '
                '${localizations.formatTimeOfDay(TimeOfDay.fromDateTime(fireAt))}';
        final subtitle = remaining == null
            ? 'fires soon'
            : remaining.isNegative
                ? 'happening now'
                : 'fires in ${remainingLabel(remaining)}';
        return Card(
          margin: const EdgeInsets.only(bottom: 8),
          child: ListTile(
            leading: Icon(
              isStop ? Icons.power_settings_new : Icons.autorenew,
              color: isStop ? KColors.accentRed : KColors.accentAmber,
            ),
            title: Text(title),
            subtitle: Text(
              subtitle,
              style: const TextStyle(color: KColors.textSecondary),
            ),
            trailing: IconButton(
              icon: const Icon(Icons.event_busy, color: KColors.accentRed),
              tooltip: 'Cancel schedule',
              onPressed: () => _cancelSchedule(schedule),
            ),
          ),
        );
      },
    );
  }
}

/// Dialog: pick an action (stop / recycle) and either an absolute time
/// (date + time pickers) or a human delay. Surfaces the API's 422 detail
/// inline; pops with `true` when a schedule was created.
class ScheduleServerActionDialog extends StatefulWidget {
  const ScheduleServerActionDialog({super.key});

  @override
  State<ScheduleServerActionDialog> createState() =>
      _ScheduleServerActionDialogState();
}

class _ScheduleServerActionDialogState
    extends State<ScheduleServerActionDialog> {
  String _action = 'stop'; // 'stop' | 'recycle'
  bool _useDelay = true; // delay | absolute time
  final _delayController = TextEditingController();
  DateTime? _at;
  String? _error;
  bool _submitting = false;

  @override
  void dispose() {
    _delayController.dispose();
    super.dispose();
  }

  Duration? get _delay => parseServerDelay(_delayController.text);

  /// The picked time as a readable local label ('Aug 24, 11:00 PM').
  String get _atLabel => _at == null
      ? 'Pick date and time'
      : '${MaterialLocalizations.of(context).formatMediumDate(_at!)} '
          '${MaterialLocalizations.of(context).formatTimeOfDay(TimeOfDay.fromDateTime(_at!))}';

  bool get _formValid =>
      _validationError == null && (_useDelay ? _delay != null : _at != null);

  /// One-line preview of when the form will fire (delay mode only).
  String? get _firesLabel {
    if (!_useDelay || _delay == null) return null;
    final localizations = MaterialLocalizations.of(context);
    final firesAt = DateTime.now().add(_delay!);
    return 'Fires ${localizations.formatMediumDate(firesAt)} at '
        '${localizations.formatTimeOfDay(TimeOfDay.fromDateTime(firesAt))} '
        '(in ${remainingLabel(_delay!)})';
  }

  /// Client-side pre-checks; empty when the form may be submitted.
  String? get _validationError {
    if (_useDelay) {
      if (_delayController.text.trim().isEmpty) return null;
      return _delay == null
          ? 'Enter a delay like "2h", "90m", "45s", or minutes'
          : null;
    }
    if (_at != null && !_at!.isAfter(DateTime.now())) {
      return 'The chosen time is in the past';
    }
    return null;
  }

  Future<void> _pickDateTime() async {
    final now = DateTime.now();
    final date = await showDatePicker(
      context: context,
      initialDate: _at ?? now.add(const Duration(hours: 1)),
      firstDate: now.subtract(const Duration(days: 1)),
      lastDate: now.add(const Duration(days: 365 * 5)),
    );
    if (date == null || !mounted) return;
    final time = await showTimePicker(
      context: context,
      initialTime:
          TimeOfDay.fromDateTime(_at ?? now.add(const Duration(hours: 1))),
    );
    if (time == null || !mounted) return;
    setState(() {
      _at = DateTime(
        date.year,
        date.month,
        date.day,
        time.hour,
        time.minute,
      );
    });
  }

  Future<void> _submit() async {
    setState(() => _submitting = true);
    final body = jsonEncode({
      'action': _action,
      if (_useDelay)
        'in_seconds': _delay!.inMilliseconds / 1000
      else
        // toUtc() so the ISO string carries a 'Z' suffix: a local
        // DateTime's toIso8601String() has no offset, and the server
        // reads a naive timestamp as UTC — the picked wall time must
        // survive the round trip in any browser timezone.
        'at': _at!.toUtc().toIso8601String(),
    });
    final auth = context.read<AuthService>();
    final resp = await auth.authPost(
      '/api/v1/server/schedule',
      body: body,
    );
    if (!mounted) return;
    if (resp.statusCode == 200) {
      Navigator.pop(context, true);
      return;
    }
    String detail = 'Failed to schedule (${resp.statusCode})';
    try {
      detail = jsonDecode(resp.body)['detail'] ?? detail;
    } catch (_) {}
    setState(() {
      _error = detail;
      _submitting = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    final labelStyle = const TextStyle(
      color: KColors.textPrimary,
      fontWeight: FontWeight.bold,
    );
    final validation = _validationError;
    return AlertDialog(
      title: Text(
        'Schedule Server Action',
        style: TextStyle(color: KColors.textPrimary),
      ),
      content: SizedBox(
        width: 420,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('Action', style: labelStyle),
            const SizedBox(height: 4),
            SegmentedButton<String>(
              segments: const [
                ButtonSegment(
                  value: 'stop',
                  label: Text('Stop'),
                  icon: Icon(Icons.power_settings_new),
                ),
                ButtonSegment(
                  value: 'recycle',
                  label: Text('Recycle'),
                  icon: Icon(Icons.autorenew),
                ),
              ],
              selected: {_action},
              onSelectionChanged: (sel) => setState(() => _action = sel.first),
            ),
            const SizedBox(height: 4),
            Text(
              _action == 'stop'
                  ? 'Stop: graceful shutdown, then the process exits.'
                  : 'Recycle: graceful in-place restart; never exits.',
              style: const TextStyle(
                color: KColors.textSecondary,
                fontSize: 12,
              ),
            ),
            const SizedBox(height: 16),
            Text('When', style: labelStyle),
            const SizedBox(height: 4),
            SegmentedButton<bool>(
              segments: const [
                ButtonSegment(value: true, label: Text('After a delay')),
                ButtonSegment(value: false, label: Text('At a time')),
              ],
              selected: {_useDelay},
              onSelectionChanged: (sel) =>
                  setState(() => _useDelay = sel.first),
            ),
            const SizedBox(height: 12),
            if (_useDelay)
              TextField(
                controller: _delayController,
                decoration: InputDecoration(
                  labelText: 'Delay',
                  labelStyle: labelStyle,
                  floatingLabelStyle: labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
                  border: const OutlineInputBorder(),
                  hintText: 'e.g. 2h, 90m, 45s, or 2h 30m',
                  helperText: 'A bare number means minutes',
                  errorText: validation,
                ),
                autofocus: true,
                onChanged: (_) => setState(() {}),
              )
            else ...[
              OutlinedButton.icon(
                onPressed: _pickDateTime,
                icon: const Icon(Icons.event),
                label: Text(_atLabel),
              ),
              if (validation != null)
                Padding(
                  padding: const EdgeInsets.only(top: 8),
                  child: Text(
                    validation,
                    style: TextStyle(color: KColors.accentRed, fontSize: 12),
                  ),
                ),
            ],
            const SizedBox(height: 8),
            if (_firesLabel != null)
              Text(
                _firesLabel!,
                style: const TextStyle(
                  color: KColors.textSecondary,
                  fontSize: 12,
                ),
              ),
            if (_error != null) ...[
              const SizedBox(height: 8),
              Text(
                _error!,
                style: TextStyle(color: KColors.accentRed, fontSize: 12),
              ),
            ],
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
          child: const Text('Cancel'),
        ),
        FilledButton(
          onPressed: _formValid && !_submitting ? _submit : null,
          child: _submitting
              ? const SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2),
                )
              : const Text('Schedule'),
        ),
      ],
    );
  }
}
