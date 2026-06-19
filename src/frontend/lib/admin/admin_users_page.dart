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

  bool _canUsers = false;
  bool _canGroups = false;
  bool _canInvitations = false;

  @override
  void initState() {
    super.initState();
    _resolvePermissions();
    _loadData();
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

  Future<void> _loadUsers() async {
    setState(() {
      _loading = true;
      _error = null;
    });
    try {
      final auth = context.read<AuthService>();
      final resp = await auth.authGet('/admin/users');
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as List;
        setState(() {
          _users = data.cast<Map<String, dynamic>>();
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

  Future<void> _loadInvitations() async {
    try {
      final auth = context.read<AuthService>();
      final resp = await auth.authGet('/admin/invitations');
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as List;
        if (mounted) {
          setState(() {
            _invitations = data.cast<Map<String, dynamic>>();
          });
        }
      }
    } catch (_) {
      // Invitations tab is best-effort
    }
  }

  Future<void> _loadGroups() async {
    try {
      final auth = context.read<AuthService>();
      final resp = await auth.authGet('/admin/groups');
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as List;
        if (mounted) {
          setState(() {
            _groups = data.cast<Map<String, dynamic>>();
          });
        }
      }
    } catch (_) {
      // Groups tab is best-effort
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
      '/admin/groups',
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
    final resp = await auth.authDelete('/admin/groups/$groupId');
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

    final membersResp = await auth.authGet('/admin/groups/$groupId/members');
    final usersResp = await auth.authGet('/admin/users');
    if (!mounted) return;
    if (membersResp.statusCode != 200 || usersResp.statusCode != 200) return;

    var members = List<Map<String, dynamic>>.from(jsonDecode(membersResp.body));
    final allUsers = List<Map<String, dynamic>>.from(
      jsonDecode(usersResp.body),
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
                          '/admin/groups/$groupId/members',
                          body: jsonEncode({'user_id': userId}),
                        );
                        if (resp.statusCode == 200) {
                          final r = await auth.authGet(
                            '/admin/groups/$groupId/members',
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
                                      '/admin/groups/$groupId/members/${member['id']}',
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
    } catch (_) {
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
      '/admin/invitations',
      body: jsonEncode({'email': email}),
    );
    if (resp.statusCode == 200) {
      _loadInvitations();
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
    final resp = await auth.authDelete('/admin/invitations/$invitationId');
    if (resp.statusCode == 200) {
      _loadInvitations();
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
    final resp = await auth.authPost('/admin/invitations/$invitationId/resend');
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
    final result = await showDialog<Map<String, String>>(
      context: context,
      builder: (ctx) => _AddUserDialog(),
    );
    if (result == null) return;

    final auth = context.read<AuthService>();
    final resp = await auth.authPost('/admin/users', body: jsonEncode(result));
    if (resp.statusCode == 200) {
      _loadUsers();
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
    final resp = await auth.authDelete('/admin/users/$userId');
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
      '/admin/users/${user['id']}',
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
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) return Center(child: Text(_error!));
    if (_users.isEmpty) return const Center(child: Text('No users'));
    return ListView.builder(
      padding: const EdgeInsets.all(16),
      itemCount: _users.length,
      itemBuilder: (ctx, i) {
        final user = _users[i];
        final groups = List<Map<String, dynamic>>.from(
          user['groups'] as List? ?? [],
        );
        final groupNames = groups.map((g) => g['name'] as String).toList();
        final isAdmin = groupNames.contains('admin');
        final isSelf = user['id'] == context.read<AuthService>().userId;
        final isSystem = user['provider'] == 'system';
        final email = user['email'] as String? ?? '';
        final initial = email.isNotEmpty ? email[0].toUpperCase() : '?';
        return Card(
          margin: const EdgeInsets.only(bottom: 8),
          child: ListTile(
            leading: _UserAvatar(
              initial: initial,
              email: email,
              isAdmin: isAdmin,
            ),
            title: Text(email),
            subtitle: Text(
              groupNames.isEmpty
                  ? 'No groups'
                  : 'Groups: ${groupNames.join(", ")}',
              style: const TextStyle(color: KColors.textSecondary),
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
    final pendingCount =
        _invitations.where((i) => i['status'] == 'pending').length;
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
            TextField(
              controller: _passwordController,
              decoration: InputDecoration(
                labelText: 'Password',
                labelStyle: labelStyle,
                floatingLabelStyle: labelStyle,
                floatingLabelBehavior: FloatingLabelBehavior.always,
                border: const OutlineInputBorder(),
              ),
              obscureText: true,
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
            final password = _passwordController.text;
            if (email.isEmpty || password.isEmpty) return;
            Navigator.pop(context, {'email': email, 'password': password});
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
              ),
              obscureText: true,
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
  final bool isAdmin;

  const _UserAvatar({
    required this.initial,
    required this.email,
    required this.isAdmin,
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
          if (isAdmin)
            Positioned(
              right: -2,
              bottom: -2,
              child: Container(
                width: 18,
                height: 18,
                decoration: BoxDecoration(
                  color: KColors.accentAmber,
                  shape: BoxShape.circle,
                  border: Border.all(color: KColors.bgSurface, width: 2),
                ),
                child: const Icon(Icons.shield, size: 10, color: Colors.white),
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
