// coverage:ignore-file
/// Admin → Events tab → All subtab: the time-correlated merged audit
/// stream (#3251).
///
/// Reads `GET /api/v1/events` — one newest-first stream merged across
/// the three audit tables (`audit_events`, `container_events`,
/// `egress_consent`), each row naming its origin in `source` and
/// embedding the full origin row in `data`. This is the SV-222439
/// replay view: a login, a workspace start, and an egress-consent
/// decision read as one interleaved sequence. Filters: event, actor
/// (id or email), workspace (id or name) — the same substring filters
/// the sibling panels use; the time window is an API-only filter.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../auth/auth_service.dart';
import '../theme/colors.dart';

/// The All subtab body: event/actor/workspace filter fields, paging
/// controls, and the merged history table. Each row expands in place
/// to the read-only detail view (the origin row's full JSON).
class AllEventsPanel extends StatefulWidget {
  const AllEventsPanel({super.key});

  @override
  State<AllEventsPanel> createState() => _AllEventsPanelState();
}

class _AllEventsPanelState extends State<AllEventsPanel> {
  static const _pageSize = 50;

  List<Map<String, dynamic>> _events = [];
  int _total = 0;
  int _offset = 0;
  bool _loading = true;
  String? _error;

  String _eventQuery = '';
  String _actorQuery = '';
  String _workspaceQuery = '';

  /// Row key whose detail area is expanded (null = none). The key is
  /// `source:id` — ids are only unique within one origin table.
  String? _expandedKey;

  /// Monotonic load sequence: a response from a superseded load (a
  /// newer filter/page request raced it) must not overwrite the table.
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
      // A fresh page or filter result invalidates any expansion.
      _expandedKey = null;
    });
    try {
      final query = <String, String>{
        'limit': '$_pageSize',
        'offset': '$offset',
        if (_eventQuery.isNotEmpty) 'event': _eventQuery,
        if (_actorQuery.isNotEmpty) 'actor': _actorQuery,
        if (_workspaceQuery.isNotEmpty) 'workspace': _workspaceQuery,
      };
      final auth = context.read<AuthService>();
      final resp = await auth.authGet('/api/v1/events?${_encodeQuery(query)}');
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
          _error = 'Failed to load events (${resp.statusCode})';
          _loading = false;
        });
      }
    } catch (e) {
      debugPrint('[AllEventsPanel] load failed: $e');
      if (mounted && seq == _seq) {
        setState(() {
          _error = 'Could not load events. Please try again.';
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

  /// The row's actor label: email when one is known (denormalized on
  /// audit rows, resolved by the backend elsewhere), the raw id
  /// otherwise, 'anonymous' when nobody acted (a pending consent
  /// request, an unauthenticated login.failed).
  String _actorLabel(Map<String, dynamic> row) {
    final id = row['actor_id'] as String?;
    final email = row['actor_email'] as String?;
    if (email != null && email.isNotEmpty) return email;
    if (id != null && id.isNotEmpty) return id;
    return 'anonymous';
  }

  /// Workspace name when the backend resolved one, the raw id
  /// otherwise (a deleted workspace still has history worth reading).
  String _workspaceLabel(Map<String, dynamic> row) {
    final name = row['workspace_name'] as String?;
    if (name != null && name.isNotEmpty) return name;
    return row['workspace_id'] as String? ?? '';
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
                  fieldKey: const ValueKey('all-event-filter'),
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
                  fieldKey: const ValueKey('all-actor-filter'),
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
                  fieldKey: const ValueKey('all-workspace-filter'),
                  label: 'Filter by workspace id or name',
                  onChanged: (value) {
                    _workspaceQuery = value;
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
  /// every data row (the #3006 pattern the sibling panels use). Long
  /// cell values ellipsize; the tooltip carries the full text, and the
  /// row's expanded detail view repeats everything in full.
  static const _columns = <(String, int)>[
    ('When', 3),
    ('Source', 2),
    ('Event', 2),
    ('Actor', 3),
    ('Workspace', 3),
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
      return const Center(child: Text('No events recorded'));
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
    final key = '${row['source']}:${row['id']}';
    final expanded = _expandedKey == key;
    // Cells built in _columns order; the flex weights below always come
    // from the same _columns iteration, so a column reorder cannot
    // relabel the header while leaving the old weight on a cell.
    final cells = <Widget>[
      _textCell(_timeLabel(row['created_at'])),
      _sourceChip(row['source'] as String? ?? ''),
      _eventChip(row['event'] as String? ?? ''),
      _textCell(_actorLabel(row)),
      _textCell(_workspaceLabel(row)),
    ];
    return Container(
      decoration: const BoxDecoration(
        border: Border(bottom: BorderSide(color: KColors.borderMuted)),
      ),
      child: Column(
        children: [
          InkWell(
            onTap: () => setState(() => _expandedKey = expanded ? null : key),
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

  /// The expanded, read-only per-row detail view: the origin row's
  /// full JSON (`data` — every source-specific field, never secrets)
  /// plus the resolved correlation fields.
  Widget _detailArea(Map<String, dynamic> row) {
    final data = row['data'];
    // An empty map is a row pruned between the backend's union read
    // and its detail fetch — nothing left to show.
    final dataJson = data == null || (data is Map && data.isEmpty)
        ? '—'
        : const JsonEncoder.withIndent('  ').convert(data);
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
      key: const ValueKey('all-event-detail'),
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
      color: KColors.bgSurface,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          field('Source', row['source'] as String? ?? '—'),
          field('Actor', _actorLabel(row)),
          field('Workspace', _workspaceLabel(row)),
          field('Data', dataJson),
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

  /// Source badge: one color per origin table — identity/privilege
  /// rows blue, container lifecycle green, egress consent amber.
  Widget _sourceChip(String source) {
    final color = switch (source) {
      'audit' => KColors.accentBlue,
      'container' => KColors.accentGreen,
      _ => KColors.accentAmber,
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: color,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(
        source,
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: const TextStyle(color: Colors.white, fontSize: 12),
      ),
    );
  }

  /// Failure/destruction events read red (login.failed, *.delete,
  /// *.remove, *.revoke/.revoked, egress.denied/expired), everything
  /// else green — the binary chip coloring of the sibling panels.
  Widget _eventChip(String event) {
    final negative = event.endsWith('.failed') ||
        event.endsWith('.delete') ||
        event.endsWith('.remove') ||
        event.endsWith('.revoke') ||
        event.endsWith('.revoked') ||
        event.endsWith('.denied') ||
        event.endsWith('.expired');
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

/// A debounced filter field (the shared pattern of the events
/// panels): reports trimmed changes to [onChanged] once typing
/// pauses, keeping the request burst down.
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
