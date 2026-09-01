// coverage:ignore-file
/// Admin → Events tab: paged container start/stop history (#2923).
///
/// Reads the `container_events` audit table (#2915) through
/// `GET /api/v1/events` — newest first, optional
/// workspace-id filter, offset-based paging. The tab itself is gated on
/// the dedicated `manage-events` permission over
/// `/admin/container-events` (see [AdminUsersPage]); admins hold it via
/// the `/admin` wildcard, other principals only via an explicit grant.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:provider/provider.dart';

import '../auth/auth_service.dart';
import '../theme/colors.dart';
import '../utils/system_agent.dart';

/// The Admin → Events tab body: filter field, paging controls, and the
/// history table.
class ContainerEventsPanel extends StatefulWidget {
  const ContainerEventsPanel({super.key});

  @override
  State<ContainerEventsPanel> createState() => _ContainerEventsPanelState();
}

class _ContainerEventsPanelState extends State<ContainerEventsPanel> {
  static const _pageSize = 50;

  List<Map<String, dynamic>> _events = [];
  int _total = 0;
  int _offset = 0;
  bool _loading = true;
  String? _error;

  final _workspaceController = TextEditingController();
  String _workspaceQuery = '';
  Timer? _queryDebounce;

  @override
  void initState() {
    super.initState();
    _workspaceController.addListener(() {
      final value = _workspaceController.text.trim();
      if (value == _workspaceQuery) return;
      _workspaceQuery = value;
      _queryDebounce?.cancel();
      _queryDebounce = Timer(
        const Duration(milliseconds: 300),
        () => _load(offset: 0),
      );
    });
    _load();
  }

  @override
  void dispose() {
    _queryDebounce?.cancel();
    _workspaceController.dispose();
    super.dispose();
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
    setState(() {
      _loading = true;
      _error = null;
      _offset = offset;
    });
    try {
      final query = <String, String>{
        'limit': '$_pageSize',
        'offset': '$offset',
        if (_workspaceQuery.isNotEmpty) 'workspace_id': _workspaceQuery,
      };
      final auth = context.read<AuthService>();
      final resp = await auth.authGet(
        '/api/v1/events?${_encodeQuery(query)}',
      );
      if (!mounted) return;
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
      debugPrint('[ContainerEventsPanel] load failed: $e');
      if (mounted) {
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

  /// 'user someone@example.com' / 'system agent' / 'system' — email when
  /// the backend resolved one, a friendly label for the fixed agent
  /// identity, raw id otherwise (a purged user).
  String _actorLabel(Map<String, dynamic> row) {
    final type = row['actor_type'] as String? ?? 'system';
    final id = row['actor_id'] as String?;
    if (id != null && id == agentUserId) return 'system agent';
    final email = row['actor_email'] as String?;
    if (email != null && email.isNotEmpty) return '$type $email';
    if (id != null && id.isNotEmpty) return '$type $id';
    return type;
  }

  String _containerLabel(Map<String, dynamic> row) {
    final id = row['container_id'] as String?;
    if (id == null || id.isEmpty) return '';
    final role = row['container_role'] as String?;
    return role == null || role == 'workspace' ? id : '$id ($role)';
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
                child: TextField(
                  key: const ValueKey('events-workspace-filter'),
                  controller: _workspaceController,
                  decoration: const InputDecoration(
                    isDense: true,
                    labelText: 'Filter by workspace id',
                    border: OutlineInputBorder(),
                    prefixIcon: Icon(Icons.filter_list),
                  ),
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
      return const Center(child: Text('No container events recorded'));
    }
    return ScrollConfiguration(
      behavior: ScrollConfiguration.of(context).copyWith(scrollbars: true),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        child: SingleChildScrollView(
          child: DataTable(
            headingTextStyle: const TextStyle(
              color: KColors.textSecondary,
              fontWeight: FontWeight.bold,
            ),
            columns: const [
              DataColumn(label: Text('When')),
              DataColumn(label: Text('Workspace')),
              DataColumn(label: Text('Event')),
              DataColumn(label: Text('Actor')),
              DataColumn(label: Text('Cause')),
              DataColumn(label: Text('Container')),
              DataColumn(label: Text('Network ns')),
            ],
            rows: [
              for (final row in _events)
                DataRow(
                  cells: [
                    DataCell(Text(_timeLabel(row['created_at']))),
                    DataCell(Text(_workspaceLabel(row))),
                    DataCell(_eventChip(row['event'] as String? ?? '')),
                    DataCell(Text(_actorLabel(row))),
                    DataCell(Text(row['cause'] as String? ?? '')),
                    DataCell(Text(_containerLabel(row))),
                    DataCell(Text(row['network_namespace'] as String? ?? '')),
                  ],
                ),
            ],
          ),
        ),
      ),
    );
  }

  /// Workspace name when the backend resolved one, raw id otherwise (a
  /// deleted workspace still has history worth reading).
  String _workspaceLabel(Map<String, dynamic> row) {
    final name = row['workspace_name'] as String?;
    if (name != null && name.isNotEmpty) return name;
    return row['workspace_id'] as String? ?? '';
  }

  Widget _eventChip(String event) {
    final isStart = event == 'start';
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: isStart ? KColors.accentGreen : KColors.accentRed,
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text(
        event,
        style: const TextStyle(color: Colors.white, fontSize: 12),
      ),
    );
  }
}
