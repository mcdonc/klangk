import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../theme/colors.dart';

/// Reusable ACL editor widget for any resource path.
/// Shows ACE entries with add/remove/reorder, saves on button press.
class AclEditor extends StatefulWidget {
  final String resource;

  const AclEditor({super.key, required this.resource});

  @override
  State<AclEditor> createState() => AclEditorState();
}

class AclEditorState extends State<AclEditor> {
  List<Map<String, dynamic>> _entries = [];
  List<Map<String, dynamic>> _original = [];
  bool _loading = true;
  bool _saving = false;
  String? _message;

  static const _actionLabels = {0: 'Deny', 1: 'Allow'};
  static const _principalTypeLabels = {0: 'System', 1: 'User', 2: 'Group'};
  static const _permissions = [
    'view',
    'terminal',
    'files',
    'chat',
    'edit',
    'share',
    'delete',
    'create',
    '*',
  ];

  @override
  void initState() {
    super.initState();
    _load();
  }

  bool get _dirty => !_listEquals(_entries, _original);

  bool _listEquals(List<Map<String, dynamic>> a, List<Map<String, dynamic>> b) {
    if (a.length != b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (a[i].toString() != b[i].toString()) return false;
    }
    return true;
  }

  /// Reload ACL entries from the server.
  Future<void> reload() => _load();

  Future<void> _load() async {
    setState(() {
      _loading = true;
      _message = null;
    });
    final auth = context.read<AuthService>();
    final resp = await auth.authGet('/workspaces/${_wsId}/acl');
    if (!mounted) return;
    if (resp.statusCode == 200) {
      final data = List<Map<String, dynamic>>.from(jsonDecode(resp.body));
      setState(() {
        _entries = data;
        _original = List<Map<String, dynamic>>.from(
          data.map((e) => Map<String, dynamic>.from(e)),
        );
        _loading = false;
      });
    } else {
      setState(() {
        _loading = false;
        _message = 'Failed to load ACL';
      });
    }
  }

  String get _wsId {
    final parts = widget.resource.split('/');
    return parts.length >= 3 ? parts[2] : '';
  }

  void _removeEntry(int index) {
    setState(() => _entries.removeAt(index));
  }

  Future<void> _addEntry() async {
    final auth = context.read<AuthService>();

    // Fetch users and groups for the dropdown
    List<Map<String, dynamic>> users = [];
    List<Map<String, dynamic>> groups = [];
    try {
      final uResp = await auth.authGet('/admin/users');
      if (uResp.statusCode == 200) {
        users = List<Map<String, dynamic>>.from(jsonDecode(uResp.body));
      }
    } catch (_) {}
    try {
      final gResp = await auth.authGet('/admin/groups');
      if (gResp.statusCode == 200) {
        groups = List<Map<String, dynamic>>.from(jsonDecode(gResp.body));
      }
    } catch (_) {}

    if (!mounted) return;

    var principalType = 1; // user
    String? selectedUserId;
    String? selectedGroupId;
    var selectedPermission = 'view';
    var selectedAction = 1; // allow

    final result = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setDialogState) => AlertDialog(
          title: const Text('Add ACE'),
          content: SizedBox(
            width: 350,
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                DropdownButtonFormField<int>(
                  value: selectedAction,
                  decoration: const InputDecoration(
                    labelText: 'Action',
                    border: OutlineInputBorder(),
                  ),
                  items: _actionLabels.entries
                      .map((e) =>
                          DropdownMenuItem(value: e.key, child: Text(e.value)))
                      .toList(),
                  onChanged: (v) =>
                      setDialogState(() => selectedAction = v ?? 1),
                ),
                const SizedBox(height: 12),
                DropdownButtonFormField<int>(
                  value: principalType,
                  decoration: const InputDecoration(
                    labelText: 'Principal Type',
                    border: OutlineInputBorder(),
                  ),
                  items: _principalTypeLabels.entries
                      .map((e) =>
                          DropdownMenuItem(value: e.key, child: Text(e.value)))
                      .toList(),
                  onChanged: (v) => setDialogState(() {
                    principalType = v ?? 1;
                    selectedUserId = null;
                    selectedGroupId = null;
                  }),
                ),
                const SizedBox(height: 12),
                if (principalType == 1 && users.isNotEmpty)
                  DropdownButtonFormField<String>(
                    value: selectedUserId,
                    decoration: const InputDecoration(
                      labelText: 'User',
                      border: OutlineInputBorder(),
                    ),
                    items: users
                        .map((u) => DropdownMenuItem(
                            value: u['id'] as String,
                            child: Text(u['email'] as String)))
                        .toList(),
                    onChanged: (v) => setDialogState(() => selectedUserId = v),
                  ),
                if (principalType == 2 && groups.isNotEmpty)
                  DropdownButtonFormField<String>(
                    value: selectedGroupId,
                    decoration: const InputDecoration(
                      labelText: 'Group',
                      border: OutlineInputBorder(),
                    ),
                    items: groups
                        .map((g) => DropdownMenuItem(
                            value: g['id'] as String,
                            child: Text(g['name'] as String)))
                        .toList(),
                    onChanged: (v) => setDialogState(() => selectedGroupId = v),
                  ),
                if (principalType == 0) ...[
                  DropdownButtonFormField<int>(
                    value: 1,
                    decoration: const InputDecoration(
                      labelText: 'System Principal',
                      border: OutlineInputBorder(),
                    ),
                    items: const [
                      DropdownMenuItem(value: 0, child: Text('Everyone')),
                      DropdownMenuItem(value: 1, child: Text('Authenticated')),
                    ],
                    onChanged: (_) {},
                  ),
                ],
                const SizedBox(height: 12),
                DropdownButtonFormField<String>(
                  value: selectedPermission,
                  decoration: const InputDecoration(
                    labelText: 'Permission',
                    border: OutlineInputBorder(),
                  ),
                  items: _permissions
                      .map((p) => DropdownMenuItem(value: p, child: Text(p)))
                      .toList(),
                  onChanged: (v) =>
                      setDialogState(() => selectedPermission = v ?? 'view'),
                ),
              ],
            ),
          ),
          actions: [
            TextButton(
                onPressed: () => Navigator.pop(ctx),
                child: const Text('Cancel')),
            FilledButton(
              onPressed: () {
                final entry = <String, dynamic>{
                  'action': selectedAction,
                  'principal_type': principalType,
                  'permission': selectedPermission,
                };
                if (principalType == 0) {
                  entry['system_principal'] = 1; // Authenticated default
                  entry['principal'] = 'Authenticated';
                } else if (principalType == 1 && selectedUserId != null) {
                  entry['user_id'] = selectedUserId;
                  final u = users.firstWhere((u) => u['id'] == selectedUserId);
                  entry['principal'] = u['email'];
                } else if (principalType == 2 && selectedGroupId != null) {
                  entry['group_id'] = selectedGroupId;
                  final g =
                      groups.firstWhere((g) => g['id'] == selectedGroupId);
                  entry['principal'] = g['name'];
                } else {
                  return; // no principal selected
                }
                Navigator.pop(ctx, entry);
              },
              child: const Text('Add'),
            ),
          ],
        ),
      ),
    );

    if (result != null) {
      setState(() => _entries.add(result));
    }
  }

  Future<void> _save() async {
    setState(() {
      _saving = true;
      _message = null;
    });
    final auth = context.read<AuthService>();
    final payload = _entries.asMap().entries.map((e) {
      final entry = e.value;
      return {
        'action': entry['action'],
        'principal_type': entry['principal_type'],
        'permission': entry['permission'],
        'user_id': entry['user_id'],
        'group_id': entry['group_id'],
        'system_principal': entry['system_principal'],
      };
    }).toList();

    final resp = await auth.authPut(
      '/workspaces/$_wsId/acl',
      body: jsonEncode(payload),
    );
    if (!mounted) return;
    if (resp.statusCode == 200) {
      final data = List<Map<String, dynamic>>.from(jsonDecode(resp.body));
      setState(() {
        _entries = data;
        _original = List<Map<String, dynamic>>.from(
          data.map((e) => Map<String, dynamic>.from(e)),
        );
        _saving = false;
        _message = 'Saved';
      });
      Future.delayed(const Duration(seconds: 2), () {
        if (mounted) setState(() => _message = null);
      });
    } else {
      String detail;
      try {
        detail = (jsonDecode(resp.body) as Map)['detail'] ?? resp.body;
      } catch (_) {
        detail = 'Error';
      }
      setState(() {
        _saving = false;
        _message = 'Failed: $detail — reloading from server';
      });
      // Reload to show current server state after conflict
      await _load();
    }
  }

  void _discard() {
    setState(() {
      _entries = List<Map<String, dynamic>>.from(
        _original.map((e) => Map<String, dynamic>.from(e)),
      );
      _message = null;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            const Text('Access Control Entries',
                style: TextStyle(fontWeight: FontWeight.bold, fontSize: 13)),
            const Spacer(),
            TextButton.icon(
              onPressed: _addEntry,
              icon: const Icon(Icons.add, size: 16),
              label: const Text('Add'),
            ),
          ],
        ),
        if (_message != null)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text(
              _message!,
              style: TextStyle(
                color: _message!.startsWith('Failed')
                    ? KColors.accentRed
                    : KColors.accentGreen,
                fontSize: 12,
              ),
            ),
          ),
        if (_entries.isEmpty)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 8),
            child: Text('No ACL entries',
                style: TextStyle(color: KColors.textSecondary, fontSize: 12)),
          )
        else
          ReorderableListView.builder(
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            itemCount: _entries.length,
            onReorder: (oldIndex, newIndex) {
              setState(() {
                if (newIndex > oldIndex) newIndex--;
                final item = _entries.removeAt(oldIndex);
                _entries.insert(newIndex, item);
              });
            },
            itemBuilder: (context, i) {
              final entry = _entries[i];
              final action = entry['action'] as int;
              final principal = entry['principal'] as String? ?? '?';
              final permission = entry['permission'] as String;

              return Card(
                key: ValueKey('ace-$i-${entry['id'] ?? i}'),
                margin: const EdgeInsets.only(bottom: 2),
                child: Padding(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
                  child: Row(
                    children: [
                      // Drag handle
                      ReorderableDragStartListener(
                        index: i,
                        child: const Icon(Icons.drag_handle,
                            size: 18, color: KColors.textMuted),
                      ),
                      const SizedBox(width: 4),
                      // Allow/Deny badge
                      Container(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 6, vertical: 2),
                        decoration: BoxDecoration(
                          color: action == 1
                              ? KColors.accentGreen.withValues(alpha: 0.2)
                              : KColors.accentRed.withValues(alpha: 0.2),
                          borderRadius: BorderRadius.circular(4),
                        ),
                        child: Text(
                          action == 1 ? 'Allow' : 'Deny',
                          style: TextStyle(
                            fontSize: 11,
                            fontWeight: FontWeight.bold,
                            color: action == 1
                                ? KColors.accentGreen
                                : KColors.accentRed,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                      // Principal
                      Expanded(
                        child: Text(principal,
                            style: const TextStyle(fontSize: 12)),
                      ),
                      // Permission
                      Container(
                        padding: const EdgeInsets.symmetric(
                            horizontal: 6, vertical: 2),
                        decoration: BoxDecoration(
                          color: KColors.bgCanvas,
                          borderRadius: BorderRadius.circular(4),
                          border: Border.all(color: KColors.borderDefault),
                        ),
                        child: Text(permission,
                            style: const TextStyle(fontSize: 11)),
                      ),
                      const SizedBox(width: 4),
                      // Remove
                      InkWell(
                        onTap: () => _removeEntry(i),
                        child: const Icon(Icons.close,
                            size: 16, color: KColors.textMuted),
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
        if (_dirty) ...[
          const SizedBox(height: 8),
          Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              TextButton(
                onPressed: _discard,
                child: const Text('Discard'),
              ),
              const SizedBox(width: 8),
              FilledButton.icon(
                onPressed: _saving ? null : _save,
                icon: _saving
                    ? const SizedBox(
                        width: 14,
                        height: 14,
                        child: CircularProgressIndicator(
                            strokeWidth: 2, color: Colors.white))
                    : const Icon(Icons.save, size: 16),
                label: const Text('Save ACL'),
              ),
            ],
          ),
        ],
      ],
    );
  }
}
