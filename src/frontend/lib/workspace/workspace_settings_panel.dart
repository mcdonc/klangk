import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../theme/colors.dart';

/// Workspace settings panel: config editing + ACL management.
/// Used as a tab in the IDE layout.
class WorkspaceSettingsPanel extends StatefulWidget {
  final String workspaceId;

  const WorkspaceSettingsPanel({super.key, required this.workspaceId});

  @override
  State<WorkspaceSettingsPanel> createState() => WorkspaceSettingsPanelState();
}

class WorkspaceSettingsPanelState extends State<WorkspaceSettingsPanel> {
  Map<String, dynamic>? _workspace;
  List<Map<String, dynamic>> _members = [];
  bool _loading = true;
  String? _error;
  String? _saveMessage;
  bool _canShare = false;

  @override
  void initState() {
    super.initState();
    _loadData();
  }

  Future<void> _loadData() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    final auth = context.read<AuthService>();

    // Load workspace list to find this workspace's data
    final wsResp = await auth.authGet('/workspaces');
    if (!mounted) return;
    if (wsResp.statusCode != 200) {
      // Try shared workspaces
      final sharedResp = await auth.authGet('/workspaces/shared');
      if (!mounted) return;
      if (sharedResp.statusCode == 200) {
        final shared = List<Map<String, dynamic>>.from(
          jsonDecode(sharedResp.body),
        );
        final ws = shared.cast<Map<String, dynamic>?>().firstWhere(
            (w) => w!['id'] == widget.workspaceId,
            orElse: () => null);
        if (ws != null) {
          setState(() {
            _workspace = ws;
            _loading = false;
            _canShare = false;
          });
          return;
        }
      }
      setState(() {
        _error = 'Failed to load workspace';
        _loading = false;
      });
      return;
    }

    final workspaces = List<Map<String, dynamic>>.from(
      jsonDecode(wsResp.body),
    );
    final ws = workspaces
        .cast<Map<String, dynamic>?>()
        .firstWhere((w) => w!['id'] == widget.workspaceId, orElse: () => null);

    if (ws == null) {
      setState(() {
        _error = 'Workspace not found';
        _loading = false;
      });
      return;
    }

    _workspace = ws;
    _canShare = true; // Owner can share

    // Load members
    try {
      final membersResp = await auth.authGet(
        '/workspaces/${widget.workspaceId}/members',
      );
      if (mounted && membersResp.statusCode == 200) {
        _members = List<Map<String, dynamic>>.from(
          jsonDecode(membersResp.body),
        );
      }
    } catch (_) {
      _canShare = false;
    }

    if (mounted) {
      setState(() => _loading = false);
    }
  }

  Future<void> _saveSettings(Map<String, dynamic> fields) async {
    final auth = context.read<AuthService>();
    final resp = await auth.authPut(
      '/workspaces/${widget.workspaceId}',
      body: jsonEncode(fields),
    );
    if (!mounted) return;
    if (resp.statusCode == 200) {
      setState(() => _saveMessage = 'Settings saved');
      _loadData();
      Future.delayed(const Duration(seconds: 2), () {
        if (mounted) setState(() => _saveMessage = null);
      });
    } else {
      String detail;
      try {
        detail = (jsonDecode(resp.body) as Map)['detail'] ?? resp.body;
      } catch (_) {
        detail = 'Error: ${resp.statusCode}';
      }
      setState(() => _saveMessage = 'Failed: $detail');
    }
  }

  Future<void> _addMember(String email) async {
    final auth = context.read<AuthService>();
    final resp = await auth.authPost(
      '/workspaces/${widget.workspaceId}/members',
      body: jsonEncode({'email': email}),
    );
    if (!mounted) return;
    if (resp.statusCode == 200) {
      _loadData();
    } else {
      String detail;
      try {
        detail = (jsonDecode(resp.body) as Map)['detail'] ?? resp.body;
      } catch (_) {
        detail = 'Error';
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text(detail)),
      );
    }
  }

  Future<void> _removeMember(String memberId) async {
    final auth = context.read<AuthService>();
    await auth.authDelete(
      '/workspaces/${widget.workspaceId}/members/$memberId',
    );
    if (mounted) _loadData();
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) return Center(child: Text(_error!));
    if (_workspace == null) return const Center(child: Text('No data'));

    return _SettingsForm(
      workspace: _workspace!,
      members: _members,
      canShare: _canShare,
      saveMessage: _saveMessage,
      onSave: _saveSettings,
      onAddMember: _addMember,
      onRemoveMember: _removeMember,
    );
  }
}

class _SettingsForm extends StatefulWidget {
  final Map<String, dynamic> workspace;
  final List<Map<String, dynamic>> members;
  final bool canShare;
  final String? saveMessage;
  final Future<void> Function(Map<String, dynamic>) onSave;
  final Future<void> Function(String email) onAddMember;
  final Future<void> Function(String memberId) onRemoveMember;

  const _SettingsForm({
    required this.workspace,
    required this.members,
    required this.canShare,
    required this.saveMessage,
    required this.onSave,
    required this.onAddMember,
    required this.onRemoveMember,
  });

  @override
  State<_SettingsForm> createState() => _SettingsFormState();
}

class _SettingsFormState extends State<_SettingsForm> {
  late TextEditingController _nameCtrl;
  late TextEditingController _cmdCtrl;
  final _shareCtrl = TextEditingController();
  List<Map<String, dynamic>> _searchResults = [];
  Timer? _searchDebounce;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _nameCtrl = TextEditingController(
      text: widget.workspace['name'] as String? ?? '',
    );
    _cmdCtrl = TextEditingController(
      text: widget.workspace['default_command'] as String? ?? '',
    );
  }

  @override
  void didUpdateWidget(_SettingsForm old) {
    super.didUpdateWidget(old);
    if (old.workspace['name'] != widget.workspace['name']) {
      _nameCtrl.text = widget.workspace['name'] as String? ?? '';
    }
    if (old.workspace['default_command'] !=
        widget.workspace['default_command']) {
      _cmdCtrl.text = widget.workspace['default_command'] as String? ?? '';
    }
  }

  @override
  void dispose() {
    _nameCtrl.dispose();
    _cmdCtrl.dispose();
    _shareCtrl.dispose();
    _searchDebounce?.cancel();
    super.dispose();
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    await widget.onSave({
      'name': _nameCtrl.text.trim(),
      'default_command':
          _cmdCtrl.text.trim().isEmpty ? null : _cmdCtrl.text.trim(),
    });
    if (mounted) setState(() => _saving = false);
  }

  void _searchUsers(String query) {
    _searchDebounce?.cancel();
    if (query.trim().isEmpty) {
      setState(() => _searchResults = []);
      return;
    }
    _searchDebounce = Timer(const Duration(milliseconds: 300), () async {
      final auth = context.read<AuthService>();
      try {
        final resp = await auth.authGet(
          '/users/search?q=${Uri.encodeQueryComponent(query.trim())}',
        );
        if (mounted && resp.statusCode == 200) {
          setState(() {
            _searchResults = List<Map<String, dynamic>>.from(
              jsonDecode(resp.body) as List,
            );
          });
        }
      } catch (_) {} // coverage:ignore-line
    });
  }

  @override
  Widget build(BuildContext context) {
    final labelStyle = TextStyle(
      color: KColors.textPrimary,
      fontWeight: FontWeight.bold,
    );

    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 500),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            // Save feedback
            if (widget.saveMessage != null) ...[
              Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                decoration: BoxDecoration(
                  color: widget.saveMessage!.startsWith('Failed')
                      ? KColors.accentRed.withValues(alpha: 0.1)
                      : KColors.accentGreen.withValues(alpha: 0.1),
                  borderRadius: BorderRadius.circular(4),
                ),
                child: Text(widget.saveMessage!),
              ),
              const SizedBox(height: 16),
            ],
            // Name
            TextField(
              controller: _nameCtrl,
              decoration: InputDecoration(
                labelText: 'Workspace Name',
                labelStyle: labelStyle,
                floatingLabelBehavior: FloatingLabelBehavior.always,
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 16),
            // Default command
            TextField(
              controller: _cmdCtrl,
              decoration: InputDecoration(
                labelText: 'Default Shell Command',
                labelStyle: labelStyle,
                floatingLabelBehavior: FloatingLabelBehavior.always,
                border: const OutlineInputBorder(),
                hintText: 'Optional — runs on terminal open',
              ),
            ),
            const SizedBox(height: 16),
            // Save button
            Align(
              alignment: Alignment.centerRight,
              child: FilledButton.icon(
                onPressed: _saving ? null : _save,
                icon: _saving
                    ? const SizedBox(
                        width: 16,
                        height: 16,
                        child: CircularProgressIndicator(
                          strokeWidth: 2,
                          color: Colors.white,
                        ))
                    : const Icon(Icons.save, size: 18),
                label: const Text('Save'),
              ),
            ),
            const SizedBox(height: 24),
            const Divider(),
            const SizedBox(height: 16),
            // Sharing / ACL section
            Text('Access Control', style: labelStyle),
            const SizedBox(height: 8),
            if (!widget.canShare)
              const Text(
                'You do not have permission to manage access for this workspace.',
                style: TextStyle(color: KColors.textSecondary),
              )
            else ...[
              // Current members
              if (widget.members.isEmpty)
                const Padding(
                  padding: EdgeInsets.only(bottom: 8),
                  child: Text('No shared users',
                      style: TextStyle(color: KColors.textSecondary)),
                )
              else
                ...widget.members.map((m) => Padding(
                      padding: const EdgeInsets.only(bottom: 4),
                      child: Row(
                        children: [
                          CircleAvatar(
                            radius: 14,
                            backgroundColor: KColors.accentBlue,
                            child: Text(
                              (m['email'] as String)[0].toUpperCase(),
                              style: const TextStyle(
                                  color: Colors.white, fontSize: 12),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: Text(m['email'] as String,
                                style: const TextStyle(fontSize: 13)),
                          ),
                          IconButton(
                            icon: const Icon(Icons.close, size: 18),
                            onPressed: () =>
                                widget.onRemoveMember(m['id'] as String),
                            padding: EdgeInsets.zero,
                            constraints: const BoxConstraints(),
                            tooltip: 'Remove access',
                          ),
                        ],
                      ),
                    )),
              const SizedBox(height: 8),
              // Search to add
              TextField(
                controller: _shareCtrl,
                decoration: const InputDecoration(
                  hintText: 'Type email to share...',
                  isDense: true,
                  border: OutlineInputBorder(),
                  prefixIcon: Icon(Icons.person_add, size: 18),
                ),
                style: const TextStyle(fontSize: 13),
                onChanged: _searchUsers,
              ),
              ..._searchResults.map((r) => ListTile(
                    dense: true,
                    title: Text(r['email'] as String,
                        style: const TextStyle(fontSize: 13)),
                    onTap: () {
                      widget.onAddMember(r['email'] as String);
                      setState(() {
                        _searchResults = [];
                        _shareCtrl.clear();
                      });
                    },
                  )),
            ],
          ],
        ),
      ),
    );
  }
}
