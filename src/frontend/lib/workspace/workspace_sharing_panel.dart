import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../theme/colors.dart';
import '../widgets/acl_editor.dart';

/// Workspace sharing panel with role-based buckets.
class WorkspaceSharingPanel extends StatefulWidget {
  final String workspaceId;

  const WorkspaceSharingPanel({super.key, required this.workspaceId});

  @override
  State<WorkspaceSharingPanel> createState() => WorkspaceSharingPanelState();
}

class WorkspaceSharingPanelState extends State<WorkspaceSharingPanel> {
  List<Map<String, dynamic>> _roles = [];
  bool _loading = true;
  bool _aclExpanded = false;
  final _aclEditorKey = GlobalKey<AclEditorState>();

  static const _roleOrder = ['owners', 'collaborators', 'coders', 'spectators'];

  static const _roleDescriptions = {
    'owners': 'Full admin access',
    'coders':
        'Use isolated terminals, spectate on shared terminals, files, chat',
    'collaborators':
        'Use isolated and shared terminals, share terminals, files, chat',
    'spectators': 'Watch shared terminals',
  };

  static const _roleIcons = {
    'owners': Icons.shield,
    'coders': Icons.code,
    'collaborators': Icons.people,
    'spectators': Icons.visibility,
  };

  static const _roleColors = {
    'owners': KColors.accentAmber,
    'coders': KColors.accentGreen,
    'collaborators': KColors.accentCyan,
    'spectators': KColors.accentBlue,
  };

  List<Map<String, dynamic>> get _sortedRoles {
    final order = {
      for (var i = 0; i < _roleOrder.length; i++) _roleOrder[i]: i,
    };
    return List.of(_roles)..sort(
      (a, b) => (order[a['role']] ?? 99).compareTo(order[b['role']] ?? 99),
    );
  }

  @override
  void initState() {
    super.initState();
    _loadRoles();
  }

  Future<void> _loadRoles() async {
    setState(() => _loading = true);
    final auth = context.read<AuthService>();
    final resp = await auth.authGet(
      '/api/v1/workspaces/${widget.workspaceId}/roles',
    );
    if (!mounted) return;
    if (resp.statusCode == 200) {
      _roles = List<Map<String, dynamic>>.from(jsonDecode(resp.body));
    }
    setState(() => _loading = false);
  }

  Future<void> _addToRole(String role, String email) async {
    final auth = context.read<AuthService>();
    final resp = await auth.authPost(
      '/api/v1/workspaces/${widget.workspaceId}/roles/$role',
      body: jsonEncode({'email': email}),
    );
    if (!mounted) return;
    if (resp.statusCode == 200) {
      _loadRoles();
      _aclEditorKey.currentState?.reload();
    } else {
      String detail;
      try {
        detail = (jsonDecode(resp.body) as Map)['detail'] ?? resp.body;
      } catch (e) {
        debugPrint('[WorkspaceSharingPanel] parse error detail failed: $e');
        detail = 'Error';
      }
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(detail)));
      }
    }
  }

  Future<void> _removeFromRole(String role, String memberId) async {
    final auth = context.read<AuthService>();
    await auth.authDelete(
      '/api/v1/workspaces/${widget.workspaceId}/roles/$role/$memberId',
    );
    if (mounted) {
      _loadRoles();
      _aclEditorKey.currentState?.reload();
    }
  }

  void _showAddDialog(String role) {
    final controller = TextEditingController();
    final searchResults = ValueNotifier<List<Map<String, dynamic>>>([]);
    Timer? debounce;

    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: Text('Add to ${role}'),
        content: SizedBox(
          width: 300,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              TextField(
                controller: controller,
                autofocus: true,
                decoration: const InputDecoration(
                  hintText: 'Type email...',
                  border: OutlineInputBorder(),
                  prefixIcon: Icon(Icons.person_add, size: 18),
                ),
                onChanged: (q) {
                  debounce?.cancel();
                  if (q.trim().isEmpty) {
                    searchResults.value = [];
                    return;
                  }
                  debounce = Timer(const Duration(milliseconds: 300), () async {
                    final auth = context.read<AuthService>();
                    try {
                      final resp = await auth.authGet(
                        '/api/v1/users/search?q=${Uri.encodeQueryComponent(q.trim())}',
                      );
                      if (resp.statusCode == 200) {
                        searchResults.value = List<Map<String, dynamic>>.from(
                          jsonDecode(resp.body) as List,
                        );
                      }
                    } catch (e) {
                      debugPrint(
                        '[WorkspaceSharingPanel] user search failed: $e',
                      );
                    }
                  });
                },
                onSubmitted: (value) {
                  final email = value.trim();
                  if (email.isNotEmpty) {
                    Navigator.of(ctx).pop();
                    _addToRole(role, email);
                  }
                },
              ),
              const SizedBox(height: 8),
              ValueListenableBuilder<List<Map<String, dynamic>>>(
                valueListenable: searchResults,
                builder: (_, results, __) => Column(
                  mainAxisSize: MainAxisSize.min,
                  children: results
                      .map(
                        (r) => ListTile(
                          dense: true,
                          title: Text(
                            r['email'] as String,
                            style: const TextStyle(fontSize: 13),
                          ),
                          onTap: () {
                            Navigator.of(ctx).pop();
                            _addToRole(role, r['email'] as String);
                          },
                        ),
                      )
                      .toList(),
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () {
              debounce?.cancel();
              Navigator.of(ctx).pop();
            },
            child: const Text('Cancel'),
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 500),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (_loading)
                const Center(child: CircularProgressIndicator())
              else
                for (final role in _sortedRoles) _buildRoleBucket(role),
              const SizedBox(height: 16),
              // Collapsible ACL editor
              GestureDetector(
                onTap: () => setState(() => _aclExpanded = !_aclExpanded),
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 8),
                  child: Row(
                    children: [
                      Icon(
                        _aclExpanded ? Icons.expand_less : Icons.expand_more,
                        size: 20,
                        color: KColors.textSecondary,
                      ),
                      const SizedBox(width: 4),
                      const Text(
                        'Advanced: Access Control',
                        style: TextStyle(
                          fontWeight: FontWeight.bold,
                          color: KColors.textSecondary,
                          fontSize: 13,
                        ),
                      ),
                    ],
                  ),
                ),
              ),
              if (_aclExpanded)
                AclEditor(
                  key: _aclEditorKey,
                  resource: '/workspaces/${widget.workspaceId}',
                ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildRoleBucket(Map<String, dynamic> role) {
    final roleName = role['role'] as String;
    final members = role['members'] as List? ?? [];
    final icon = _roleIcons[roleName] ?? Icons.group;
    final color = _roleColors[roleName] ?? KColors.accentBlue;
    final desc = _roleDescriptions[roleName] ?? '';

    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Container(
        padding: const EdgeInsets.all(12),
        decoration: BoxDecoration(
          border: Border.all(color: KColors.borderDefault),
          borderRadius: BorderRadius.circular(8),
          color: KColors.bgSurface,
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Icon(icon, size: 16, color: color),
                const SizedBox(width: 8),
                Text(
                  roleName[0].toUpperCase() + roleName.substring(1),
                  style: const TextStyle(
                    fontWeight: FontWeight.bold,
                    fontSize: 14,
                  ),
                ),
                const SizedBox(width: 8),
                Expanded(
                  child: Text(
                    desc,
                    style: const TextStyle(
                      color: KColors.textSecondary,
                      fontSize: 11,
                    ),
                  ),
                ),
                IconButton(
                  icon: const Icon(Icons.person_add, size: 16),
                  onPressed: () => _showAddDialog(roleName),
                  padding: EdgeInsets.zero,
                  constraints: const BoxConstraints(
                    minWidth: 28,
                    minHeight: 28,
                  ),
                  tooltip: 'Add user',
                ),
              ],
            ),
            if (members.isEmpty)
              const Padding(
                padding: EdgeInsets.only(top: 4),
                child: Text(
                  'No members',
                  style: TextStyle(color: KColors.textMuted, fontSize: 12),
                ),
              )
            else
              Wrap(
                spacing: 6,
                runSpacing: 4,
                children: [
                  for (final m in members)
                    Chip(
                      label: Text(
                        m['email'] as String,
                        style: const TextStyle(fontSize: 11),
                      ),
                      deleteIcon: const Icon(Icons.close, size: 14),
                      onDeleted: () =>
                          _removeFromRole(roleName, m['id'] as String),
                      materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                      visualDensity: VisualDensity.compact,
                    ),
                ],
              ),
          ],
        ),
      ),
    );
  }
}
