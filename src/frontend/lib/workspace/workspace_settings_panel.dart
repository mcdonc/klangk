import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../theme/colors.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
import '../ws/ws_client.dart';

/// Workspace settings panel: config editing only.
/// Used as a tab in the IDE layout.
class WorkspaceSettingsPanel extends StatefulWidget {
  final String workspaceId;

  const WorkspaceSettingsPanel({super.key, required this.workspaceId});

  @override
  State<WorkspaceSettingsPanel> createState() => WorkspaceSettingsPanelState();
}

class WorkspaceSettingsPanelState extends State<WorkspaceSettingsPanel> {
  Map<String, dynamic>? _workspace;
  List<String> _allowedImages = [];
  String _defaultImage = 'klangk-pi';
  bool _loading = true;
  String? _error;
  String? _saveMessage;

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

    final wsResp = await auth.authGet('/api/v1/workspaces');
    if (!mounted) return;

    List<Map<String, dynamic>> workspaces = [];
    if (wsResp.statusCode == 200) {
      workspaces = List<Map<String, dynamic>>.from(jsonDecode(wsResp.body));
    }

    var ws = workspaces.cast<Map<String, dynamic>?>().firstWhere(
      (w) => w!['id'] == widget.workspaceId,
      orElse: () => null,
    );

    // Try shared workspaces if not found in owned
    if (ws == null) {
      final sharedResp = await auth.authGet('/api/v1/workspaces/shared');
      if (!mounted) return;
      if (sharedResp.statusCode == 200) {
        final shared = List<Map<String, dynamic>>.from(
          jsonDecode(sharedResp.body),
        );
        ws = shared.cast<Map<String, dynamic>?>().firstWhere(
          (w) => w!['id'] == widget.workspaceId,
          orElse: () => null,
        );
      }
    }

    if (ws == null) {
      setState(() {
        _error = 'Workspace not found';
        _loading = false;
      });
      return;
    }

    _workspace = ws;

    // Load allowed images
    try {
      final imgResp = await auth.authGet('/api/v1/images');
      if (mounted && imgResp.statusCode == 200) {
        final imgData = jsonDecode(imgResp.body) as Map<String, dynamic>;
        _defaultImage = imgData['default'] as String? ?? 'klangk-pi';
        _allowedImages =
            (imgData['allowed'] as List?)?.cast<String>() ?? [_defaultImage];
      }
    } catch (e) {
      // coverage:ignore-start
      debugPrint('[WorkspaceSettingsPanel] load images failed: $e');
    } // coverage:ignore-end

    if (mounted) setState(() => _loading = false);
  }

  Future<void> _saveSettings(Map<String, dynamic> fields) async {
    final auth = context.read<AuthService>();
    final resp = await auth.authPut(
      '/api/v1/workspaces/${widget.workspaceId}',
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
      } catch (e) {
        debugPrint('[WorkspaceSettingsPanel] parse error detail failed: $e');
        detail = 'Error: ${resp.statusCode}';
      }
      setState(() => _saveMessage = 'Failed: $detail');
    }
  }

  @override
  Widget build(BuildContext context) {
    if (_loading) return const Center(child: CircularProgressIndicator());
    if (_error != null) return Center(child: Text(_error!));
    if (_workspace == null) return const Center(child: Text('No data'));

    return _SettingsForm(
      workspaceId: widget.workspaceId,
      workspace: _workspace!,
      allowedImages: _allowedImages,
      defaultImage: _defaultImage,
      saveMessage: _saveMessage,
      onSave: _saveSettings,
    );
  }
}

class _SettingsForm extends StatefulWidget {
  final String workspaceId;
  final Map<String, dynamic> workspace;
  final List<String> allowedImages;
  final String defaultImage;
  final String? saveMessage;
  final Future<void> Function(Map<String, dynamic>) onSave;

  const _SettingsForm({
    required this.workspaceId,
    required this.workspace,
    required this.allowedImages,
    required this.defaultImage,
    required this.saveMessage,
    required this.onSave,
  });

  @override
  State<_SettingsForm> createState() => _SettingsFormState();
}

class _SettingsFormState extends State<_SettingsForm> {
  late TextEditingController _nameCtrl;
  late TextEditingController _cmdCtrl;
  final _mountCtrl = TextEditingController();
  final _envCtrl = TextEditingController();
  late String _selectedImage;
  late List<String> _mounts;
  late Map<String, String> _envVars;
  String? _mountError;
  String? _envError;
  bool _saving = false;
  bool _exporting = false;

  @override
  void initState() {
    super.initState();
    _nameCtrl = TextEditingController(
      text: widget.workspace['name'] as String? ?? '',
    );
    _cmdCtrl = TextEditingController(
      text: widget.workspace['default_command'] as String? ?? '',
    );
    _selectedImage =
        widget.workspace['image'] as String? ?? widget.defaultImage;
    if (!widget.allowedImages.contains(_selectedImage)) {
      _selectedImage = widget.defaultImage;
    }
    _mounts = List<String>.from(
      (widget.workspace['mounts'] as List?)?.cast<String>() ?? <String>[],
    );
    _envVars = Map<String, String>.from(
      (widget.workspace['env'] as Map?)?.cast<String, String>() ??
          <String, String>{},
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
    _mountCtrl.dispose();
    _envCtrl.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    await widget.onSave({
      'name': _nameCtrl.text.trim(),
      'image': _selectedImage,
      'default_command': _cmdCtrl.text.trim().isEmpty
          ? null
          : _cmdCtrl.text.trim(),
      'mounts': _mounts.isNotEmpty ? _mounts : null,
      'env': _envVars.isNotEmpty ? _envVars : null,
    });
    if (mounted) setState(() => _saving = false);
  }

  void _tryAddMount() {
    final v = _mountCtrl.text.trim();
    if (v.isEmpty) return;
    if (!v.contains(':')) {
      setState(() => _mountError = 'Expected host:container format');
      return;
    }
    setState(() {
      _mounts.add(v);
      _mountCtrl.clear();
      _mountError = null;
    });
  }

  void _tryAddEnv() {
    final v = _envCtrl.text.trim();
    if (v.isEmpty) return;
    if (!v.contains('=')) {
      setState(() => _envError = 'Expected KEY=VALUE format');
      return;
    }
    final key = v.substring(0, v.indexOf('='));
    final value = v.substring(v.indexOf('=') + 1);
    if (key.isEmpty) {
      setState(() => _envError = 'Key cannot be empty');
      return;
    }
    setState(() {
      _envVars[key] = value;
      _envCtrl.clear();
      _envError = null;
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
      child: Center(
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 500),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              if (widget.saveMessage != null) ...[
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 12,
                    vertical: 8,
                  ),
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
              Container(
                padding: const EdgeInsets.all(16),
                decoration: BoxDecoration(
                  border: Border.all(color: KColors.borderDefault),
                  borderRadius: BorderRadius.circular(8),
                  color: KColors.bgSurface,
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        const Icon(
                          Icons.settings,
                          size: 18,
                          color: KColors.textSecondary,
                        ),
                        const SizedBox(width: 8),
                        const Text(
                          'Workspace Configuration',
                          style: TextStyle(
                            fontWeight: FontWeight.bold,
                            fontSize: 14,
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
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
                    if (widget.allowedImages.isNotEmpty)
                      DropdownButtonFormField<String>(
                        value: _selectedImage,
                        decoration: InputDecoration(
                          labelText: 'Container Image',
                          labelStyle: labelStyle,
                          floatingLabelBehavior: FloatingLabelBehavior.always,
                          border: const OutlineInputBorder(),
                        ),
                        items: widget.allowedImages
                            .map(
                              (img) => DropdownMenuItem(
                                value: img,
                                child: Text(img),
                              ),
                            )
                            .toList(),
                        onChanged: (v) => setState(
                          () => _selectedImage = v ?? widget.defaultImage,
                        ),
                      ),
                    const SizedBox(height: 16),
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
                    Text('Mounts', style: labelStyle),
                    const SizedBox(height: 8),
                    ..._mounts.asMap().entries.map(
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
                              onPressed: () =>
                                  setState(() => _mounts.removeAt(e.key)),
                              padding: EdgeInsets.zero,
                              constraints: const BoxConstraints(),
                            ),
                          ],
                        ),
                      ),
                    ),
                    if (_mountError != null) ...[
                      Text(
                        _mountError!,
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
                            controller: _mountCtrl,
                            decoration: const InputDecoration(
                              hintText: '/host/path:/container/path',
                              isDense: true,
                              border: OutlineInputBorder(),
                            ),
                            style: const TextStyle(fontSize: 13),
                            onSubmitted: (_) => _tryAddMount(),
                          ),
                        ),
                        const SizedBox(width: 8),
                        IconButton(
                          icon: const Icon(Icons.add),
                          onPressed: _tryAddMount,
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    Text('Environment Variables', style: labelStyle),
                    const SizedBox(height: 8),
                    ..._envVars.entries.toList().asMap().entries.map(
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
                              onPressed: () =>
                                  setState(() => _envVars.remove(e.value.key)),
                              padding: EdgeInsets.zero,
                              constraints: const BoxConstraints(),
                            ),
                          ],
                        ),
                      ),
                    ),
                    if (_envError != null) ...[
                      Text(
                        _envError!,
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
                            controller: _envCtrl,
                            decoration: const InputDecoration(
                              hintText: 'KEY=VALUE',
                              isDense: true,
                              border: OutlineInputBorder(),
                            ),
                            style: const TextStyle(fontSize: 13),
                            onSubmitted: (_) => _tryAddEnv(),
                          ),
                        ),
                        const SizedBox(width: 8),
                        IconButton(
                          icon: const Icon(Icons.add),
                          onPressed: _tryAddEnv,
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
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
                                ),
                              )
                            : const Icon(Icons.save, size: 18),
                        label: const Text('Save'),
                      ),
                    ),
                    const SizedBox(height: 32),
                    const Divider(),
                    const SizedBox(height: 16),
                    const SizedBox(height: 32),
                    const Divider(),
                    const SizedBox(height: 16),
                    Text(
                      'Export',
                      style: Theme.of(context).textTheme.titleMedium,
                    ),
                    const SizedBox(height: 12),
                    OutlinedButton.icon(
                      onPressed: _exporting ? null : _exportWorkspace,
                      icon: _exporting
                          ? const SizedBox(
                              width: 16,
                              height: 16,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Icon(Icons.download, size: 18),
                      label: const Text('Export Workspace'),
                    ),
                    const SizedBox(height: 32),
                    const Divider(),
                    const SizedBox(height: 16),
                    Text(
                      'Danger Zone',
                      style: Theme.of(
                        context,
                      ).textTheme.titleMedium?.copyWith(color: Colors.red),
                    ),
                    const SizedBox(height: 12),
                    OutlinedButton.icon(
                      onPressed: () => _confirmShutdown(context),
                      icon: const Icon(
                        Icons.power_settings_new,
                        size: 18,
                        color: Colors.red,
                      ),
                      label: const Text('Shut Down Container'),
                      style: OutlinedButton.styleFrom(
                        foregroundColor: Colors.red,
                        side: const BorderSide(color: Colors.red),
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Future<void> _exportWorkspace() async {
    setState(() => _exporting = true);
    try {
      final auth = context.read<AuthService>();
      final resp = await auth.authGet(
        '/api/v1/workspaces/${widget.workspaceId}/export',
      );
      if (resp.statusCode == 200) {
        final name = widget.workspace['name'] as String? ?? 'workspace';
        downloadBytes(resp.bodyBytes, '$name.tar.gz');
      } else {
        if (mounted) {
          ScaffoldMessenger.of(context).showSnackBar(
            SnackBar(content: Text('Export failed: ${resp.statusCode}')),
          );
        }
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('Export failed')));
      }
    } finally {
      if (mounted) setState(() => _exporting = false);
    }
  }

  void _confirmShutdown(BuildContext context) {
    showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Shut Down Container'),
        content: const Text(
          'This will stop the container and end all terminal '
          'sessions for all users in this workspace.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              Navigator.of(ctx).pop();
              context.read<WsClient>().sendShutdownContainer();
            },
            style: FilledButton.styleFrom(backgroundColor: Colors.red),
            child: const Text('Shut Down'),
          ),
        ],
      ),
    );
  }
}
