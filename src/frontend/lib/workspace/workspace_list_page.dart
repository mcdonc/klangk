import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import '../theme/colors.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../ws/ws_client.dart';
import '../utils/page_title.dart';
import '../widgets/app_bar_actions.dart';
import '../widgets/app_bar_title.dart';
import 'create_workspace_dialog.dart';
import 'import_workspace_dialog.dart';

const _validMountOptions = {
  'ro',
  'rw',
  'z',
  'Z',
  'nocopy',
  'consistent',
  'cached',
  'delegated',
};

String? validateMountSpec(String spec) {
  final parts = spec.split(':');
  if (parts.length < 2 || parts.length > 3) {
    return 'Expected source:dest or source:dest:options';
  }
  if (parts[0].isEmpty) {
    return 'Source is empty';
  }
  if (!parts[1].startsWith('/')) {
    return 'Container path must be absolute (start with /)';
  }
  if (parts.length == 3) {
    for (final opt in parts[2].split(',')) {
      if (opt.isNotEmpty && !_validMountOptions.contains(opt)) {
        return 'Unknown option: $opt';
      }
    }
  }
  return null;
}

class WorkspaceListPage extends StatefulWidget {
  const WorkspaceListPage({super.key}); // coverage:ignore-line

  @override
  State<WorkspaceListPage> createState() => _WorkspaceListPageState();
}

/// Mutable pagination state for one list section (owned or shared).
/// Held as a field so owned/shared advance independently — each has its
/// own cursor and its own "load more" control.
class _Section {
  _Section({this.isShared = false});

  final bool isShared;
  List<Map<String, dynamic>> workspaces = [];
  bool hasMore = false;
  int? nextOffset;
  bool loadingMore = false;

  // Independent sort/filter per section. Defaults: created, descending,
  // no filter.
  String sort = 'created';
  String order = 'desc';
  String query = '';
  Timer? queryDebounce;
  final TextEditingController searchController = TextEditingController();

  /// API path for this section, paginated at [offset] with this section's
  /// sort/order/filter query params.
  String path(int offset) {
    final base = isShared ? '/api/v1/workspaces/shared' : '/api/v1/workspaces';
    var p = '$base?limit=${_WorkspaceListPageState._pageSize}&offset=$offset'
        '&sort=$sort&order=$order';
    if (query.isNotEmpty) p += '&q=${Uri.encodeQueryComponent(query)}';
    return p;
  }

  void dispose() {
    queryDebounce?.cancel();
    searchController.dispose();
  }
}

class _WorkspaceListPageState extends State<WorkspaceListPage> {
  static const int _pageSize = 5;

  /// Per-section pagination state. Owned and shared lists come from
  /// different queries with independent cursors, so each section tracks
  /// its own list + has_more + next_offset + loading-more flag.
  final _Section _owned = _Section();
  final _Section _shared = _Section(isShared: true);
  Map<String, List<Map<String, dynamic>>> _workspaceMembers = {};
  bool _loading = true;
  StreamSubscription<void>? _workspacesChangedSub;
  StreamSubscription<Map<String, dynamic>>? _containerStatusSub;
  StreamSubscription<Map<String, dynamic>>? _serviceHealthSub;

  @override
  void initState() {
    super.initState();
    setPageTitle('Workspaces');
    _loadWorkspaces();
    final wsClient = context.read<WsClient>();
    _workspacesChangedSub = wsClient.workspacesChanged.listen((_) {
      _refreshWorkspaces();
    });
    _containerStatusSub = wsClient.containerStatus.listen(_onContainerStatus);
    _serviceHealthSub = wsClient.serviceHealth.listen(_onServiceHealth);
  }

  void _onContainerStatus(Map<String, dynamic> msg) {
    if (!mounted) return;
    final wsId = msg['workspace_id'] as String?;
    final running = msg['running'] as bool? ?? false;
    if (wsId == null) return;
    setState(() {
      for (final section in [_owned, _shared]) {
        for (final ws in section.workspaces) {
          if (ws['id'] == wsId) {
            ws['running'] = running;
            // A stopped container has no health status.
            if (!running) ws['health'] = null;
          }
        }
      }
    });
  }

  void _onServiceHealth(Map<String, dynamic> msg) {
    if (!mounted) return;
    final wsId = msg['workspace_id'] as String?;
    if (wsId == null) return;
    // ``running`` (added in #1175 item 2) distinguishes a live
    // container's check result from a terminal container-death frame:
    // a death carries running=false (and healthy=false).  Defaults to
    // true for older servers that don't send the field, and mirrors
    // _onContainerStatus so both paths render a dead container grey.
    final running = msg['running'] as bool? ?? true;
    final healthy = msg['healthy'] as bool? ?? false;
    setState(() {
      for (final section in [_owned, _shared]) {
        for (final ws in section.workspaces) {
          if (ws['id'] == wsId) {
            ws['running'] = running;
            // A stopped container has no health status; a running one
            // reflects the latest check.
            ws['health'] = running ? (healthy ? 'healthy' : 'unhealthy') : null;
          }
        }
      }
    });
  }

  @override
  void dispose() {
    _workspacesChangedSub?.cancel();
    _containerStatusSub?.cancel();
    _serviceHealthSub?.cancel();
    _owned.dispose();
    _shared.dispose();
    super.dispose();
  }

  AuthService get _auth => context.read<AuthService>();

  /// Parse a paginated envelope response into its items + cursors.
  static ({List<Map<String, dynamic>> items, bool hasMore, int? nextOffset})
      _parseEnvelope(String body) {
    final json = jsonDecode(body) as Map<String, dynamic>;
    final items = (json['items'] as List).cast<Map<String, dynamic>>();
    return (
      items: items,
      hasMore: json['has_more'] == true,
      nextOffset:
          json['next_offset'] is int ? json['next_offset'] as int : null,
    );
  }

  /// Fetch one page for [section] at [offset]. Returns the parsed page or
  /// null on failure. Caller handles members + state.
  Future<({List<Map<String, dynamic>> items, bool hasMore, int? nextOffset})?>
      _fetchPage(_Section section, int offset) async {
    try {
      final resp = await _auth.authGet(section.path(offset));
      if (resp.statusCode != 200) return null;
      return _parseEnvelope(resp.body);
    } catch (_) {
      return null;
    } // coverage:ignore-line
  }

  /// Fetch members for the given workspaces only (bounded to a page so
  /// we never fan out N+1 across the whole list). Merges into the cache.
  Future<void> _fetchMembers(List<Map<String, dynamic>> workspaces) async {
    final members = <String, List<Map<String, dynamic>>>{};
    await Future.wait(
      workspaces.map((ws) async {
        final id = ws['id'] as String;
        try {
          final resp = await _auth.authGet('/api/v1/workspaces/$id/members');
          if (resp.statusCode == 200) {
            members[id] = List<Map<String, dynamic>>.from(
              jsonDecode(resp.body) as List,
            );
          }
        } catch (_) {} // coverage:ignore-line
      }),
    );
    if (mounted) {
      setState(() {
        _workspaceMembers = {..._workspaceMembers, ...members};
      });
    }
  }

  /// Replace a section with its first page. Returns true on success.
  Future<bool> _loadFirstPage(_Section section) async {
    final page = await _fetchPage(section, 0);
    if (page == null) return false;
    await _fetchMembers(page.items);
    if (!mounted) return false;
    setState(() {
      section.workspaces = page.items;
      section.hasMore = page.hasMore;
      section.nextOffset = page.nextOffset;
    });
    return true;
  }

  /// Silent refresh: re-fetch the first page of each section without
  /// showing the loading spinner or error snackbars. Driven by
  /// `workspaces_changed` WS events — the set changed, so reset each
  /// section to its first page rather than refetching every loaded page.
  Future<void> _refreshWorkspaces() async {
    await _loadFirstPage(_owned);
    if (!mounted) return;
    await _loadFirstPage(_shared);
  }

  Future<void> _loadWorkspaces() async {
    setState(() => _loading = true);
    try {
      final ownedOk = await _loadFirstPage(_owned);
      if (!ownedOk) {
        throw Exception('owned workspaces load failed');
      }
      if (!mounted) return;
      try {
        await _loadFirstPage(_shared);
      } catch (e) {
        // coverage:ignore-start
        debugPrint('[WorkspaceListPage] fetch shared workspaces failed: $e');
      } // coverage:ignore-end
    } catch (e) {
      debugPrint('Failed to load workspaces: $e');
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            duration: Duration(days: 1),
            showCloseIcon: true,
            content: Text('Failed to load workspaces'),
          ),
        );
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  /// Append the next page of [section] (owned or shared). No-op when
  /// already loading, when there's no next page, or on fetch failure.
  Future<void> _loadMore(_Section section) async {
    if (section.loadingMore || !section.hasMore || section.nextOffset == null) {
      return;
    }
    setState(() => section.loadingMore = true);
    try {
      final page = await _fetchPage(section, section.nextOffset!);
      if (page != null) {
        await _fetchMembers(page.items);
        if (mounted) {
          setState(() {
            section.workspaces = [...section.workspaces, ...page.items];
            section.hasMore = page.hasMore;
            section.nextOffset = page.nextOffset;
          });
        }
      }
    } catch (_) {
    } // coverage:ignore-line
    finally {
      if (mounted) setState(() => section.loadingMore = false);
    }
  }

  /// Change sort column/direction for [section]. Resets that section to
  /// page 1 because a different sort reorders every row.
  Future<void> _changeSort(_Section section, String sort) async {
    section.queryDebounce?.cancel();
    if (section.sort == sort) {
      // Same column -> toggle direction.
      setState(() => section.order = section.order == 'asc' ? 'desc' : 'asc');
    } else {
      setState(() {
        section.sort = sort;
        section.order = sort == 'name' ? 'asc' : 'desc';
      });
    }
    await _loadFirstPage(section);
  }

  /// Debounced name-filter handler for [section]. Resets to page 1 on change.
  void _onQueryChanged(_Section section, String value) {
    setState(() => section.query = value);
    section.queryDebounce?.cancel();
    section.queryDebounce = Timer(const Duration(milliseconds: 300), () {
      _loadFirstPage(section);
    });
  }

  Future<Map<String, dynamic>?> _fetchImages() async {
    try {
      final response = await _auth.authGet('/api/v1/images');
      if (response.statusCode == 200) {
        return jsonDecode(response.body) as Map<String, dynamic>;
      }
    } catch (e) {
      // coverage:ignore-start
      debugPrint('[WorkspaceListPage] fetch images failed: $e');
    } // coverage:ignore-end
    return null;
  }

  Future<void> _createWorkspace() async {
    final imageData = await _fetchImages();
    final defaultImage = imageData?['default'] as String? ?? 'klangk-pi';
    final allowedImages =
        (imageData?['allowed'] as List?)?.cast<String>() ?? [defaultImage];

    if (!mounted) return;

    final created = await showDialog<bool>(
      context: context,
      builder: (context) => CreateWorkspaceDialog(
        auth: _auth,
        defaultImage: defaultImage,
        allowedImages: allowedImages,
        allowAutostart: _auth.allowAutostart,
        defaultAllowedDomains: _auth.netfilterDefaultDomains,
      ),
    );

    if (created == true) {
      await _loadWorkspaces();
    }
  }

  Future<void> _showImportDialog() async {
    final imported = await showDialog<bool>(
      context: context,
      builder: (context) => ImportWorkspaceDialog(auth: _auth),
    );

    if (imported == true) {
      await _loadWorkspaces();
    }
  }

  Future<void> _deleteWorkspace(String id) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete Workspace'),
        content: const Text(
          'This will delete the workspace and all its files. Continue?',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            style: FilledButton.styleFrom(
              backgroundColor: KColors.accentRed,
              foregroundColor: Colors.white,
            ),
            child: const Text('Delete'),
          ),
        ],
      ),
    );

    if (confirmed != true) return;

    try {
      await _auth.authDelete('/api/v1/workspaces/$id');
      await _loadWorkspaces();
    } catch (e) {
      debugPrint('Workspace delete error: $e');
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            duration: Duration(days: 1),
            showCloseIcon: true,
            content: Text('Could not delete workspace. Please try again.'),
          ),
        );
      }
    }
  }

  String _formatCreatedAt(String? raw) {
    if (raw == null || raw.isEmpty) return '';
    try {
      // Backend sends UTC datetime as "YYYY-MM-DD HH:MM:SS"
      final utc = DateTime.parse('${raw}Z');
      final local = utc.toLocal();
      final months = [
        'Jan',
        'Feb',
        'Mar',
        'Apr',
        'May',
        'Jun',
        'Jul',
        'Aug',
        'Sep',
        'Oct',
        'Nov',
        'Dec',
      ];
      final h = local.hour > 12
          ? local.hour - 12
          : (local.hour == 0 ? 12 : local.hour);
      final ampm = local.hour >= 12 ? 'PM' : 'AM';
      final min = local.minute.toString().padLeft(2, '0');
      return '${months[local.month - 1]} ${local.day}, ${local.year}'
          ' at $h:$min $ampm';
    } catch (e) {
      debugPrint('[WorkspaceListPage] format date failed: $e');
      return raw;
    }
  }

  Widget _loadMoreButton(
    String label, {
    required bool enabled,
    required bool loading,
    required VoidCallback onPressed,
  }) {
    if (!enabled) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Center(
        child: loading
            ? const SizedBox(
                width: 20,
                height: 20,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : TextButton.icon(
                onPressed: onPressed,
                icon: const Icon(Icons.expand_more, size: 18),
                label: Text(label),
              ),
      ),
    );
  }

  Widget _sortChip(_Section section, String label, String sortKey) {
    final active = section.sort == sortKey;
    final arrow = section.order == 'asc' ? '▲' : '▼';
    return ActionChip(
      label: Text(active ? '$label $arrow' : label),
      onPressed: () => _changeSort(section, sortKey),
      backgroundColor:
          active ? KColors.accentBlue.withValues(alpha: 0.2) : null,
    );
  }

  Widget _buildControls(_Section section) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Row(
        children: [
          _sortChip(section, 'Name', 'name'),
          const SizedBox(width: 8),
          _sortChip(section, 'Created', 'created'),
          const SizedBox(width: 12),
          Expanded(
            child: TextField(
              controller: section.searchController,
              decoration: const InputDecoration(
                isDense: true,
                hintText: 'Filter by name...',
                prefixIcon: Icon(Icons.search, size: 18),
                border: OutlineInputBorder(),
                contentPadding: EdgeInsets.symmetric(
                  horizontal: 8,
                  vertical: 0,
                ),
              ),
              onChanged: (v) => _onQueryChanged(section, v),
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildSection(_Section section) {
    return Container(
      decoration: BoxDecoration(
        border: Border.all(color: KColors.borderDefault),
        borderRadius: BorderRadius.circular(8),
        color: KColors.bgSurface,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ...section.workspaces.asMap().entries.map(
                (e) => Material(
                  color: e.key.isEven
                      ? Colors.white.withValues(alpha: 0.03)
                      : Colors.transparent,
                  child: ListTile(
                    leading: Icon(
                      Icons.terminal,
                      size: 20,
                      // The icon signals container/health state: green
                      // when healthy (or running with no health check),
                      // amber when running but the health check failed,
                      // grey when stopped.
                      color: () {
                        final running = (e.value['running'] as bool? ?? false);
                        if (!running) return KColors.textSecondary;
                        final health = e.value['health'] as String?;
                        if (health == 'unhealthy') {
                          return Colors.orange;
                        }
                        return KColors.accentGreen;
                      }(),
                    ),
                    title: Text(e.value['name'] as String),
                    subtitle: section.isShared
                        ? Text(
                            '${e.value['owner_email']} · ${_formatCreatedAt(e.value['created_at'] as String?)}',
                          )
                        : Builder(
                            builder: (context) {
                              final wsMembers =
                                  _workspaceMembers[e.value['id'] as String] ??
                                      [];
                              return Row(
                                children: [
                                  Text(
                                    _formatCreatedAt(
                                      e.value['created_at'] as String?,
                                    ),
                                  ),
                                  if (_hasUnenforcedEgress(e.value)) ...[
                                    const SizedBox(width: 8),
                                    Tooltip(
                                      message: 'Allowed-domains set but '
                                          'egress filtering is not '
                                          'enforced (netfilter disabled '
                                          'on server)',
                                      child: Icon(
                                        Icons.warning_amber,
                                        size: 16,
                                        color: Colors.orange,
                                      ),
                                    ),
                                  ],
                                  if (wsMembers.isNotEmpty) ...[
                                    const SizedBox(width: 8),
                                    ...wsMembers.map((m) {
                                      final email = m['email'] as String;
                                      final letter = email.isNotEmpty
                                          ? email[0].toUpperCase()
                                          : '?';
                                      return Padding(
                                        padding:
                                            const EdgeInsets.only(right: 2),
                                        child: Tooltip(
                                          message: email,
                                          child: CircleAvatar(
                                            radius: 10,
                                            backgroundColor:
                                                KColors.colorForString(
                                              email,
                                            ),
                                            child: Text(
                                              letter,
                                              style: const TextStyle(
                                                fontSize: 10,
                                                color: Colors.white,
                                                fontWeight: FontWeight.bold,
                                              ),
                                            ),
                                          ),
                                        ),
                                      );
                                    }),
                                  ],
                                ],
                              );
                            },
                          ),
                    trailing: section.isShared
                        ? null
                        : IconButton(
                            icon: const Icon(Icons.delete_outline),
                            tooltip: 'Delete workspace',
                            onPressed: () =>
                                _deleteWorkspace(e.value['id'] as String),
                          ),
                    onTap: () =>
                        // coverage:ignore-start
                        context.go('/workspace/${e.value['id']}'),
                    // coverage:ignore-end
                  ),
                ),
              ),
          _loadMoreButton(
            section.isShared
                ? 'Load more shared workspaces'
                : 'Load more workspaces',
            enabled: section.hasMore,
            loading: section.loadingMore,
            onPressed: () => _loadMore(section),
          ),
        ],
      ),
    );
  }

  /// #1769: true when this workspace declares allowed_domains but the
  /// deploy has netfilter disabled, so the allow-list is NOT enforced
  /// (deliberate fail-open). Used to badge such workspaces in the list —
  /// the gap is otherwise visible only in operator logs.
  bool _hasUnenforcedEgress(Map<String, dynamic> ws) {
    if (_auth.netfilterEnabled) return false;
    final domains = ws['allowed_domains'] as List?;
    return domains != null && domains.isNotEmpty;
  }

  Widget _buildTabBody(_Section section) {
    final empty = section.workspaces.isEmpty;
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        _buildControls(section),
        if (empty)
          Center(
            child: Padding(
              padding: const EdgeInsets.only(top: 32),
              child: Text(
                section.query.isEmpty
                    ? (section.isShared
                        ? 'No workspaces shared with you.'
                        : 'No workspaces yet. Create one to get started.')
                    : 'No workspaces match.',
              ),
            ),
          )
        else
          _buildSection(section),
      ],
    );
  }

  Widget _buildWorkspacesList() {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    return DefaultTabController(
      length: 2,
      child: Column(
        children: [
          Material(
            color: KColors.bgSurface,
            child: const TabBar(
              tabs: [
                Tab(text: 'Owned by Me'),
                Tab(text: 'Shared with Me'),
              ],
            ),
          ),
          Expanded(
            child: TabBarView(
              children: [_buildTabBody(_owned), _buildTabBody(_shared)],
            ),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const AppBarTitle(title: 'Workspaces'),
        actions: const [AppBarActions()],
      ),
      floatingActionButton:
          context.watch<AuthService>().hasPermission('/workspaces', 'create')
              ? Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    FloatingActionButton.small(
                      heroTag: 'import',
                      onPressed: _showImportDialog,
                      tooltip: 'Import Workspace',
                      child: const Icon(Icons.upload),
                    ),
                    const SizedBox(height: 12),
                    FloatingActionButton(
                      heroTag: 'create',
                      onPressed: _createWorkspace,
                      tooltip: 'New Workspace',
                      child: const Icon(Icons.add),
                    ),
                  ],
                )
              : null,
      body: _buildWorkspacesList(),
    );
  }
}
