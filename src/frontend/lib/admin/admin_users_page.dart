import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../widgets/bark_logo.dart';
import '../widgets/app_bar_actions.dart';

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
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.pop(ctx, true),
            style: TextButton.styleFrom(foregroundColor: Colors.red),
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
        title: MouseRegion(
          cursor: SystemMouseCursors.click,
          child: GestureDetector(
            onTap: () => context.go('/'),
            child: const Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                BarkLogo(height: 36),
                SizedBox(width: 12),
                Text('User Management', style: TextStyle(fontSize: 16)),
              ],
            ),
          ),
        ),
        leading: IconButton(
          icon: const Icon(Icons.arrow_back),
          onPressed: () => context.go('/workspaces'),
        ),
        actions: [
          if (context.watch<AuthService>().email != null)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              child: Center(
                child: Text(
                  context.watch<AuthService>().email!,
                  style: const TextStyle(fontSize: 13),
                ),
              ),
            ),
          const AppBarActions(),
        ],
      ),
      floatingActionButton: FloatingActionButton(
        onPressed: _addUser,
        backgroundColor: const Color(0xFFB8E6C8),
        child: const Icon(Icons.person_add, color: Color(0xFF1A237E)),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _error != null
              ? Center(child: Text(_error!))
              : _users.isEmpty
                  ? const Center(child: Text('No users'))
                  : ListView.builder(
                      itemCount: _users.length,
                      itemBuilder: (ctx, i) {
                        final user = _users[i];
                        final roles = List<String>.from(user['roles'] ?? []);
                        final isAdmin = roles.contains('admin');
                        final isSelf =
                            user['id'] == context.read<AuthService>().userId;
                        return ListTile(
                          leading: Icon(
                            isAdmin ? Icons.admin_panel_settings : Icons.person,
                            color:
                                isAdmin ? const Color(0xFF1A237E) : Colors.grey,
                          ),
                          title: Text(user['email'] ?? ''),
                          subtitle: Text(
                            roles.isEmpty
                                ? 'No roles'
                                : 'Roles: ${roles.join(", ")}',
                          ),
                          trailing: Row(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              // Edit user
                              IconButton(
                                icon: const Icon(Icons.edit,
                                    color: Color(0xFF1A237E)),
                                tooltip: 'Edit user',
                                onPressed: () => _editUser(user),
                              ),
                              if (!isSelf) ...[
                                // Toggle admin role
                                IconButton(
                                  icon: Icon(
                                    isAdmin
                                        ? Icons.shield
                                        : Icons.shield_outlined,
                                    color: isAdmin ? Colors.amber : Colors.grey,
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
                                // Delete user
                                IconButton(
                                  icon: const Icon(Icons.delete,
                                      color: Colors.red),
                                  tooltip: 'Delete user',
                                  onPressed: () => _deleteUser(
                                    user['id'],
                                    user['email'],
                                  ),
                                ),
                              ],
                            ],
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
    return AlertDialog(
      title: const Text('Add User'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            controller: _emailController,
            decoration: const InputDecoration(labelText: 'Email'),
            autofocus: true,
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _passwordController,
            decoration: const InputDecoration(labelText: 'Password'),
            obscureText: true,
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Cancel'),
        ),
        TextButton(
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
    return AlertDialog(
      title: const Text('Edit User'),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            controller: _emailController,
            decoration: const InputDecoration(labelText: 'Email'),
            autofocus: true,
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _passwordController,
            decoration: const InputDecoration(
              labelText: 'New Password',
              hintText: 'Leave blank to keep current',
            ),
            obscureText: true,
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: const Text('Cancel'),
        ),
        TextButton(
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
