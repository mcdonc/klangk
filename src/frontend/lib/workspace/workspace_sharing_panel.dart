import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../theme/colors.dart';
import '../widgets/acl_editor.dart';

/// Simple workspace sharing panel: add/remove users by email.
/// Used as a tab in the IDE layout for users with share permission.
class WorkspaceSharingPanel extends StatefulWidget {
  final String workspaceId;

  const WorkspaceSharingPanel({super.key, required this.workspaceId});

  @override
  State<WorkspaceSharingPanel> createState() => WorkspaceSharingPanelState();
}

class WorkspaceSharingPanelState extends State<WorkspaceSharingPanel> {
  List<Map<String, dynamic>> _members = [];
  bool _loading = true;
  bool _aclExpanded = false;
  final _shareCtrl = TextEditingController();
  final _aclEditorKey = GlobalKey<AclEditorState>();
  List<Map<String, dynamic>> _searchResults = [];
  Timer? _searchDebounce;

  @override
  void initState() {
    super.initState();
    _loadMembers();
  }

  @override
  void dispose() {
    _shareCtrl.dispose();
    _searchDebounce?.cancel();
    super.dispose();
  }

  Future<void> _loadMembers() async {
    setState(() => _loading = true);
    final auth = context.read<AuthService>();
    final resp = await auth.authGet(
      '/workspaces/${widget.workspaceId}/members',
    );
    if (!mounted) return;
    if (resp.statusCode == 200) {
      setState(() {
        _members = List<Map<String, dynamic>>.from(jsonDecode(resp.body));
        _loading = false;
      });
    } else {
      setState(() => _loading = false);
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
      _loadMembers();
      _aclEditorKey.currentState?.reload();
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
    if (mounted) {
      _loadMembers();
      _aclEditorKey.currentState?.reload();
    }
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
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 500),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text('Shared Users',
                style: TextStyle(fontWeight: FontWeight.bold)),
            const SizedBox(height: 12),
            if (_loading)
              const Center(child: CircularProgressIndicator())
            else if (_members.isEmpty)
              const Padding(
                padding: EdgeInsets.only(bottom: 12),
                child: Text('No shared users',
                    style: TextStyle(color: KColors.textSecondary)),
              )
            else
              ..._members.map((m) => Padding(
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
                          onPressed: () => _removeMember(m['id'] as String),
                          padding: EdgeInsets.zero,
                          constraints: const BoxConstraints(),
                          tooltip: 'Remove access',
                        ),
                      ],
                    ),
                  )),
            const SizedBox(height: 8),
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
                    _addMember(r['email'] as String);
                    setState(() {
                      _searchResults = [];
                      _shareCtrl.clear();
                    });
                  },
                )),
            const SizedBox(height: 24),
            const Divider(),
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
    );
  }
}
