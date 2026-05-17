import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../utils/backend_url.dart';
import '../utils/page_title.dart';
import '../widgets/bark_logo.dart';

class WorkspaceListPage extends StatefulWidget {
  const WorkspaceListPage({super.key});

  @override
  State<WorkspaceListPage> createState() => _WorkspaceListPageState();
}

class _WorkspaceListPageState extends State<WorkspaceListPage> {
  String get _baseUrl => baseUrl;
  List<Map<String, dynamic>> _workspaces = [];
  bool _loading = true;

  @override
  void initState() {
    super.initState();
    setPageTitle('Workspaces');
    _loadWorkspaces();
  }

  AuthService get _auth => context.read<AuthService>();

  Future<void> _loadWorkspaces() async {
    setState(() => _loading = true);
    try {
      final response = await _auth.authGet('/workspaces');
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body) as List;
        setState(() {
          _workspaces = data.cast<Map<String, dynamic>>();
        });
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
              duration: const Duration(days: 1),
              showCloseIcon: true,
              content: Text('Failed to load workspaces: $e')),
        );
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<void> _createWorkspace() async {
    final name = await showDialog<String>(
      context: context,
      builder: (context) {
        final controller = TextEditingController();
        return AlertDialog(
          title: const Text('New Workspace'),
          content: TextField(
            controller: controller,
            decoration: const InputDecoration(
              labelText: 'Workspace name',
              border: OutlineInputBorder(),
            ),
            autofocus: true,
            onSubmitted: (v) => Navigator.pop(context, v.trim()),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () => Navigator.pop(context, controller.text.trim()),
              child: const Text('Create'),
            ),
          ],
        );
      },
    );

    if (name == null || name.isEmpty) return;

    try {
      final response = await _auth.authPost('/workspaces?name=$name');
      if (response.statusCode == 200) {
        await _loadWorkspaces();
      } else {
        final error = jsonDecode(response.body);
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
                duration: const Duration(days: 1),
                showCloseIcon: true,
                content: Text(error['detail'] ?? 'Failed to create workspace')),
          );
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
              duration: const Duration(days: 1),
              showCloseIcon: true,
              content: Text('Error: $e')),
        );
      }
    }
  }

  Future<void> _deleteWorkspace(String id) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete Workspace'),
        content: const Text(
            'This will delete the workspace and all its files. Continue?'),
        actions: [
          TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            style: FilledButton.styleFrom(
                backgroundColor: Theme.of(context).colorScheme.error),
            child: const Text('Delete'),
          ),
        ],
      ),
    );

    if (confirmed != true) return;

    try {
      await _auth.authDelete('/workspaces/$id');
      await _loadWorkspaces();
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
              duration: const Duration(days: 1),
              showCloseIcon: true,
              content: Text('Error: $e')),
        );
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            BarkLogo(height: 36),
            SizedBox(width: 12),
            Text('Workspaces', style: TextStyle(fontSize: 16)),
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.logout, color: Color(0xFF1A237E)),
            tooltip: 'Logout',
            onPressed: () async {
              await context.read<AuthService>().logout();
              if (mounted) context.go('/login');
            },
          ),
        ],
      ),
      floatingActionButton: FloatingActionButton(
        onPressed: _createWorkspace,
        child: const Icon(Icons.add),
      ),
      body: _loading
          ? const Center(child: CircularProgressIndicator())
          : _workspaces.isEmpty
              ? const Center(
                  child: Text('No workspaces yet. Create one to get started.'),
                )
              : ListView.builder(
                  padding: const EdgeInsets.all(16),
                  itemCount: _workspaces.length,
                  itemBuilder: (context, index) {
                    final ws = _workspaces[index];
                    return Card(
                      child: ListTile(
                        leading: const Icon(Icons.folder),
                        title: Text(ws['name'] as String),
                        subtitle: Text('Created: ${ws['created_at'] ?? ''}'),
                        trailing: IconButton(
                          icon: const Icon(Icons.delete_outline),
                          onPressed: () => _deleteWorkspace(ws['id'] as String),
                        ),
                        onTap: () => context.go('/workspace/${ws['id']}'),
                      ),
                    );
                  },
                ),
    );
  }
}
