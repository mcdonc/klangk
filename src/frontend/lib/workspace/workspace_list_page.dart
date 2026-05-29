import 'dart:convert';
import 'package:flutter/material.dart';
import '../theme/colors.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import '../utils/page_title.dart';
import '../widgets/app_bar_actions.dart';
import '../widgets/app_bar_title.dart';

const _validMountOptions = {
  'ro',
  'rw',
  'z',
  'Z',
  'nocopy',
  'consistent',
  'cached',
  'delegated'
};

String? validateMountSpec(String spec) {
  final parts = spec.split(':');
  if (parts.length < 2 || parts.length > 3) {
    return 'Expected source:dest or source:dest:options';
  }
  if (parts[0].isEmpty) {
    return 'Source is empty';
  }
  if (!parts[1].startsWith('/')) {
    return 'Container path must be absolute (start with /)';
  }
  if (parts.length == 3) {
    for (final opt in parts[2].split(',')) {
      if (opt.isNotEmpty && !_validMountOptions.contains(opt)) {
        return 'Unknown option: $opt';
      }
    }
  }
  return null;
}

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
    final defaultImage = imageData?['default'] as String? ?? 'klangk-pi';
    final allowedImages =
        (imageData?['allowed'] as List?)?.cast<String>() ?? [defaultImage];

    if (!mounted) return;

    final created = await showDialog<bool>(
      context: context,
      builder: (context) {
        final nameController = TextEditingController();
        final cmdController = TextEditingController();
        final mountController = TextEditingController();
        final envController = TextEditingController();
        var selectedImage = defaultImage;
        final mounts = <String>[];
        final envVars = <String, String>{};
        String? errorMessage;
        String? mountError;
        String? envError;
        final primary = Theme.of(context).colorScheme.primary;
        final labelStyle = TextStyle(
          color: KColors.textPrimary,
          fontWeight: FontWeight.bold,
        );

        void tryAddMount(void Function(void Function()) setState) {
          final v = mountController.text.trim();
          if (v.isEmpty) return;
          final err = validateMountSpec(v);
          if (err != null) {
            setState(() => mountError = err);
            return;
          }
          setState(() {
            mounts.add(v);
            mountController.clear();
            mountError = null;
          });
        }

        void tryAddEnv(void Function(void Function()) setState) {
          final v = envController.text.trim();
          if (v.isEmpty) return;
          if (!v.contains('=')) {
            setState(() => envError = 'Expected KEY=VALUE format');
            return;
          }
          final key = v.substring(0, v.indexOf('='));
          final value = v.substring(v.indexOf('=') + 1);
          if (key.isEmpty) {
            setState(() => envError = 'Key cannot be empty');
            return;
          }
          setState(() {
            envVars[key] = value;
            envController.clear();
            envError = null;
          });
        }

        Future<void> submit(
            BuildContext ctx, void Function(void Function()) setState) async {
          final name = nameController.text.trim();
          if (name.isEmpty) return;
          final command = cmdController.text.trim();
          final body = <String, dynamic>{'name': name};
          if (command.isNotEmpty) body['default_command'] = command;
          if (selectedImage != defaultImage) body['image'] = selectedImage;
          if (mounts.isNotEmpty) body['mounts'] = List<String>.from(mounts);
          if (envVars.isNotEmpty)
            body['env'] = Map<String, String>.from(envVars);

          try {
            final response = await _auth.authPost(
              '/workspaces',
              body: jsonEncode(body),
            );
            if (response.statusCode == 200) {
              if (ctx.mounted) Navigator.pop(ctx, true);
            } else {
              final error = jsonDecode(response.body);
              setState(() {
                errorMessage =
                    error['detail'] as String? ?? 'Failed to create workspace';
              });
            }
          } catch (e) {
            setState(() => errorMessage = 'Error: $e');
          }
        }

        return StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: Text('New Workspace',
                style: TextStyle(color: KColors.textPrimary)),
            content: SizedBox(
              width: 400,
              child: SingleChildScrollView(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (errorMessage != null) ...[
                      Text(errorMessage!,
                          style: TextStyle(
                              color: Theme.of(context).colorScheme.error)),
                      const SizedBox(height: 12),
                    ],
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
                      onSubmitted: (_) => submit(context, setDialogState),
                    ),
                    const SizedBox(height: 16),
                    DropdownButtonFormField<String>(
                      value: selectedImage,
                      decoration: InputDecoration(
                        labelText: 'Container Image',
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
                      onChanged: (v) => setDialogState(
                          () => selectedImage = v ?? defaultImage),
                    ),
                    const SizedBox(height: 16),
                    TextField(
                      controller: cmdController,
                      decoration: InputDecoration(
                        labelText: 'Default shell command (optional)',
                        labelStyle: labelStyle,
                        floatingLabelStyle: labelStyle,
                        floatingLabelBehavior: FloatingLabelBehavior.always,
                        border: const OutlineInputBorder(),
                      ),
                      onSubmitted: (_) => submit(context, setDialogState),
                    ),
                    const SizedBox(height: 16),
                    Text('Mounts', style: labelStyle),
                    const SizedBox(height: 8),
                    ...mounts.asMap().entries.map((e) => Padding(
                          padding: const EdgeInsets.only(bottom: 4),
                          child: Row(
                            children: [
                              Expanded(
                                  child: Text(e.value,
                                      style: const TextStyle(fontSize: 13))),
                              IconButton(
                                icon: const Icon(Icons.close, size: 18),
                                onPressed: () => setDialogState(
                                    () => mounts.removeAt(e.key)),
                                padding: EdgeInsets.zero,
                                constraints: const BoxConstraints(),
                              ),
                            ],
                          ),
                        )),
                    if (mountError != null) ...[
                      Text(mountError!,
                          style: TextStyle(
                              color: Theme.of(context).colorScheme.error,
                              fontSize: 12)),
                      const SizedBox(height: 4),
                    ],
                    Row(
                      children: [
                        Expanded(
                          child: TextField(
                            controller: mountController,
                            decoration: const InputDecoration(
                              hintText: '/host/path:/container/path',
                              isDense: true,
                              border: OutlineInputBorder(),
                            ),
                            style: const TextStyle(fontSize: 13),
                            onSubmitted: (_) => tryAddMount(setDialogState),
                          ),
                        ),
                        const SizedBox(width: 8),
                        IconButton(
                          icon: const Icon(Icons.add),
                          onPressed: () => tryAddMount(setDialogState),
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    Text('Environment Variables', style: labelStyle),
                    const SizedBox(height: 8),
                    ...envVars.entries
                        .toList()
                        .asMap()
                        .entries
                        .map((e) => Padding(
                              padding: const EdgeInsets.only(bottom: 4),
                              child: Row(
                                children: [
                                  Expanded(
                                      child: Text(
                                          '${e.value.key}=${e.value.value}',
                                          style:
                                              const TextStyle(fontSize: 13))),
                                  IconButton(
                                    icon: const Icon(Icons.close, size: 18),
                                    onPressed: () => setDialogState(
                                        () => envVars.remove(e.value.key)),
                                    padding: EdgeInsets.zero,
                                    constraints: const BoxConstraints(),
                                  ),
                                ],
                              ),
                            )),
                    if (envError != null) ...[
                      Text(envError!,
                          style: TextStyle(
                              color: Theme.of(context).colorScheme.error,
                              fontSize: 12)),
                      const SizedBox(height: 4),
                    ],
                    Row(
                      children: [
                        Expanded(
                          child: TextField(
                            controller: envController,
                            decoration: const InputDecoration(
                              hintText: 'KEY=VALUE',
                              isDense: true,
                              border: OutlineInputBorder(),
                            ),
                            style: const TextStyle(fontSize: 13),
                            onSubmitted: (_) => tryAddEnv(setDialogState),
                          ),
                        ),
                        const SizedBox(width: 8),
                        IconButton(
                          icon: const Icon(Icons.add),
                          onPressed: () => tryAddEnv(setDialogState),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context),
                style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: () => submit(context, setDialogState),
                child: const Text('Create'),
              ),
            ],
          ),
        );
      },
    );

    if (created == true) {
      await _loadWorkspaces();
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
              style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
              child: const Text('Cancel')),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            style: FilledButton.styleFrom(
                backgroundColor: KColors.accentRed,
                foregroundColor: Colors.white),
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

  Future<void> _duplicateWorkspace(Map<String, dynamic> ws) async {
    final nameController =
        TextEditingController(text: '${ws['name'] as String}-copy');

    final newName = await showDialog<String>(
      context: context,
      builder: (context) {
        final primary = Theme.of(context).colorScheme.primary;
        return AlertDialog(
          title: Text('Duplicate Workspace',
              style: TextStyle(color: KColors.textPrimary)),
          content: TextField(
            controller: nameController,
            autofocus: true,
            decoration: const InputDecoration(
              labelText: 'New workspace name',
              border: OutlineInputBorder(),
            ),
            onSubmitted: (v) => Navigator.pop(context, v.trim()),
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
              child: const Text('Cancel'),
            ),
            FilledButton(
              onPressed: () =>
                  Navigator.pop(context, nameController.text.trim()),
              child: const Text('Duplicate'),
            ),
          ],
        );
      },
    );

    if (newName == null || newName.isEmpty) return;

    try {
      final response = await _auth.authPost(
        '/workspaces/${ws['id']}/duplicate',
        body: jsonEncode({'name': newName}),
      );
      if (response.statusCode == 200) {
        await _loadWorkspaces();
      } else {
        if (mounted) {
          final error = jsonDecode(response.body);
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(
              duration: const Duration(days: 1),
              showCloseIcon: true,
              content: Text(error['detail'] as String? ??
                  'Failed to duplicate workspace'),
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
              content: Text('Error: $e')),
        );
      }
    }
  }

  Future<void> _editWorkspace(Map<String, dynamic> ws) async {
    final imageData = await _fetchImages();
    final defaultImage = imageData?['default'] as String? ?? 'klangk-pi';
    final allowedImages =
        (imageData?['allowed'] as List?)?.cast<String>() ?? [defaultImage];

    if (!mounted) return;

    final nameController =
        TextEditingController(text: ws['name'] as String? ?? '');
    final cmdController =
        TextEditingController(text: ws['default_command'] as String? ?? '');
    final mountController = TextEditingController();
    final envController = TextEditingController();
    var selectedImage = ws['image'] as String? ?? defaultImage;
    if (!allowedImages.contains(selectedImage)) {
      selectedImage = defaultImage;
    }
    final mounts = List<String>.from(
        (ws['mounts'] as List?)?.cast<String>() ?? <String>[]);
    final envVars = Map<String, String>.from(
        (ws['env'] as Map?)?.cast<String, String>() ?? <String, String>{});

    final saved = await showDialog<bool>(
      context: context,
      builder: (context) {
        final primary = Theme.of(context).colorScheme.primary;
        final labelStyle = TextStyle(
          color: KColors.textPrimary,
          fontWeight: FontWeight.bold,
        );
        String? errorMessage;
        String? mountError;
        String? envError;

        void tryAddMount(void Function(void Function()) setState) {
          final v = mountController.text.trim();
          if (v.isEmpty) return;
          final err = validateMountSpec(v);
          if (err != null) {
            setState(() => mountError = err);
            return;
          }
          setState(() {
            mounts.add(v);
            mountController.clear();
            mountError = null;
          });
        }

        void tryAddEnv(void Function(void Function()) setState) {
          final v = envController.text.trim();
          if (v.isEmpty) return;
          if (!v.contains('=')) {
            setState(() => envError = 'Expected KEY=VALUE format');
            return;
          }
          final key = v.substring(0, v.indexOf('='));
          final value = v.substring(v.indexOf('=') + 1);
          if (key.isEmpty) {
            setState(() => envError = 'Key cannot be empty');
            return;
          }
          setState(() {
            envVars[key] = value;
            envController.clear();
            envError = null;
          });
        }

        Future<void> submit(
            BuildContext ctx, void Function(void Function()) setState) async {
          final name = nameController.text.trim();
          if (name.isEmpty) return;
          final command = cmdController.text.trim();

          try {
            final response = await _auth.authPut(
              '/workspaces/${ws['id']}',
              body: jsonEncode({
                'name': name,
                'image': selectedImage,
                'default_command': command.isEmpty ? null : command,
                'mounts': mounts.isNotEmpty ? mounts : null,
                'env': envVars.isNotEmpty ? envVars : null,
              }),
            );
            if (response.statusCode == 200) {
              if (ctx.mounted) Navigator.pop(ctx, true);
            } else {
              String detail;
              try {
                detail = (jsonDecode(response.body)
                        as Map<String, dynamic>)['detail'] as String? ??
                    response.body;
              } catch (_) {
                detail = response.body;
              }
              setState(() => errorMessage = 'Failed to update: $detail');
            }
          } catch (e) {
            setState(() => errorMessage = 'Error: $e');
          }
        }

        return StatefulBuilder(
          builder: (context, setDialogState) => AlertDialog(
            title: Text('Edit Workspace',
                style: TextStyle(color: KColors.textPrimary)),
            content: SizedBox(
              width: 400,
              child: SingleChildScrollView(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (errorMessage != null) ...[
                      Align(
                        alignment: Alignment.centerLeft,
                        child: Text(errorMessage!,
                            style: TextStyle(
                                color: Theme.of(context).colorScheme.error)),
                      ),
                      const SizedBox(height: 12),
                    ],
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
                        labelText: 'Container Image',
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
                      onChanged: (v) => setDialogState(
                          () => selectedImage = v ?? defaultImage),
                    ),
                    const SizedBox(height: 16),
                    TextField(
                      controller: cmdController,
                      decoration: InputDecoration(
                        labelText: 'Default shell command (optional)',
                        labelStyle: labelStyle,
                        floatingLabelStyle: labelStyle,
                        floatingLabelBehavior: FloatingLabelBehavior.always,
                        border: const OutlineInputBorder(),
                      ),
                      onSubmitted: (_) => submit(context, setDialogState),
                    ),
                    const SizedBox(height: 16),
                    Align(
                      alignment: Alignment.centerLeft,
                      child: Text('Mounts', style: labelStyle),
                    ),
                    const SizedBox(height: 8),
                    ...mounts.asMap().entries.map((e) => Padding(
                          padding: const EdgeInsets.only(bottom: 4),
                          child: Row(
                            children: [
                              Expanded(
                                  child: Text(e.value,
                                      style: const TextStyle(fontSize: 13))),
                              IconButton(
                                icon: const Icon(Icons.close, size: 18),
                                onPressed: () => setDialogState(
                                    () => mounts.removeAt(e.key)),
                                padding: EdgeInsets.zero,
                                constraints: const BoxConstraints(),
                              ),
                            ],
                          ),
                        )),
                    if (mountError != null) ...[
                      Align(
                        alignment: Alignment.centerLeft,
                        child: Text(mountError!,
                            style: TextStyle(
                                color: Theme.of(context).colorScheme.error,
                                fontSize: 12)),
                      ),
                      const SizedBox(height: 4),
                    ],
                    Row(
                      children: [
                        Expanded(
                          child: TextField(
                            controller: mountController,
                            decoration: const InputDecoration(
                              hintText: '/host/path:/container/path',
                              isDense: true,
                              border: OutlineInputBorder(),
                            ),
                            style: const TextStyle(fontSize: 13),
                            onSubmitted: (_) => tryAddMount(setDialogState),
                          ),
                        ),
                        const SizedBox(width: 8),
                        IconButton(
                          icon: const Icon(Icons.add),
                          onPressed: () => tryAddMount(setDialogState),
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    Align(
                      alignment: Alignment.centerLeft,
                      child: Text('Environment Variables', style: labelStyle),
                    ),
                    const SizedBox(height: 8),
                    ...envVars.entries
                        .toList()
                        .asMap()
                        .entries
                        .map((e) => Padding(
                              padding: const EdgeInsets.only(bottom: 4),
                              child: Row(
                                children: [
                                  Expanded(
                                      child: Text(
                                          '${e.value.key}=${e.value.value}',
                                          style:
                                              const TextStyle(fontSize: 13))),
                                  IconButton(
                                    icon: const Icon(Icons.close, size: 18),
                                    onPressed: () => setDialogState(
                                        () => envVars.remove(e.value.key)),
                                    padding: EdgeInsets.zero,
                                    constraints: const BoxConstraints(),
                                  ),
                                ],
                              ),
                            )),
                    if (envError != null) ...[
                      Align(
                        alignment: Alignment.centerLeft,
                        child: Text(envError!,
                            style: TextStyle(
                                color: Theme.of(context).colorScheme.error,
                                fontSize: 12)),
                      ),
                      const SizedBox(height: 4),
                    ],
                    Row(
                      children: [
                        Expanded(
                          child: TextField(
                            controller: envController,
                            decoration: const InputDecoration(
                              hintText: 'KEY=VALUE',
                              isDense: true,
                              border: OutlineInputBorder(),
                            ),
                            style: const TextStyle(fontSize: 13),
                            onSubmitted: (_) => tryAddEnv(setDialogState),
                          ),
                        ),
                        const SizedBox(width: 8),
                        IconButton(
                          icon: const Icon(Icons.add),
                          onPressed: () => tryAddEnv(setDialogState),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
            actions: [
              TextButton(
                onPressed: () => Navigator.pop(context),
                style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
                child: const Text('Cancel'),
              ),
              FilledButton(
                onPressed: () => submit(context, setDialogState),
                child: const Text('Save'),
              ),
            ],
          ),
        );
      },
    );

    if (saved == true) {
      await _loadWorkspaces();
    }
  }

  String _formatCreatedAt(String? raw) {
    if (raw == null || raw.isEmpty) return '';
    try {
      // Backend sends UTC datetime as "YYYY-MM-DD HH:MM:SS"
      final utc = DateTime.parse('${raw}Z');
      final local = utc.toLocal();
      final months = [
        'Jan',
        'Feb',
        'Mar',
        'Apr',
        'May',
        'Jun',
        'Jul',
        'Aug',
        'Sep',
        'Oct',
        'Nov',
        'Dec',
      ];
      final h = local.hour > 12
          ? local.hour - 12
          : (local.hour == 0 ? 12 : local.hour);
      final ampm = local.hour >= 12 ? 'PM' : 'AM';
      final min = local.minute.toString().padLeft(2, '0');
      return '${months[local.month - 1]} ${local.day}, ${local.year}'
          ' at $h:$min $ampm';
    } catch (_) {
      return raw;
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const AppBarTitle(title: 'Workspaces'),
        actions: const [
          AppBarActions(),
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
                        subtitle:
                            Text(_formatCreatedAt(ws['created_at'] as String?)),
                        trailing: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            IconButton(
                              icon: const Icon(Icons.settings_outlined),
                              onPressed: () => _editWorkspace(ws),
                            ),
                            IconButton(
                              icon: const Icon(Icons.copy_outlined),
                              onPressed: () => _duplicateWorkspace(ws),
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
