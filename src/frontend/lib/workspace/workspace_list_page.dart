import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';
import '../utils/page_title.dart';
import '../widgets/app_bar_actions.dart';
import '../widgets/bark_logo.dart';

class WorkspaceListPage extends StatefulWidget {
  const WorkspaceListPage({super.key}); // coverage:ignore-line

  @override
  State<WorkspaceListPage> createState() => _WorkspaceListPageState();
}

class _WorkspaceListPageState extends State<WorkspaceListPage> {
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

  Future<Map<String, dynamic>?> _fetchImages() async {
    try {
      final response = await _auth.authGet('/images');
      if (response.statusCode == 200) {
        return jsonDecode(response.body) as Map<String, dynamic>;
      }
    } catch (_) {}
    return null;
  }

  Future<void> _createWorkspace() async {
    final imageData = await _fetchImages();
    final defaultImage = imageData?['default'] as String? ?? 'bark-pi';
    final allowedImages =
        (imageData?['allowed'] as List?)?.cast<String>() ?? [defaultImage];

    if (!mounted) return;

    final result = await showDialog<Map<String, String>>(
      context: context,
      builder: (context) {
        final nameController = TextEditingController();
        final cmdController = TextEditingController();
        var selectedImage = defaultImage;
        final primary = Theme.of(context).colorScheme.primary;
        final labelStyle = TextStyle(
          color: primary,
          fontWeight: FontWeight.bold,
        );
        return StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: Text('New Workspace', style: TextStyle(color: primary)),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                TextField(
                  controller: nameController,
                  decoration: InputDecoration(
                    labelText: 'Name',
                    labelStyle: labelStyle,
                    floatingLabelStyle: labelStyle,
                    floatingLabelBehavior: FloatingLabelBehavior.always,
                    border: const OutlineInputBorder(),
                  ),
                  autofocus: true,
                  onSubmitted: (_) => Navigator.pop(context, {
                    'name': nameController.text.trim(),
                    'command': cmdController.text.trim(),
                    'image': selectedImage,
                  }),
                ),
                const SizedBox(height: 16),
                DropdownButtonFormField<String>(
                  value: selectedImage,
                  decoration: InputDecoration(
                    labelText: 'Image',
                    labelStyle: labelStyle,
                    floatingLabelStyle: labelStyle,
                    floatingLabelBehavior: FloatingLabelBehavior.always,
                    border: const OutlineInputBorder(),
                  ),
                  items: allowedImages
                      .map((img) => DropdownMenuItem(
                            value: img,
                            child: Text(img),
                          ))
                      .toList(),
                  onChanged: (v) =>
                      setDialogState(() => selectedImage = v ?? defaultImage),
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: cmdController,
                  decoration: InputDecoration(
                    labelText: 'Default command (optional)',
                    labelStyle: labelStyle,
                    floatingLabelStyle: labelStyle,
                    floatingLabelBehavior: FloatingLabelBehavior.always,
                    border: const OutlineInputBorder(),
                  ),
                  onSubmitted: (_) => Navigator.pop(context, {
                    'name': nameController.text.trim(),
                    'command': cmdController.text.trim(),
                    'image': selectedImage,
                  }),
                ),
              ],
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(context, {
                  'name': nameController.text.trim(),
                  'command': cmdController.text.trim(),
                  'image': selectedImage,
                }),
                child: const Text('Create'),
              ),
            ],
          ),
        );
      },
    );

    if (result == null || result['name']!.isEmpty) return;

    final name = result['name']!;
    final command = result['command']!;
    final image = result['image']!;
    final body = <String, dynamic>{'name': name};
    if (command.isNotEmpty) {
      body['default_command'] = command;
    }
    if (image != defaultImage) {
      body['image'] = image;
    }

    try {
      final response = await _auth.authPost(
        '/workspaces',
        body: jsonEncode(body),
      );
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

  Future<void> _editWorkspace(Map<String, dynamic> ws) async {
    final imageData = await _fetchImages();
    final defaultImage = imageData?['default'] as String? ?? 'bark-pi';
    final allowedImages =
        (imageData?['allowed'] as List?)?.cast<String>() ?? [defaultImage];

    if (!mounted) return;

    final nameController =
        TextEditingController(text: ws['name'] as String? ?? '');
    final cmdController =
        TextEditingController(text: ws['default_command'] as String? ?? '');
    var selectedImage = ws['image'] as String? ?? defaultImage;
    if (!allowedImages.contains(selectedImage)) {
      selectedImage = defaultImage;
    }

    final saved = await showDialog<bool>(
      context: context,
      builder: (context) {
        final primary = Theme.of(context).colorScheme.primary;
        final labelStyle = TextStyle(
          color: primary,
          fontWeight: FontWeight.bold,
        );
        return StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: Text('Edit Workspace', style: TextStyle(color: primary)),
            content: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                TextField(
                  controller: nameController,
                  autofocus: true,
                  decoration: InputDecoration(
                    labelText: 'Name',
                    labelStyle: labelStyle,
                    floatingLabelStyle: labelStyle,
                    floatingLabelBehavior: FloatingLabelBehavior.always,
                    border: const OutlineInputBorder(),
                  ),
                ),
                const SizedBox(height: 16),
                DropdownButtonFormField<String>(
                  value: selectedImage,
                  decoration: InputDecoration(
                    labelText: 'Image',
                    labelStyle: labelStyle,
                    floatingLabelStyle: labelStyle,
                    floatingLabelBehavior: FloatingLabelBehavior.always,
                    border: const OutlineInputBorder(),
                  ),
                  items: allowedImages
                      .map((img) => DropdownMenuItem(
                            value: img,
                            child: Text(img),
                          ))
                      .toList(),
                  onChanged: (v) =>
                      setDialogState(() => selectedImage = v ?? defaultImage),
                ),
                const SizedBox(height: 16),
                TextField(
                  controller: cmdController,
                  decoration: InputDecoration(
                    labelText: 'Default command (optional)',
                    labelStyle: labelStyle,
                    floatingLabelStyle: labelStyle,
                    floatingLabelBehavior: FloatingLabelBehavior.always,
                    border: const OutlineInputBorder(),
                  ),
                  onSubmitted: (_) => Navigator.pop(context, true),
                ),
              ],
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(context, true),
                child: const Text('Save'),
              ),
            ],
          ),
        );
      },
    );

    if (saved != true) return;

    final name = nameController.text.trim();
    final command = cmdController.text.trim();
    if (name.isEmpty) return;

    try {
      final response = await _auth.authPut(
        '/workspaces/${ws['id']}',
        body: jsonEncode({
          'name': name,
          'image': selectedImage,
          'default_command': command.isEmpty ? null : command,
        }),
      );
      if (response.statusCode == 200) {
        await _loadWorkspaces();
      } else {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              duration: const Duration(days: 1),
              showCloseIcon: true,
              content: Text('Failed to update: ${response.body}'),
            ),
          );
        }
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(
            duration: const Duration(days: 1),
            showCloseIcon: true,
            content: Text('Error: $e'),
          ),
        );
      }
    }
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
                Text('Workspaces', style: TextStyle(fontSize: 16)),
              ],
            ),
          ),
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
                        trailing: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            IconButton(
                              icon: const Icon(Icons.settings_outlined),
                              onPressed: () => _editWorkspace(ws),
                            ),
                            IconButton(
                              icon: const Icon(Icons.delete_outline),
                              onPressed: () =>
                                  _deleteWorkspace(ws['id'] as String),
                            ),
                          ],
                        ),
                        onTap: () => context.go('/workspace/${ws['id']}'),
                      ),
                    );
                  },
                ),
    );
  }
}
