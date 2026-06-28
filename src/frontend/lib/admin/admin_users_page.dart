// coverage:ignore-file
import 'dart:async';
import 'dart:convert';
// ignore: unused_import
import '../theme/colors.dart';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../widgets/acl_editor.dart';
import '../widgets/app_bar_actions.dart';
import '../widgets/app_bar_title.dart';
import '../widgets/skeuo_tab.dart';

class AdminUsersPage extends StatefulWidget {
  const AdminUsersPage({super.key});

  @override
  State<AdminUsersPage> createState() => _AdminUsersPageState();
}

class _AdminUsersPageState extends State<AdminUsersPage> {
  List<Map<String, dynamic>> _users = [];
  List<Map<String, dynamic>> _invitations = [];
  List<Map<String, dynamic>> _groups = [];
  bool _loading = true;
  String? _error;
  int _selectedIndex = 0;

  // Admin users list: server-side pagination / sort / filter state.
  int _usersPage = 1;
  final int _usersPageSize = 10;
  int _usersTotal = 0;
  String _usersSort = 'created'; // email | handle | created
  String _usersOrder = 'desc'; // asc | desc
  String _usersQuery = '';
  final TextEditingController _searchController = TextEditingController();
  Timer? _usersQueryDebounce;

  // Admin invitations list: server-side pagination / sort / filter state.
  int _invitationsPage = 1;
  final int _invitationsPageSize = 10;
  int _invitationsTotal = 0;
  int _invitationsPending = 0; // global pending count (drives the tab badge)
  String _invitationsSort = 'created'; // email | invited_by | created
  String _invitationsOrder = 'desc'; // asc | desc
  String _invitationsQuery = '';
  final TextEditingController _invitationsSearchController =
      TextEditingController();
  Timer? _invitationsQueryDebounce;

  bool _canUsers = false;
  bool _canGroups = false;
  bool _canInvitations = false;

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

  @override
  void initState() {
    super.initState();
    // Live filter: re-query the backend as the user types, debounced so
    // we don't fire a request per keystroke. Restarts the debounce timer
    // on every change and resets to page 1 (a narrower result set may
    // collapse the previous page range).
    _searchController.addListener(() {
      final value = _searchController.text;
      if (value == _usersQuery) return;
      _usersQuery = value;
      _usersQueryDebounce?.cancel();
      _usersQueryDebounce =
          Timer(const Duration(milliseconds: 300), () => _loadUsers(page: 1));
    });
    _invitationsSearchController.addListener(() {
      final value = _invitationsSearchController.text;
      if (value == _invitationsQuery) return;
      _invitationsQuery = value;
      _invitationsQueryDebounce?.cancel();
      _invitationsQueryDebounce = Timer(
          const Duration(milliseconds: 300), () => _loadInvitations(page: 1));
    });
    _loadData();
  }

  @override
  void dispose() {
    _usersQueryDebounce?.cancel();
    _invitationsQueryDebounce?.cancel();
    _searchController.dispose();
    _invitationsSearchController.dispose();
    super.dispose();
  }

  void _resolvePermissions() {
    final auth = context.read<AuthService>();
    _canUsers = auth.hasPermission('/admin', '*') ||
        auth.hasPermission('/admin/users', 'view');
    _canGroups = auth.hasPermission('/admin', '*') ||
        auth.hasPermission('/admin/groups', 'view');
    _canInvitations = auth.hasPermission('/admin', '*') ||
        auth.hasPermission('/admin/invitations', 'view');
  }

  Future<void> _loadData() async {
    await Future.wait([_loadUsers(), _loadInvitations(), _loadGroups()]);
  }

  Future<void> _loadUsers({int page = 1}) async {
    setState(() {
      _loading = true;
      _error = null;
      _usersPage = page;
    });
    try {
      final auth = context.read<AuthService>();
      final query = <String, String>{
        'page': '$_usersPage',
        'page_size': '$_usersPageSize',
        'sort': _usersSort,
        'order': _usersOrder,
      };
      final q = _usersQuery.trim();
      if (q.isNotEmpty) query['q'] = q;
      final resp = await auth.authGet(
        '/api/v1/admin/users?${_encodeQuery(query)}',
      );
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as Map<String, dynamic>;
        setState(() {
          _users = (data['users'] as List).cast<Map<String, dynamic>>();
          _usersTotal = (data['total'] as num).toInt();
          _loading = false;
        });
      } else {
        setState(() {
          _error = 'Failed to load users: ${resp.statusCode}';
          _loading = false;
        });
      }
    } catch (e) {
      setState(() {
        _error = 'Error: $e';
        _loading = false;
      });
    }
  }

  Future<void> _loadInvitations({int page = 1}) async {
    setState(() {
      _invitationsPage = page;
    });
    try {
      final auth = context.read<AuthService>();
      final q = _invitationsQuery.trim();
      final query = 'page=$_invitationsPage'
          '&page_size=$_invitationsPageSize'
          '&sort=${Uri.encodeQueryComponent(_invitationsSort)}'
          '&order=${Uri.encodeQueryComponent(_invitationsOrder)}'
          '${q.isNotEmpty ? '&q=${Uri.encodeQueryComponent(q)}' : ''}';
      final resp = await auth.authGet(
        '/api/v1/admin/invitations?$query',
      );
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as Map<String, dynamic>;
        if (mounted) {
          setState(() {
            _invitations =
                (data['invitations'] as List).cast<Map<String, dynamic>>();
            _invitationsTotal = (data['total'] as num).toInt();
            _invitationsPending = (data['pending_count'] as num).toInt();
          });
        }
      }
    } catch (e) {
      debugPrint('[AdminUsersPage] load invitations failed: $e');
    }
  }

  Future<void> _loadGroups() async {
    try {
      final auth = context.read<AuthService>();
      final resp = await auth.authGet('/api/v1/admin/groups');
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as List;
        if (mounted) {
          setState(() {
            _groups = data.cast<Map<String, dynamic>>();
          });
        }
      }
    } catch (e) {
      debugPrint('[AdminUsersPage] load groups failed: $e');
    }
  }

  Future<void> _createGroup() async {
    final nameCtrl = TextEditingController();
    final descCtrl = TextEditingController();
    final result = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Create Group'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: nameCtrl,
              decoration: const InputDecoration(labelText: 'Group name'),
              autofocus: true,
            ),
            const SizedBox(height: 8),
            TextField(
              controller: descCtrl,
              decoration: const InputDecoration(
                labelText: 'Description (optional)',
              ),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            child: const Text('Create'),
          ),
        ],
      ),
    );
    if (result != true || nameCtrl.text.trim().isEmpty) return;
    final auth = context.read<AuthService>();
    final resp = await auth.authPost(
      '/api/v1/admin/groups',
      body: jsonEncode({
        'name': nameCtrl.text.trim(),
        if (descCtrl.text.trim().isNotEmpty)
          'description': descCtrl.text.trim(),
      }),
    );
    if (!mounted) return;
    if (resp.statusCode == 200) {
      _loadGroups();
    } else {
      _showSnack(resp);
    }
  }

  Future<void> _deleteGroup(String groupId, String groupName) async {
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete Group'),
        content: Text(
          'Delete group "$groupName"? All ACL entries for this group '
          'will be removed.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: FilledButton.styleFrom(backgroundColor: KColors.accentRed),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirm != true) return;
    final auth = context.read<AuthService>();
    final resp = await auth.authDelete('/api/v1/admin/groups/$groupId');
    if (!mounted) return;
    if (resp.statusCode == 200) {
      _loadGroups();
    } else {
      _showSnack(resp);
    }
  }

  Future<void> _manageMembers(Map<String, dynamic> group) async {
    final auth = context.read<AuthService>();
    final groupId = group['id'] as String;
    final groupName = group['name'] as String;

    final membersResp = await auth.authGet(
      '/api/v1/admin/groups/$groupId/members',
    );
    final usersResp = await auth.authGet(
      '/api/v1/admin/users?page_size=200',
    );
    if (!mounted) return;
    if (membersResp.statusCode != 200 || usersResp.statusCode != 200) return;

    var members = List<Map<String, dynamic>>.from(jsonDecode(membersResp.body));
    final allUsers = List<Map<String, dynamic>>.from(
      (jsonDecode(usersResp.body) as Map<String, dynamic>)['users'] as List,
    );

    await showDialog<void>(
      context: context,
      builder: (ctx) => StatefulBuilder(
        builder: (ctx, setDialogState) {
          final memberIds = members.map((m) => m['id']).toSet();
          final nonMembers =
              allUsers.where((u) => !memberIds.contains(u['id'])).toList();

          return AlertDialog(
            title: Text('Members of "$groupName"'),
            content: SizedBox(
              width: 400,
              height: 400,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (nonMembers.isNotEmpty)
                    DropdownButton<String>(
                      isExpanded: true,
                      hint: const Text('Add member...'),
                      items: nonMembers.map((u) {
                        return DropdownMenuItem(
                          value: u['id'] as String,
                          child: Text(u['email'] as String),
                        );
                      }).toList(),
                      onChanged: (userId) async {
                        if (userId == null) return;
                        final resp = await auth.authPost(
                          '/api/v1/admin/groups/$groupId/members',
                          body: jsonEncode({'user_id': userId}),
                        );
                        if (resp.statusCode == 200) {
                          final r = await auth.authGet(
                            '/api/v1/admin/groups/$groupId/members',
                          );
                          if (r.statusCode == 200) {
                            setDialogState(() {
                              members = List<Map<String, dynamic>>.from(
                                jsonDecode(r.body),
                              );
                            });
                          }
                        }
                      },
                    ),
                  const SizedBox(height: 8),
                  Expanded(
                    child: members.isEmpty
                        ? const Center(
                            child: Text(
                              'No members',
                              style: TextStyle(color: KColors.textSecondary),
                            ),
                          )
                        : ListView.builder(
                            itemCount: members.length,
                            itemBuilder: (ctx, i) {
                              final member = members[i];
                              return ListTile(
                                leading: CircleAvatar(
                                  backgroundColor: KColors.accentBlue,
                                  child: Text(
                                    (member['email'] as String)[0]
                                        .toUpperCase(),
                                    style: const TextStyle(color: Colors.white),
                                  ),
                                ),
                                title: Text(member['email'] as String),
                                trailing: IconButton(
                                  icon: const Icon(
                                    Icons.remove_circle_outline,
                                    color: KColors.accentRed,
                                  ),
                                  tooltip: 'Remove from group',
                                  onPressed: () async {
                                    final resp = await auth.authDelete(
                                      '/api/v1/admin/groups/$groupId/members/${member['id']}',
                                    );
                                    if (resp.statusCode == 200) {
                                      setDialogState(() {
                                        members.removeAt(i);
                                      });
                                    }
                                  },
                                ),
                              );
                            },
                          ),
                  ),
                ],
              ),
            ),
            actions: [
              FilledButton(
                onPressed: () => Navigator.pop(ctx),
                child: const Text('Done'),
              ),
            ],
          );
        },
      ),
    );
    _loadGroups();
  }

  void _showSnack(dynamic resp) {
    String msg;
    try {
      msg = jsonDecode(resp.body)['detail'] ?? 'Error';
    } catch (e) {
      debugPrint('[AdminUsersPage] parse error detail failed: $e');
      msg = 'Error: ${resp.statusCode}';
    }
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(msg)));
  }

  Widget _buildGroupsTab() {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_groups.isEmpty) return const Center(child: Text('No groups'));
    return ListView.builder(
      padding: const EdgeInsets.all(16),
      itemCount: _groups.length,
      itemBuilder: (ctx, i) {
        final group = _groups[i];
        final name = group['name'] as String;
        final desc = group['description'] as String? ?? '';
        return Card(
          margin: const EdgeInsets.only(bottom: 8),
          child: ListTile(
            leading: CircleAvatar(
              backgroundColor: KColors.colorForString(name),
              child: const Icon(Icons.group, color: Colors.white),
            ),
            title: Text(name),
            subtitle: Text(
              desc.isNotEmpty ? desc : ' ',
              style: const TextStyle(color: KColors.textSecondary),
            ),
            onTap: () => _manageMembers(group),
            trailing: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (name != 'admin')
                  IconButton(
                    icon: const Icon(
                      Icons.delete_outline,
                      color: KColors.accentRed,
                    ),
                    tooltip: 'Delete group',
                    onPressed: () => _deleteGroup(group['id'], name),
                  ),
                IconButton(
                  icon: const Icon(Icons.people, color: KColors.accentBlue),
                  tooltip: 'Manage members',
                  onPressed: () => _manageMembers(group),
                ),
              ],
            ),
          ),
        );
      },
    );
  }

  Future<void> _inviteUser() async {
    final email = await showDialog<String>(
      context: context,
      builder: (ctx) => _InviteUserDialog(),
    );
    if (email == null) return;

    final auth = context.read<AuthService>();
    final resp = await auth.authPost(
      '/api/v1/admin/invitations',
      body: jsonEncode({'email': email}),
    );
    if (resp.statusCode == 200) {
      // New invitation sorts to the top (created desc) — jump to page 1
      // so the admin sees it immediately.
      _loadInvitations(page: 1);
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text('Invitation sent to $email')));
      }
    } else {
      if (mounted) {
        final error = jsonDecode(resp.body);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(error['detail'] ?? 'Failed to send invitation'),
          ),
        );
      }
    }
  }

  Future<void> _revokeInvitation(String invitationId, String email) async {
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Revoke Invitation'),
        content: Text('Revoke the invitation for "$email"?'),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: FilledButton.styleFrom(
              backgroundColor: KColors.accentRed,
              foregroundColor: Colors.white,
            ),
            child: const Text('Revoke'),
          ),
        ],
      ),
    );
    if (confirm != true) return;

    final auth = context.read<AuthService>();
    final resp = await auth.authDelete(
      '/api/v1/admin/invitations/$invitationId',
    );
    if (resp.statusCode == 200) {
      // Revoked invitations stay in the list (status filter is out of
      // scope), so just refresh the current page to show the new status.
      _loadInvitations(page: _invitationsPage);
    } else {
      if (mounted) {
        final error = jsonDecode(resp.body);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(error['detail'] ?? 'Failed to revoke invitation'),
          ),
        );
      }
    }
  }

  Future<void> _resendInvitation(String invitationId, String email) async {
    final auth = context.read<AuthService>();
    final resp = await auth.authPost(
      '/api/v1/admin/invitations/$invitationId/resend',
    );
    if (mounted) {
      if (resp.statusCode == 200) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text('Invitation resent to $email')));
      } else {
        final error = jsonDecode(resp.body);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            content: Text(error['detail'] ?? 'Failed to resend invitation'),
          ),
        );
      }
    }
  }

  Future<void> _addUser() async {
    final result = await showDialog<Map<String, dynamic>>(
      context: context,
      builder: (ctx) => _AddUserDialog(),
    );
    if (result == null) return;

    final auth = context.read<AuthService>();
    final resp = await auth.authPost(
      '/api/v1/admin/users',
      body: jsonEncode(result),
    );
    if (resp.statusCode == 200) {
      _loadUsers();
      if (mounted) {
        final body = jsonDecode(resp.body);
        final status = body['status'] as String? ?? '';
        if (status == 'pending_verification') {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              content: Text('Verification email sent to ${result['email']}'),
            ),
          );
        }
      }
    } else {
      if (mounted) {
        final error = jsonDecode(resp.body);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(error['detail'] ?? 'Failed to add user')),
        );
      }
    }
  }

  Future<void> _deleteUser(String userId, String email) async {
    final confirm = await showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Delete User'),
        content: Text(
          'Delete user "$email"? This will delete all their workspaces and data.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(ctx, false),
            style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: FilledButton.styleFrom(
              backgroundColor: KColors.accentRed,
              foregroundColor: Colors.white,
            ),
            child: const Text('Delete'),
          ),
        ],
      ),
    );
    if (confirm != true) return;

    final auth = context.read<AuthService>();
    final resp = await auth.authDelete('/api/v1/admin/users/$userId');
    if (resp.statusCode == 200) {
      _loadUsers();
    } else {
      if (mounted) {
        final error = jsonDecode(resp.body);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(error['detail'] ?? 'Failed to delete user')),
        );
      }
    }
  }

  Future<void> _editUser(Map<String, dynamic> user) async {
    final result = await showDialog<Map<String, String>>(
      context: context,
      builder: (ctx) => _EditUserDialog(
        currentEmail: user['email'] as String,
        currentHandle: user['handle'] as String? ?? '',
      ),
    );
    if (result == null) return;

    final auth = context.read<AuthService>();
    final resp = await auth.authPatch(
      '/api/v1/admin/users/${user['id']}',
      body: jsonEncode(result),
    );
    if (resp.statusCode == 200) {
      _loadUsers();
    } else {
      if (mounted) {
        final error = jsonDecode(resp.body);
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(error['detail'] ?? 'Failed to update user')),
        );
      }
    }
  }

  Widget _buildUsersTab() {
    return Column(
      children: [
        _AdminListToolbar(
          key: const ValueKey('admin-users-toolbar'),
          columns: const [
            ('Email', 'email'),
            ('Handle', 'handle'),
            ('Created', 'created'),
          ],
          sort: _usersSort,
          order: _usersOrder,
          onChangeSort: _changeSort,
          searchController: _searchController,
          page: _usersPage,
          pageSize: _usersPageSize,
          total: _usersTotal,
          onPage: (p) => _loadUsers(page: p),
        ),
        Expanded(child: _buildUsersList()),
      ],
    );
  }

  /// Select a sort column, or toggle direction if already selected —
  /// mirrors the WorkspaceListPage sort chips. Resets to page 1 because a
  /// different sort reorders every row.
  Future<void> _changeSort(String sortKey) async {
    if (_usersSort == sortKey) {
      setState(() => _usersOrder = _usersOrder == 'asc' ? 'desc' : 'asc');
    } else {
      setState(() {
        _usersSort = sortKey;
        // Email/handle read naturally ascending; created descending.
        _usersOrder = sortKey == 'created' ? 'desc' : 'asc';
      });
    }
    await _loadUsers(page: 1);
  }

  Widget _buildUsersList() {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) return Center(child: Text(_error!));
    if (_users.isEmpty) return const Center(child: Text('No users'));
    return ListView.builder(
      padding: const EdgeInsets.all(16),
      itemCount: _users.length,
      itemBuilder: (ctx, i) {
        final user = _users[i];
        final isSelf = user['id'] == context.read<AuthService>().userId;
        final isSystem = user['provider'] == 'system';
        final email = user['email'] as String? ?? '';
        final initial = email.isNotEmpty ? email[0].toUpperCase() : '?';
        return Card(
          margin: const EdgeInsets.only(bottom: 8),
          child: ListTile(
            leading: _UserAvatar(initial: initial, email: email),
            title: Row(
              children: [
                Text(email),
                if ((user['handle'] as String?)?.isNotEmpty == true) ...[
                  const SizedBox(width: 8),
                  Text(
                    '@${user['handle']}',
                    style: const TextStyle(
                      color: KColors.textSecondary,
                      fontSize: 13,
                    ),
                  ),
                ],
              ],
            ),
            onTap: () => _editUser(user),
            trailing: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (!isSelf && !isSystem) ...[
                  IconButton(
                    icon: const Icon(
                      Icons.delete_outline,
                      color: KColors.accentRed,
                    ),
                    tooltip: 'Delete user',
                    onPressed: () => _deleteUser(user['id'], user['email']),
                  ),
                ],
              ],
            ),
          ),
        );
      },
    );
  }

  Widget _buildInvitationsTab() {
    return Column(
      children: [
        _AdminListToolbar(
          key: const ValueKey('admin-invitations-toolbar'),
          columns: const [
            ('Email', 'email'),
            ('Invited by', 'invited_by'),
            ('Created', 'created'),
          ],
          sort: _invitationsSort,
          order: _invitationsOrder,
          onChangeSort: _changeInvitationsSort,
          searchController: _invitationsSearchController,
          page: _invitationsPage,
          pageSize: _invitationsPageSize,
          total: _invitationsTotal,
          onPage: (p) => _loadInvitations(page: p),
        ),
        Expanded(child: _buildInvitationsList()),
      ],
    );
  }

  /// Select a sort column, or toggle direction if already selected —
  /// resets to page 1 because a different sort reorders every row.
  Future<void> _changeInvitationsSort(String sortKey) async {
    if (_invitationsSort == sortKey) {
      setState(() {
        _invitationsOrder = _invitationsOrder == 'asc' ? 'desc' : 'asc';
      });
    } else {
      setState(() {
        _invitationsSort = sortKey;
        // Email/invited_by read naturally ascending; created descending.
        _invitationsOrder = sortKey == 'created' ? 'desc' : 'asc';
      });
    }
    await _loadInvitations(page: 1);
  }

  Widget _buildInvitationsList() {
    if (_invitations.isEmpty) {
      return const Center(child: Text('No invitations'));
    }
    return ListView.builder(
      padding: const EdgeInsets.all(16),
      itemCount: _invitations.length,
      itemBuilder: (ctx, i) {
        final inv = _invitations[i];
        final email = inv['email'] as String? ?? '';
        final status = inv['status'] as String? ?? '';
        final invitedBy = inv['invited_by_email'] as String? ?? '';
        final createdAt = (inv['created_at'] as String? ?? '').length >= 10
            ? (inv['created_at'] as String).substring(0, 10)
            : '';
        final isPending = status == 'pending';
        final initial = email.isNotEmpty ? email[0].toUpperCase() : '?';
        return Card(
          margin: const EdgeInsets.only(bottom: 8),
          child: ListTile(
            leading: CircleAvatar(
              backgroundColor: isPending
                  ? KColors.accentAmber
                  : status == 'accepted'
                      ? KColors.accentGreen
                      : KColors.textMuted,
              child: Text(
                initial,
                style: const TextStyle(
                  color: Colors.white,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
            title: Text(email),
            subtitle: Text(
              'Status: $status — invited by $invitedBy on $createdAt',
              style: const TextStyle(color: KColors.textSecondary),
            ),
            trailing: isPending
                ? Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      IconButton(
                        icon: const Icon(Icons.send, color: KColors.accentBlue),
                        tooltip: 'Resend invitation',
                        onPressed: () => _resendInvitation(inv['id'], email),
                      ),
                      IconButton(
                        icon: const Icon(
                          Icons.cancel_outlined,
                          color: KColors.accentRed,
                        ),
                        tooltip: 'Revoke invitation',
                        onPressed: () => _revokeInvitation(inv['id'], email),
                      ),
                    ],
                  )
                : null,
          ),
        );
      },
    );
  }

  /// Map from tab index to tab type for FAB selection.
  List<String> get _tabTypes {
    final types = <String>[];
    if (_canUsers) types.add('users');
    if (_canGroups) types.add('groups');
    if (_canInvitations) types.add('invitations');
    if (types.isNotEmpty) types.add('acl');
    return types;
  }

  @override
  Widget build(BuildContext context) {
    // Re-resolve on each build: AuthService loads permissions asynchronously,
    // so the first build (before they arrive) would otherwise show no tabs.
    _resolvePermissions();
    final pendingCount = _invitationsPending;
    final tabs = <SkeuoTab>[];
    final views = <Widget>[];
    final tabTypes = _tabTypes;

    if (_canUsers) {
      final idx = tabs.length;
      tabs.add(
        SkeuoTab(
          label: 'Users',
          icon: Icons.people,
          isSelected: _selectedIndex == idx,
          onTap: () => setState(() => _selectedIndex = idx),
        ),
      );
      views.add(_buildUsersTab());
    }
    if (_canGroups) {
      final idx = tabs.length;
      tabs.add(
        SkeuoTab(
          label: 'Groups',
          icon: Icons.group,
          isSelected: _selectedIndex == idx,
          onTap: () => setState(() => _selectedIndex = idx),
        ),
      );
      views.add(_buildGroupsTab());
    }
    if (_canInvitations) {
      final idx = tabs.length;
      tabs.add(
        SkeuoTab(
          label: 'Invitations',
          icon: Icons.mail_outline,
          isSelected: _selectedIndex == idx,
          badge: pendingCount > 0 ? pendingCount : null,
          onTap: () => setState(() => _selectedIndex = idx),
        ),
      );
      views.add(_buildInvitationsTab());
    }

    // ACL tab — always visible for admins
    if (tabTypes.isNotEmpty) {
      final idx = tabs.length;
      tabs.add(
        SkeuoTab(
          label: 'Access Control',
          icon: Icons.security,
          isSelected: _selectedIndex == idx,
          onTap: () => setState(() => _selectedIndex = idx),
        ),
      );
      views.add(const _AclBrowserTab());
    }

    if (tabs.isEmpty) {
      views.add(const Center(child: Text('No admin sections available')));
    }

    Widget? fab;
    final currentType = tabTypes.isNotEmpty && _selectedIndex < tabTypes.length
        ? tabTypes[_selectedIndex]
        : '';
    if (currentType == 'users') {
      fab = FloatingActionButton(
        heroTag: 'add',
        onPressed: _addUser,
        tooltip: 'Add user',
        child: const Icon(Icons.person_add),
      );
    } else if (currentType == 'invitations') {
      fab = FloatingActionButton(
        heroTag: 'invite',
        onPressed: _inviteUser,
        tooltip: 'Invite user',
        child: const Icon(Icons.mail_outline),
      );
    } else if (currentType == 'groups') {
      fab = FloatingActionButton(
        heroTag: 'add-group',
        onPressed: _createGroup,
        tooltip: 'Create group',
        child: const Icon(Icons.group_add),
      );
    }

    return Scaffold(
      appBar: AppBar(
        title: const AppBarTitle(title: 'Admin'),
        actions: const [AppBarActions()],
      ),
      floatingActionButton: fab,
      body: Column(
        children: [
          if (tabs.isNotEmpty)
            Container(
              height: 40,
              color: KColors.bgCanvas,
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: tabs.map((t) => Expanded(child: t)).toList(),
              ),
            ),
          Expanded(
            child: IndexedStack(
              index: _selectedIndex < views.length ? _selectedIndex : 0,
              children: views,
            ),
          ),
        ],
      ),
    );
  }
}

class _AddUserDialog extends StatefulWidget {
  @override
  State<_AddUserDialog> createState() => _AddUserDialogState();
}

class _AddUserDialogState extends State<_AddUserDialog> {
  final _emailController = TextEditingController();
  final _passwordController = TextEditingController();
  bool _sendVerificationEmail = false;
  bool _obscurePassword = true;

  @override
  void dispose() {
    _emailController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final labelStyle = const TextStyle(
      color: KColors.textPrimary,
      fontWeight: FontWeight.bold,
    );
    return AlertDialog(
      title: Text('Add User', style: TextStyle(color: KColors.textPrimary)),
      content: SizedBox(
        width: 400,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: _emailController,
              decoration: InputDecoration(
                labelText: 'Email',
                labelStyle: labelStyle,
                floatingLabelStyle: labelStyle,
                floatingLabelBehavior: FloatingLabelBehavior.always,
                border: const OutlineInputBorder(),
              ),
              autofocus: true,
            ),
            const SizedBox(height: 16),
            CheckboxListTile(
              value: _sendVerificationEmail,
              onChanged: (v) =>
                  setState(() => _sendVerificationEmail = v ?? false),
              title: const Text('Send verification email'),
              subtitle: const Text(
                'User sets their own password via email link',
              ),
              controlAffinity: ListTileControlAffinity.leading,
              contentPadding: EdgeInsets.zero,
            ),
            if (!_sendVerificationEmail) ...[
              const SizedBox(height: 16),
              TextField(
                controller: _passwordController,
                decoration: InputDecoration(
                  labelText: 'Password',
                  labelStyle: labelStyle,
                  floatingLabelStyle: labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
                  border: const OutlineInputBorder(),
                  suffixIcon: IconButton(
                    icon: Icon(_obscurePassword
                        ? Icons.visibility_off
                        : Icons.visibility),
                    onPressed: () =>
                        setState(() => _obscurePassword = !_obscurePassword),
                  ),
                ),
                obscureText: _obscurePassword,
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
          onPressed: () {
            final email = _emailController.text.trim();
            if (email.isEmpty) return;
            if (_sendVerificationEmail) {
              Navigator.pop(context, <String, dynamic>{
                'email': email,
                'send_verification_email': true,
              });
            } else {
              final password = _passwordController.text;
              if (password.isEmpty) return;
              Navigator.pop(context, <String, dynamic>{
                'email': email,
                'password': password,
              });
            }
          },
          child: const Text('Add'),
        ),
      ],
    );
  }
}

class _EditUserDialog extends StatefulWidget {
  final String currentEmail;
  final String currentHandle;

  const _EditUserDialog({
    required this.currentEmail,
    required this.currentHandle,
  });

  @override
  State<_EditUserDialog> createState() => _EditUserDialogState();
}

class _EditUserDialogState extends State<_EditUserDialog> {
  late final TextEditingController _emailController;
  late final TextEditingController _handleController;
  final _passwordController = TextEditingController();
  bool _obscurePassword = true;

  @override
  void initState() {
    super.initState();
    _emailController = TextEditingController(text: widget.currentEmail);
    _handleController = TextEditingController(text: widget.currentHandle);
  }

  @override
  void dispose() {
    _emailController.dispose();
    _handleController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final labelStyle = const TextStyle(
      color: KColors.textPrimary,
      fontWeight: FontWeight.bold,
    );
    return AlertDialog(
      title: Text('Edit User', style: TextStyle(color: KColors.textPrimary)),
      content: SizedBox(
        width: 400,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            TextField(
              controller: _emailController,
              decoration: InputDecoration(
                labelText: 'Email',
                labelStyle: labelStyle,
                floatingLabelStyle: labelStyle,
                floatingLabelBehavior: FloatingLabelBehavior.always,
                border: const OutlineInputBorder(),
              ),
              autofocus: true,
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _handleController,
              decoration: InputDecoration(
                labelText: 'Handle',
                labelStyle: labelStyle,
                floatingLabelStyle: labelStyle,
                floatingLabelBehavior: FloatingLabelBehavior.always,
                border: const OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _passwordController,
              decoration: InputDecoration(
                labelText: 'New Password',
                labelStyle: labelStyle,
                floatingLabelStyle: labelStyle,
                floatingLabelBehavior: FloatingLabelBehavior.always,
                hintText: 'Leave blank to keep current',
                border: const OutlineInputBorder(),
                suffixIcon: IconButton(
                  icon: Icon(_obscurePassword
                      ? Icons.visibility_off
                      : Icons.visibility),
                  onPressed: () =>
                      setState(() => _obscurePassword = !_obscurePassword),
                ),
              ),
              obscureText: _obscurePassword,
            ),
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
          onPressed: () {
            final email = _emailController.text.trim();
            final handle = _handleController.text.trim();
            final password = _passwordController.text;
            if (email.isEmpty) return;
            final result = <String, String>{'email': email};
            if (handle != widget.currentHandle) result['handle'] = handle;
            if (password.isNotEmpty) result['password'] = password;
            Navigator.pop(context, result);
          },
          child: const Text('Save'),
        ),
      ],
    );
  }
}

class _InviteUserDialog extends StatefulWidget {
  @override
  State<_InviteUserDialog> createState() => _InviteUserDialogState();
}

class _InviteUserDialogState extends State<_InviteUserDialog> {
  final _emailController = TextEditingController();

  @override
  void dispose() {
    _emailController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final labelStyle = const TextStyle(
      color: KColors.textPrimary,
      fontWeight: FontWeight.bold,
    );
    return AlertDialog(
      title: Text('Invite User', style: TextStyle(color: KColors.textPrimary)),
      content: SizedBox(
        width: 400,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Text(
              'An email will be sent with a link to set their password and create an account.',
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _emailController,
              decoration: InputDecoration(
                labelText: 'Email',
                labelStyle: labelStyle,
                floatingLabelStyle: labelStyle,
                floatingLabelBehavior: FloatingLabelBehavior.always,
                border: const OutlineInputBorder(),
              ),
              autofocus: true,
              onSubmitted: (_) {
                final email = _emailController.text.trim();
                if (email.isNotEmpty) Navigator.pop(context, email);
              },
            ),
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
          onPressed: () {
            final email = _emailController.text.trim();
            if (email.isEmpty) return;
            Navigator.pop(context, email);
          },
          child: const Text('Send Invitation'),
        ),
      ],
    );
  }
}

class _UserAvatar extends StatelessWidget {
  final String initial;
  final String email;

  const _UserAvatar({
    required this.initial,
    required this.email,
  });

  @override
  Widget build(BuildContext context) {
    return SizedBox(
      width: 40,
      height: 40,
      child: Stack(
        clipBehavior: Clip.none,
        children: [
          CircleAvatar(
            radius: 20,
            backgroundColor: KColors.colorForString(email),
            child: Text(
              initial,
              style: const TextStyle(
                color: Colors.white,
                fontWeight: FontWeight.w600,
                fontSize: 16,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// ACL browser tab: shows the static resource tree and lets you edit ACLs.
class _AclBrowserTab extends StatefulWidget {
  const _AclBrowserTab();

  @override
  State<_AclBrowserTab> createState() => _AclBrowserTabState();
}

class _AclBrowserTabState extends State<_AclBrowserTab> {
  static const _resources = [
    ('/', 'Root', Icons.home),
    ('/workspaces', 'Workspaces', Icons.folder),
    ('/groups', 'Groups', Icons.group),
    ('/admin', 'Admin', Icons.manage_accounts),
    ('/admin/users', 'Users', Icons.people),
    ('/admin/invitations', 'Invitations', Icons.mail_outline),
    ('/admin/groups', 'Admin Groups', Icons.group),
  ];

  String _selectedResource = '/';

  String get _selectedLabel =>
      _resources.firstWhere((r) => r.$1 == _selectedResource).$2;

  IconData get _selectedIcon =>
      _resources.firstWhere((r) => r.$1 == _selectedResource).$3;

  @override
  Widget build(BuildContext context) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        // Resource tree sidebar
        SizedBox(
          width: 220,
          child: Container(
            decoration: const BoxDecoration(
              border: Border(right: BorderSide(color: KColors.borderDefault)),
            ),
            child: ListView(
              padding: const EdgeInsets.symmetric(vertical: 8),
              children: _resources.map((r) {
                final (path, label, icon) = r;
                final isSelected = path == _selectedResource;
                final depth = path == '/' ? 0 : path.split('/').length - 1;
                return InkWell(
                  onTap: () => setState(() => _selectedResource = path),
                  child: Container(
                    color: isSelected
                        ? KColors.accentGreen.withValues(alpha: 0.15)
                        : null,
                    padding: EdgeInsets.only(
                      left: 12.0 + depth * 16.0,
                      right: 12,
                      top: 10,
                      bottom: 10,
                    ),
                    child: Row(
                      children: [
                        Icon(
                          icon,
                          size: 16,
                          color: isSelected
                              ? KColors.accentGreen
                              : KColors.textSecondary,
                        ),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Text(
                            label,
                            style: TextStyle(
                              fontSize: 13,
                              fontWeight: isSelected
                                  ? FontWeight.bold
                                  : FontWeight.normal,
                              color: isSelected
                                  ? KColors.textPrimary
                                  : KColors.textSecondary,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                );
              }).toList(),
            ),
          ),
        ),
        // ACL editor for selected resource
        Expanded(
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(
                    border: Border.all(color: KColors.borderDefault),
                    borderRadius: BorderRadius.circular(8),
                    color: KColors.bgSurface,
                  ),
                  child: Row(
                    children: [
                      Icon(_selectedIcon, size: 20, color: KColors.accentGreen),
                      const SizedBox(width: 10),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            _selectedLabel,
                            style: const TextStyle(
                              fontWeight: FontWeight.bold,
                              fontSize: 14,
                            ),
                          ),
                          Text(
                            _selectedResource,
                            style: const TextStyle(
                              fontSize: 11,
                              color: KColors.textMuted,
                              fontFamily: 'monospace',
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
                const SizedBox(height: 12),
                Expanded(
                  child: SingleChildScrollView(
                    child: AclEditor(
                      key: ValueKey(_selectedResource),
                      resource: _selectedResource,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ],
    );
  }
}

/// Shared toolbar for the admin list tabs (Users, Invitations): sort chips,
/// a debounced email filter, and prev/next paging. The owning State holds
/// the sort/order/page/total fields and the search controller (whose
/// listener debounces a backend re-query); this widget just renders the
/// controls and reports interactions back via callbacks.
class _AdminListToolbar extends StatelessWidget {
  final List<(String, String)> columns; // (label, sortKey)
  final String sort;
  final String order; // 'asc' | 'desc'
  final ValueChanged<String> onChangeSort;
  final TextEditingController searchController;
  final int page;
  final int pageSize;
  final int total;
  final ValueChanged<int> onPage; // requested page number

  const _AdminListToolbar({
    super.key,
    required this.columns,
    required this.sort,
    required this.order,
    required this.onChangeSort,
    required this.searchController,
    required this.page,
    required this.pageSize,
    required this.total,
    required this.onPage,
  });

  Widget _sortChip(String label, String sortKey) {
    final active = sort == sortKey;
    final arrow = order == 'asc' ? '▲' : '▼';
    return ActionChip(
      label: Text(active ? '$label $arrow' : label),
      onPressed: () => onChangeSort(sortKey),
      backgroundColor:
          active ? KColors.accentBlue.withValues(alpha: 0.2) : null,
    );
  }

  @override
  Widget build(BuildContext context) {
    final totalPages = total == 0 ? 1 : (total + pageSize - 1) ~/ pageSize;
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 4),
      child: Wrap(
        spacing: 8,
        runSpacing: 8,
        crossAxisAlignment: WrapCrossAlignment.center,
        children: [
          for (final (label, key) in columns) _sortChip(label, key),
          const SizedBox(width: 12),
          SizedBox(
            width: 220,
            child: TextField(
              controller: searchController,
              decoration: const InputDecoration(
                isDense: true,
                hintText: 'Filter by email…',
                prefixIcon: Icon(Icons.search, size: 18),
                border: OutlineInputBorder(),
                contentPadding: EdgeInsets.symmetric(
                  horizontal: 8,
                  vertical: 0,
                ),
              ),
              // Enter submits immediately; the live debounce runs via the
              // controller listener in the owning State.
              onSubmitted: (_) => onPage(1),
            ),
          ),
          Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              IconButton(
                tooltip: 'Previous page',
                icon: const Icon(Icons.chevron_left),
                onPressed: page > 1 ? () => onPage(page - 1) : null,
              ),
              Text('$page / $totalPages'),
              IconButton(
                tooltip: 'Next page',
                icon: const Icon(Icons.chevron_right),
                onPressed: page < totalPages ? () => onPage(page + 1) : null,
              ),
            ],
          ),
        ],
      ),
    );
  }
}
