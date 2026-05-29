import 'dart:convert';
// ignore: unused_import
import '../theme/colors.dart';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../widgets/app_bar_actions.dart';
import '../widgets/app_bar_title.dart';

class AdminUsersPage extends StatefulWidget {
  const AdminUsersPage({super.key});

  @override
  State<AdminUsersPage> createState() => _AdminUsersPageState();
}

class _AdminUsersPageState extends State<AdminUsersPage> {
  List<Map<String, dynamic>> _users = [];
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _loadUsers();
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

  Future<void> _addUser() async {
    final result = await showDialog<Map<String, String>>(
      context: context,
      builder: (ctx) => _AddUserDialog(),
    );
    if (result == null) return;

    final auth = context.read<AuthService>();
    final resp = await auth.authPost(
      '/auth/register',
      body: jsonEncode(result),
    );
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
                foregroundColor: Colors.white),
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

  Future<void> _toggleRole(String userId, String role, bool hasRole) async {
    final auth = context.read<AuthService>();
    if (hasRole) {
      await auth.authDelete('/admin/users/$userId/roles/$role');
    } else {
      await auth.authPost('/admin/users/$userId/roles/$role');
    }
    _loadUsers();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const AppBarTitle(title: 'User Management'),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => context.go('/workspaces'),
        ),
        actions: const [
          AppBarActions(),
        ],
      ),
      floatingActionButton: FloatingActionButton(
        onPressed: _addUser,
        child: const Icon(Icons.person_add),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
              ? Center(child: Text(_error!))
              : _users.isEmpty
                  ? const Center(child: Text('No users'))
                  : ListView.builder(
                      padding: const EdgeInsets.all(16),
                      itemCount: _users.length,
                      itemBuilder: (ctx, i) {
                        final user = _users[i];
                        final roles = List<String>.from(user['roles'] ?? []);
                        final isAdmin = roles.contains('admin');
                        final isSelf =
                            user['id'] == context.read<AuthService>().userId;
                        final email = user['email'] as String? ?? '';
                        final initial =
                            email.isNotEmpty ? email[0].toUpperCase() : '?';
                        return Card(
                          margin: const EdgeInsets.only(bottom: 8),
                          child: ListTile(
                            leading:
                                _UserAvatar(initial: initial, isAdmin: isAdmin),
                            title: Text(email),
                            subtitle: Text(
                              roles.isEmpty
                                  ? 'No roles'
                                  : 'Roles: ${roles.join(", ")}',
                              style:
                                  const TextStyle(color: KColors.textSecondary),
                            ),
                            onTap: () => _editUser(user),
                            trailing: Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                if (!isSelf) ...[
                                  IconButton(
                                    icon: Icon(
                                      isAdmin
                                          ? Icons.shield
                                          : Icons.shield_outlined,
                                      color: isAdmin
                                          ? KColors.accentAmber
                                          : KColors.textMuted,
                                    ),
                                    tooltip: isAdmin
                                        ? 'Remove admin role'
                                        : 'Grant admin role',
                                    onPressed: () => _toggleRole(
                                      user['id'],
                                      'admin',
                                      isAdmin,
                                    ),
                                  ),
                                  IconButton(
                                    icon: const Icon(Icons.delete_outline,
                                        color: KColors.accentRed),
                                    tooltip: 'Delete user',
                                    onPressed: () => _deleteUser(
                                      user['id'],
                                      user['email'],
                                    ),
                                  ),
                                ],
                              ],
                            ),
                          ),
                        );
                      },
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
            Navigator.pop(context, {
              'email': email,
              'password': password,
            });
          },
          child: const Text('Add'),
        ),
      ],
    );
  }
}

class _EditUserDialog extends StatefulWidget {
  final String currentEmail;

  const _EditUserDialog({required this.currentEmail});

  @override
  State<_EditUserDialog> createState() => _EditUserDialogState();
}

class _EditUserDialogState extends State<_EditUserDialog> {
  late final TextEditingController _emailController;
  final _passwordController = TextEditingController();

  @override
  void initState() {
    super.initState();
    _emailController = TextEditingController(text: widget.currentEmail);
  }

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
            final password = _passwordController.text;
            if (email.isEmpty) return;
            final result = <String, String>{'email': email};
            if (password.isNotEmpty) result['password'] = password;
            Navigator.pop(context, result);
          },
          child: const Text('Save'),
        ),
      ],
    );
  }
}

class _UserAvatar extends StatelessWidget {
  final String initial;
  final bool isAdmin;

  const _UserAvatar({required this.initial, required this.isAdmin});

  static const _letterColors = [
    Color(0xFF3B82F6), // blue
    Color(0xFF8B5CF6), // violet
    Color(0xFFEC4899), // pink
    Color(0xFFEF4444), // red
    Color(0xFFF97316), // orange
    Color(0xFFF59E0B), // amber
    Color(0xFF10B981), // emerald
    Color(0xFF14B8A6), // teal
    Color(0xFF06B6D4), // cyan
    Color(0xFF6366F1), // indigo
  ];

  @override
  Widget build(BuildContext context) {
    final colorIndex = initial.codeUnitAt(0) % _letterColors.length;
    return SizedBox(
      width: 40,
      height: 40,
      child: Stack(
        clipBehavior: Clip.none,
        children: [
          CircleAvatar(
            radius: 20,
            backgroundColor: _letterColors[colorIndex],
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
