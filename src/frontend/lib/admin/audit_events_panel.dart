// coverage:ignore-file
/// Admin → Events tab → Audit subtab: paged identity/privilege audit
/// history (#3217).
///
/// Reads the `audit_events` table (#3205) through
/// `GET /api/v1/events/audit` — newest first, optional `event` /
/// `actor` / `target` substring filters, offset-based paging. Mirrors
/// [ContainerEventsPanel] (same paged envelope, same filter-field +
/// paging layout). The subtab rides the Events tab's `manage-events`
/// gate (see [AdminUsersPage]); the `/events` ACL resource governs
/// both streams, so no separate permission wiring exists. The
/// data-level file events (#3257) — `file.download`, `file.upload`,
/// `file.write`, `file.delete` — render here like any other row; their
/// detail expansion shows the file icon and the audited path.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../auth/auth_service.dart';
import '../theme/colors.dart';

/// The Audit subtab body: event/actor/target filter fields, paging
/// controls, and the history table. Each row expands in place to the
/// read-only detail view (detail JSON, full source IP, full user
/// agent) for incident correlation.
class AuditEventsPanel extends StatefulWidget {
  const AuditEventsPanel({super.key});

  @override
  State<AuditEventsPanel> createState() => _AuditEventsPanelState();
}

class _AuditEventsPanelState extends State<AuditEventsPanel> {
  static const _pageSize = 50;

  List<Map<String, dynamic>> _events = [];
  int _total = 0;
  int _offset = 0;
  bool _loading = true;
  String? _error;

  String _eventQuery = '';
  String _actorQuery = '';
  String _targetQuery = '';

  /// Row id whose detail area is expanded (null = none).
  int? _expandedId;

  /// Monotonic load sequence: a response from a superseded load (a
  /// newer filter/page request raced it) must not overwrite the table
  /// — the three independent filter debouncers make overlapping
  /// requests the common case, not the exception.
  int _seq = 0;

  @override
  void initState() {
    super.initState();
    _load();
  }

  /// URL-encode a query param map (sorted for stable, cacheable URLs).
  static String _encodeQuery(Map<String, String> params) {
    final pairs = <String>[];
    for (final key in params.keys.toList()..sort()) {
      pairs.add(
        '${Uri.encodeQueryComponent(key)}='
        '${Uri.encodeQueryComponent(params[key]!)}',
      );
    }
    return pairs.join('&');
  }

  Future<void> _load({int offset = 0}) async {
    final seq = ++_seq;
    setState(() {
      _loading = true;
      _error = null;
      _offset = offset;
      // A fresh page or filter result invalidates any expansion —
      // ids are unique, but a row resurfacing pages later pre-opened
      // would be surprising.
      _expandedId = null;
    });
    try {
      final query = <String, String>{
        'limit': '$_pageSize',
        'offset': '$offset',
        if (_eventQuery.isNotEmpty) 'event': _eventQuery,
        if (_actorQuery.isNotEmpty) 'actor': _actorQuery,
        if (_targetQuery.isNotEmpty) 'target': _targetQuery,
      };
      final auth = context.read<AuthService>();
      final resp =
          await auth.authGet('/api/v1/events/audit?${_encodeQuery(query)}');
      if (!mounted || seq != _seq) return;
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as Map<String, dynamic>;
        setState(() {
          _events = (data['items'] as List).cast<Map<String, dynamic>>();
          _total = (data['total'] as num).toInt();
          _loading = false;
        });
      } else {
        setState(() {
          _error = 'Failed to load audit events (${resp.statusCode})';
          _loading = false;
        });
      }
    } catch (e) {
      debugPrint('[AuditEventsPanel] load failed: $e');
      if (mounted && seq == _seq) {
        setState(() {
          _error = 'Could not load audit events. Please try again.';
          _loading = false;
        });
      }
    }
  }

  bool get _canPrev => _offset > 0;
  bool get _canNext => _offset + _events.length < _total;

  /// '12–61 of 214' — the slice currently on screen.
  String get _rangeLabel {
    if (_total == 0) return '0 events';
    final first = _offset + 1;
    final last = _offset + _events.length;
    return '$first–$last of $_total';
  }

  /// Render an epoch-seconds `created_at` as a local 'MMM d, HH:MM' label.
  String _timeLabel(dynamic createdAt) {
    final secs = (createdAt as num?)?.toDouble();
    if (secs == null) return '';
    final dt = DateTime.fromMillisecondsSinceEpoch((secs * 1000).round());
    final localizations = MaterialLocalizations.of(context);
    return '${localizations.formatMediumDate(dt)} '
        '${localizations.formatTimeOfDay(TimeOfDay.fromDateTime(dt))}';
  }

  /// Actor email when the backend denormalized one at write time, the
  /// raw id otherwise (attribution survives the actor's own deletion,
  /// #3205). A missing actor is an unauthenticated row (login.failed,
  /// user.register) — labeled 'anonymous', not 'system': nobody
  /// authenticated performed it.
  String _actorLabel(Map<String, dynamic> row) {
    final id = row['actor_id'] as String?;
    final email = row['actor_email'] as String?;
    if (email != null && email.isNotEmpty) return email;
    if (id != null && id.isNotEmpty) return id;
    return 'anonymous';
  }

  /// 'user 2f9a…' / 'group g1' / '' when the event has no target.
  String _targetLabel(Map<String, dynamic> row) {
    final id = row['target_id'] as String?;
    final type = row['target_type'] as String?;
    if (id == null || id.isEmpty) return '';
    if (type == null || type.isEmpty) return id;
    return '$type $id';
  }

  @override
  Widget build(BuildContext context) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
          child: Row(
            children: [
              Expanded(
                child: _FilterField(
                  fieldKey: const ValueKey('audit-event-filter'),
                  label: 'Filter by event name',
                  onChanged: (value) {
                    _eventQuery = value;
                    _load(offset: 0);
                  },
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: _FilterField(
                  fieldKey: const ValueKey('audit-actor-filter'),
                  label: 'Filter by actor id or email',
                  onChanged: (value) {
                    _actorQuery = value;
                    _load(offset: 0);
                  },
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: _FilterField(
                  fieldKey: const ValueKey('audit-target-filter'),
                  label: 'Filter by target id',
                  onChanged: (value) {
                    _targetQuery = value;
                    _load(offset: 0);
                  },
                ),
              ),
              const SizedBox(width: 8),
              IconButton(
                icon: const Icon(Icons.refresh),
                tooltip: 'Refresh',
                onPressed: () => _load(offset: _offset),
              ),
            ],
          ),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  _rangeLabel,
                  style: const TextStyle(color: KColors.textSecondary),
                ),
              ),
              IconButton(
                icon: const Icon(Icons.chevron_left),
                tooltip: 'Previous page',
                onPressed: _canPrev
                    ? () => _load(
                          offset:
                              _offset - _pageSize < 0 ? 0 : _offset - _pageSize,
                        )
                    : null,
              ),
              IconButton(
                icon: const Icon(Icons.chevron_right),
                tooltip: 'Next page',
                onPressed: _canNext
                    ? () => _load(offset: _offset + _events.length)
                    : null,
              ),
            ],
          ),
        ),
        Expanded(child: _buildBody()),
      ],
    );
  }

  /// Column labels with the flex weights shared by the header row and
  /// every data row: aligned columns that always share the panel width
  /// (the #3006 container-table pattern). Long cell values ellipsize;
  /// the tooltip carries the full text, and the row's expanded detail
  /// view repeats source IP / user agent in full.
  static const _columns = <(String, int)>[
    ('When', 3),
    ('Event', 2),
    ('Actor', 3),
    ('Target', 3),
    ('Source IP', 2),
    ('User agent', 4),
  ];

  Widget _buildBody() {
    if (_loading && _events.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_error != null && _events.isEmpty) {
      return Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(_error!),
            const SizedBox(height: 8),
            OutlinedButton(
              onPressed: () => _load(offset: 0),
              child: const Text('Retry'),
            ),
          ],
        ),
      );
    }
    if (_events.isEmpty) {
      return const Center(child: Text('No audit events recorded'));
    }
    return Column(
      children: [
        Container(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
          decoration: const BoxDecoration(
            border: Border(bottom: BorderSide(color: KColors.borderDefault)),
          ),
          child: Row(
            children: [
              for (final (label, flex) in _columns)
                Expanded(
                  flex: flex,
                  child: Text(
                    label,
                    style: const TextStyle(
                      color: KColors.textSecondary,
                      fontWeight: FontWeight.bold,
                      fontSize: 12,
                    ),
                  ),
                ),
            ],
          ),
        ),
        Expanded(
          child: ListView.builder(
            itemCount: _events.length,
            itemBuilder: (ctx, i) => _eventRow(_events[i]),
          ),
        ),
      ],
    );
  }

  Widget _eventRow(Map<String, dynamic> row) {
    final expanded = _expandedId == row['id'];
    // Cells built in _columns order; the flex weights below always come
    // from the same _columns iteration, so a column reorder cannot
    // relabel the header while leaving the old weight on a cell.
    final cells = <Widget>[
      _textCell(_timeLabel(row['created_at'])),
      _eventChip(row['event'] as String? ?? ''),
      _textCell(_actorLabel(row)),
      _textCell(_targetLabel(row)),
      _textCell(row['source_ip'] as String? ?? ''),
      _textCell(row['user_agent'] as String? ?? ''),
    ];
    return Container(
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: KColors.borderMuted)),
      ),
      child: Column(
        children: [
          InkWell(
            onTap: () => setState(
              () => _expandedId = expanded ? null : row['id'] as int?,
            ),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
              child: Row(
                children: [
                  for (final (i, (_, flex)) in _columns.indexed)
                    Expanded(flex: flex, child: cells[i]),
                ],
              ),
            ),
          ),
          if (expanded) _detailArea(row),
        ],
      ),
    );
  }

  /// The expanded, read-only per-row detail view: the action-specific
  /// `detail` blob (pretty-printed JSON, never secrets — #3205) plus
  /// the full correlation fields the row columns ellipsize. File
  /// events (#3257) carry a `path` in their detail — rendered as a
  /// file-icon row above the raw JSON so the reviewed path reads at
  /// a glance.
  Widget _detailArea(Map<String, dynamic> row) {
    final detail = row['detail'];
    final detailJson = detail == null
        ? '—'
        : const JsonEncoder.withIndent('  ').convert(detail);
    // File events only (#3257): the icon row keys on the event kind,
    // not the presence of a `path` detail key — `login.failed` rows
    // carry `path: "resend-verification"` naming the auth flow, not
    // a file (#3205/#2618), and must not render as a file path.
    final isFileEvent = (row['event'] as String? ?? '').startsWith('file.');
    final filePath = isFileEvent && detail is Map && detail['path'] is String
        ? detail['path'] as String
        : null;
    Widget field(String label, String value) {
      return Padding(
        padding: const EdgeInsets.only(bottom: 4),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(
              width: 90,
              child: Text(
                label,
                style: const TextStyle(
                  color: KColors.textSecondary,
                  fontSize: 12,
                ),
              ),
            ),
            Expanded(
              child: SelectableText(
                value,
                style: const TextStyle(fontSize: 12),
              ),
            ),
          ],
        ),
      );
    }

    return Container(
      key: const ValueKey('audit-event-detail'),
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
      color: KColors.bgSurface,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (filePath != null)
            Padding(
              key: const ValueKey('audit-event-path'),
              padding: const EdgeInsets.only(bottom: 4),
              child: Row(
                children: [
                  const Icon(
                    Icons.insert_drive_file,
                    size: 16,
                    color: KColors.textSecondary,
                  ),
                  const SizedBox(width: 6),
                  Expanded(
                    child: SelectableText(
                      filePath,
                      style: const TextStyle(fontSize: 12),
                    ),
                  ),
                ],
              ),
            ),
          field('Source IP', row['source_ip'] as String? ?? '—'),
          field('User agent', row['user_agent'] as String? ?? '—'),
          field('Detail', detailJson),
        ],
      ),
    );
  }

  /// One ellipsized table cell; the tooltip keeps the full value
  /// reachable when the flex share is too narrow to show it.
  Widget _textCell(String text) {
    return Tooltip(
      message: text,
      child: Text(text, maxLines: 1, overflow: TextOverflow.ellipsis),
    );
  }

  /// Failure/destruction events read red (login.failed, user.delete,
  /// group.member.remove, session.revoke, …), everything else green —
  /// the binary chip coloring of the container table.
  Widget _eventChip(String event) {
    final negative = event.endsWith('.failed') ||
        event.endsWith('.delete') ||
        event.endsWith('.remove') ||
        event.endsWith('.revoke');
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: negative ? KColors.accentRed : KColors.accentGreen,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(
        event,
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: const TextStyle(color: Colors.white, fontSize: 12),
      ),
    );
  }
}

/// A debounced filter field (the workspace-filter pattern of the
/// container events panel): reports trimmed changes to [onChanged]
/// once typing pauses, keeping the request burst down.
class _FilterField extends StatefulWidget {
  const _FilterField({
    required this.fieldKey,
    required this.label,
    required this.onChanged,
  });

  final Key fieldKey;
  final String label;
  final ValueChanged<String> onChanged;

  @override
  State<_FilterField> createState() => _FilterFieldState();
}

class _FilterFieldState extends State<_FilterField> {
  final _controller = TextEditingController();
  Timer? _debounce;
  String _lastReported = '';

  @override
  void initState() {
    super.initState();
    _controller.addListener(() {
      final value = _controller.text.trim();
      if (value == _lastReported) return;
      _lastReported = value;
      _debounce?.cancel();
      _debounce = Timer(
        const Duration(milliseconds: 300),
        () => widget.onChanged(value),
      );
    });
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return TextField(
      key: widget.fieldKey,
      controller: _controller,
      decoration: InputDecoration(
        isDense: true,
        labelText: widget.label,
        border: const OutlineInputBorder(),
        prefixIcon: const Icon(Icons.filter_list),
      ),
    );
  }
}
