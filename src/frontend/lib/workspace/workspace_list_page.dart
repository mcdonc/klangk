import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import '../theme/colors.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import '../utils/page_title.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
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
  'delegated',
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
  List<Map<String, dynamic>> _sharedWorkspaces = [];
  Map<String, List<Map<String, dynamic>>> _workspaceMembers = {};
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
      final response = await _auth.authGet('/api/v1/workspaces');
      if (response.statusCode == 200) {
        final data = jsonDecode(response.body) as List;
        final workspaces = data.cast<Map<String, dynamic>>();
        // Fetch members for each workspace in parallel
        final members = <String, List<Map<String, dynamic>>>{};
        await Future.wait(
          workspaces.map((ws) async {
            final id = ws['id'] as String;
            try {
              final resp =
                  await _auth.authGet('/api/v1/workspaces/$id/members');
              if (resp.statusCode == 200) {
                members[id] = List<Map<String, dynamic>>.from(
                  jsonDecode(resp.body) as List,
                );
              }
            } catch (e) {
              // coverage:ignore-start
              debugPrint('[WorkspaceListPage] fetch members failed: $e');
            } // coverage:ignore-end
          }),
        );
        // Fetch shared workspaces
        List<Map<String, dynamic>> shared = [];
        try {
          final sharedResp = await _auth.authGet('/api/v1/workspaces/shared');
          if (sharedResp.statusCode == 200) {
            shared = List<Map<String, dynamic>>.from(
              jsonDecode(sharedResp.body) as List,
            );
          }
        } catch (e) {
          // coverage:ignore-start
          debugPrint('[WorkspaceListPage] fetch shared workspaces failed: $e');
        } // coverage:ignore-end
        setState(() {
          _workspaces = workspaces;
          _sharedWorkspaces = shared;
          _workspaceMembers = members;
        });
      }
    } catch (e) {
      debugPrint('Failed to load workspaces: $e');
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(
            duration: Duration(days: 1),
            showCloseIcon: true,
            content: Text('Failed to load workspaces'),
          ),
        );
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  Future<Map<String, dynamic>?> _fetchImages() async {
    try {
      final response = await _auth.authGet('/api/v1/images');
      if (response.statusCode == 200) {
        return jsonDecode(response.body) as Map<String, dynamic>;
      }
    } catch (e) {
      // coverage:ignore-start
      debugPrint('[WorkspaceListPage] fetch images failed: $e');
    } // coverage:ignore-end
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
          BuildContext ctx,
          void Function(void Function()) setState,
        ) async {
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
              '/api/v1/workspaces',
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
            title: Text(
              'New Workspace',
              style: TextStyle(color: KColors.textPrimary),
            ),
            content: SizedBox(
              width: 400,
              child: SingleChildScrollView(
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (errorMessage != null) ...[
                      Text(
                        errorMessage!,
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.error,
                        ),
                      ),
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
                          .map(
                            (img) =>
                                DropdownMenuItem(value: img, child: Text(img)),
                          )
                          .toList(),
                      onChanged: (v) => setDialogState(
                        () => selectedImage = v ?? defaultImage,
                      ),
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
                    ...mounts.asMap().entries.map(
                          (e) => Padding(
                            padding: const EdgeInsets.only(bottom: 4),
                            child: Row(
                              children: [
                                Expanded(
                                  child: Text(
                                    e.value,
                                    style: const TextStyle(fontSize: 13),
                                  ),
                                ),
                                IconButton(
                                  icon: const Icon(Icons.close, size: 18),
                                  onPressed: () => setDialogState(
                                      () => mounts.removeAt(e.key)),
                                  padding: EdgeInsets.zero,
                                  constraints: const BoxConstraints(),
                                ),
                              ],
                            ),
                          ),
                        ),
                    if (mountError != null) ...[
                      Text(
                        mountError!,
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.error,
                          fontSize: 12,
                        ),
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
                    Text('Environment Variables', style: labelStyle),
                    const SizedBox(height: 8),
                    ...envVars.entries.toList().asMap().entries.map(
                          (e) => Padding(
                            padding: const EdgeInsets.only(bottom: 4),
                            child: Row(
                              children: [
                                Expanded(
                                  child: Text(
                                    '${e.value.key}=${e.value.value}',
                                    style: const TextStyle(fontSize: 13),
                                  ),
                                ),
                                IconButton(
                                  icon: const Icon(Icons.close, size: 18),
                                  onPressed: () => setDialogState(
                                    () => envVars.remove(e.value.key),
                                  ),
                                  padding: EdgeInsets.zero,
                                  constraints: const BoxConstraints(),
                                ),
                              ],
                            ),
                          ),
                        ),
                    if (envError != null) ...[
                      Text(
                        envError!,
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.error,
                          fontSize: 12,
                        ),
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
              OutlinedButton.icon(
                onPressed: () =>
                    _importWorkspace(context), // coverage:ignore-line
                icon: const Icon(Icons.upload, size: 18),
                label: const Text('Import'),
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

  // coverage:ignore-start
  Future<void> _importWorkspace(BuildContext dialogContext) async {
    final bytes = await pickFileBytes(accept: '.tar.gz,.tgz');
    if (bytes == null) return;

    try {
      final request = http.MultipartRequest(
        'POST',
        Uri.parse('$baseUrl/api/v1/workspaces/import'),
      );
      request.headers['Authorization'] = 'Bearer ${_auth.token}';
      request.files.add(http.MultipartFile.fromBytes(
        'file',
        bytes,
        filename: 'workspace.tar.gz',
      ));
      final streamed = await request.send();
      final resp = await http.Response.fromStream(streamed);
      if (resp.statusCode == 200 || resp.statusCode == 201) {
        if (dialogContext.mounted) Navigator.pop(dialogContext, true);
      } else {
        String detail;
        try {
          detail = (jsonDecode(resp.body) as Map)['detail'] as String? ??
              '${resp.statusCode}';
        } catch (_) {
          detail = '${resp.statusCode}';
        }
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('Import failed: $detail')),
          );
        }
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Import failed')),
        );
      }
    }
  }
  // coverage:ignore-end

  Future<void> _deleteWorkspace(String id) async {
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: const Text('Delete Workspace'),
        content: const Text(
          'This will delete the workspace and all its files. Continue?',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            style: TextButton.styleFrom(foregroundColor: KColors.accentRed),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            style: FilledButton.styleFrom(
              backgroundColor: KColors.accentRed,
              foregroundColor: Colors.white,
            ),
            child: const Text('Delete'),
          ),
        ],
      ),
    );

    if (confirmed != true) return;

    try {
      await _auth.authDelete('/api/v1/workspaces/$id');
      await _loadWorkspaces();
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
    } catch (e) {
      debugPrint('[WorkspaceListPage] format date failed: $e');
      return raw;
    }
  }

  Widget _buildWorkspacesList() {
    if (_loading) {
      return const Center(child: CircularProgressIndicator());
    }
    if (_workspaces.isEmpty && _sharedWorkspaces.isEmpty) {
      return const Center(
        child: Text('No workspaces yet. Create one to get started.'),
      );
    }
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        if (_workspaces.isNotEmpty)
          Container(
            margin: const EdgeInsets.only(bottom: 16),
            decoration: BoxDecoration(
              border: Border.all(color: KColors.borderDefault),
              borderRadius: BorderRadius.circular(8),
              color: KColors.bgSurface,
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
                  child: Row(
                    children: [
                      const Icon(
                        Icons.folder,
                        size: 18,
                        color: KColors.textSecondary,
                      ),
                      const SizedBox(width: 8),
                      Text(
                        'Owned by Me',
                        style: TextStyle(
                          fontSize: 14,
                          fontWeight: FontWeight.bold,
                          color: KColors.textPrimary,
                        ),
                      ),
                    ],
                  ),
                ),
                ..._workspaces.asMap().entries.map((e) {
                  final i = e.key;
                  final ws = e.value;
                  final wsMembers = _workspaceMembers[ws['id'] as String] ?? [];
                  // Material (not a plain ColoredBox/Container color) so the
                  // ListTile paints its background and ink splashes on this
                  // surface; Flutter 3.44+ asserts when a ListTile's nearest
                  // ancestor with a background is a ColoredBox.
                  return Material(
                    color: i.isEven
                        ? Colors.white.withValues(alpha: 0.03)
                        : Colors.transparent,
                    child: ListTile(
                      leading: const Icon(
                        Icons.terminal,
                        size: 20,
                        color: KColors.accentGreen,
                      ),
                      title: Text(ws['name'] as String),
                      subtitle: Row(
                        children: [
                          Text(_formatCreatedAt(ws['created_at'] as String?)),
                          if (wsMembers.isNotEmpty) ...[
                            const SizedBox(width: 8),
                            ...wsMembers.map((m) {
                              final email = m['email'] as String;
                              final letter = email.isNotEmpty
                                  ? email[0].toUpperCase()
                                  : '?';
                              return Padding(
                                padding: const EdgeInsets.only(right: 2),
                                child: Tooltip(
                                  message: email,
                                  child: CircleAvatar(
                                    radius: 10,
                                    backgroundColor: KColors.colorForString(
                                      email,
                                    ),
                                    child: Text(
                                      letter,
                                      style: const TextStyle(
                                        fontSize: 10,
                                        color: Colors.white,
                                        fontWeight: FontWeight.bold,
                                      ),
                                    ),
                                  ),
                                ),
                              );
                            }),
                          ],
                        ],
                      ),
                      trailing: IconButton(
                        icon: const Icon(Icons.delete_outline),
                        tooltip: 'Delete workspace',
                        onPressed: () => _deleteWorkspace(ws['id'] as String),
                      ),
                      onTap: () => context.go('/workspace/${ws['id']}'),
                    ),
                  );
                }),
              ],
            ),
          ),
        if (_sharedWorkspaces.isNotEmpty)
          Container(
            decoration: BoxDecoration(
              border: Border.all(color: KColors.borderDefault),
              borderRadius: BorderRadius.circular(8),
              color: KColors.bgSurface,
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 8),
                  child: Row(
                    children: [
                      const Icon(
                        Icons.folder_shared,
                        size: 18,
                        color: KColors.textSecondary,
                      ),
                      const SizedBox(width: 8),
                      Text(
                        'Shared with Me',
                        style: TextStyle(
                          fontSize: 14,
                          fontWeight: FontWeight.bold,
                          color: KColors.textPrimary,
                        ),
                      ),
                    ],
                  ),
                ),
                ..._sharedWorkspaces.asMap().entries.map(
                      (e) => Material(
                        color: e.key.isEven
                            ? Colors.white.withValues(alpha: 0.03)
                            : Colors.transparent,
                        child: ListTile(
                          leading: const Icon(
                            Icons.terminal,
                            size: 20,
                            color: KColors.accentBlue,
                          ),
                          title: Text(e.value['name'] as String),
                          subtitle: Text(
                            '${e.value['owner_email']} · ${_formatCreatedAt(e.value['created_at'] as String?)}',
                          ),
                          // coverage:ignore-start
                          onTap: () =>
                              context.go('/workspace/${e.value['id']}'),
                          // coverage:ignore-end
                        ),
                      ),
                    ),
              ],
            ),
          ),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const AppBarTitle(title: 'Workspaces'),
        actions: const [AppBarActions()],
      ),
      floatingActionButton:
          context.watch<AuthService>().hasPermission('/workspaces', 'create')
              ? FloatingActionButton(
                  onPressed: _createWorkspace,
                  child: const Icon(Icons.add),
                )
              : null,
      body: _buildWorkspacesList(),
    );
  }
}
