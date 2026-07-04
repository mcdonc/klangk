import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
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
      allowAutostart:
          context.select<AuthService, bool>((a) => a.allowAutostart),
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
  final bool allowAutostart;
  final String? saveMessage;
  final Future<void> Function(Map<String, dynamic>) onSave;

  const _SettingsForm({
    required this.workspaceId,
    required this.workspace,
    required this.allowedImages,
    required this.defaultImage,
    required this.allowAutostart,
    required this.saveMessage,
    required this.onSave,
  });

  @override
  State<_SettingsForm> createState() => _SettingsFormState();
}

class _SettingsFormState extends State<_SettingsForm> {
  late TextEditingController _nameCtrl;
  late TextEditingController _cmdCtrl;
  late TextEditingController _healthCheckCtrl;
  final _mountCtrl = TextEditingController();
  final _envCtrl = TextEditingController();
  late String _selectedImage;
  late List<String> _mounts;
  late Map<String, String> _envVars;
  bool _autoStart = false;
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
      text: widget.workspace['service_command'] as String? ?? '',
    );
    _healthCheckCtrl = TextEditingController(
      text: widget.workspace['health_check'] as String? ?? '',
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
    _autoStart = (widget.workspace['auto_start'] as bool?) ?? false;
  }

  @override
  void didUpdateWidget(_SettingsForm old) {
    super.didUpdateWidget(old);
    // The parent rebuilds this form with a fresh workspace map after each
    // _loadData; resync the controllers when the underlying value changed.
    if (old.workspace['name'] != widget.workspace['name']) {
      // coverage:ignore-start
      _nameCtrl.text = widget.workspace['name'] as String? ?? '';
    }
    if (old.workspace['service_command'] !=
        widget.workspace['service_command']) {
      _cmdCtrl.text = widget.workspace['service_command'] as String? ?? '';
    } // coverage:ignore-end
    if (old.workspace['health_check'] != widget.workspace['health_check']) {
      // coverage:ignore-start
      _healthCheckCtrl.text = widget.workspace['health_check'] as String? ?? '';
    } // coverage:ignore-end
    if (old.workspace['auto_start'] != widget.workspace['auto_start']) {
      // coverage:ignore-start
      _autoStart = (widget.workspace['auto_start'] as bool?) ?? false;
    } // coverage:ignore-end
    if (old.workspace['image'] != widget.workspace['image']) {
      _selectedImage =
          widget.workspace['image'] as String? ?? widget.defaultImage;
      if (!widget.allowedImages.contains(_selectedImage)) {
        _selectedImage = widget.defaultImage;
      }
    }
    if (old.workspace['mounts'] != widget.workspace['mounts']) {
      _mounts = List<String>.from(
        (widget.workspace['mounts'] as List?)?.cast<String>() ?? <String>[],
      );
    }
    if (old.workspace['env'] != widget.workspace['env']) {
      _envVars = Map<String, String>.from(
        (widget.workspace['env'] as Map?)?.cast<String, String>() ??
            <String, String>{},
      );
    }
  }

  @override
  void dispose() {
    _nameCtrl.dispose();
    _cmdCtrl.dispose();
    _healthCheckCtrl.dispose();
    _mountCtrl.dispose();
    _envCtrl.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    await widget.onSave({
      'name': _nameCtrl.text.trim(),
      'image': _selectedImage,
      'service_command':
          _cmdCtrl.text.trim().isEmpty ? null : _cmdCtrl.text.trim(),
      'health_check': _healthCheckCtrl.text.trim().isEmpty
          ? null
          : _healthCheckCtrl.text.trim(),
      'mounts': _mounts.isNotEmpty ? _mounts : null,
      'env': _envVars.isNotEmpty ? _envVars : null,
      if (widget.allowAutostart) 'auto_start': _autoStart,
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
                _buildSaveMessage(),
                const SizedBox(height: 16),
              ],
              _buildConfigCard(labelStyle),
              const SizedBox(height: 16),
              _buildExportCard(),
              const SizedBox(height: 16),
              _buildDangerZoneCard(),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildSaveMessage() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: widget.saveMessage!.startsWith('Failed')
            ? KColors.accentRed.withValues(alpha: 0.1)
            : KColors.accentGreen.withValues(alpha: 0.1),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Text(widget.saveMessage!),
    );
  }

  /// A titled surface card used to group related controls.
  Widget _card({
    required IconData icon,
    required String title,
    Color? titleColor,
    required List<Widget> children,
  }) {
    return Container(
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
              Icon(icon, size: 18, color: titleColor ?? KColors.textSecondary),
              const SizedBox(width: 8),
              Text(
                title,
                style: TextStyle(
                  fontWeight: FontWeight.bold,
                  fontSize: 14,
                  color: titleColor,
                ),
              ),
            ],
          ),
          const SizedBox(height: 16),
          ...children,
        ],
      ),
    );
  }

  Widget _buildConfigCard(TextStyle labelStyle) {
    return _card(
      icon: Icons.settings,
      title: 'Workspace Configuration',
      children: [
        TextField(
          controller: _nameCtrl,
          decoration: InputDecoration(
            labelText: 'Name',
            labelStyle: labelStyle,
            floatingLabelBehavior: FloatingLabelBehavior.always,
            border: const OutlineInputBorder(),
          ),
        ),
        const SizedBox(height: 16),
        _buildMountsEditor(labelStyle),
        const SizedBox(height: 16),
        _buildEnvVarsEditor(labelStyle),
        const SizedBox(height: 16),
        TextField(
          controller: _cmdCtrl,
          decoration: InputDecoration(
            labelText: 'Service Shell Command',
            labelStyle: labelStyle,
            floatingLabelBehavior: FloatingLabelBehavior.always,
            border: const OutlineInputBorder(),
            hintText: 'Optional — runs on terminal open',
          ),
        ),
        const SizedBox(height: 16),
        TextField(
          controller: _healthCheckCtrl,
          decoration: InputDecoration(
            labelText: 'Health Check Command',
            labelStyle: labelStyle,
            floatingLabelBehavior: FloatingLabelBehavior.always,
            border: const OutlineInputBorder(),
            hintText: 'Optional — polled to gauge service health',
          ),
        ),
        const SizedBox(height: 16),
        if (widget.allowedImages.isNotEmpty)
          DropdownButtonFormField<String>(
            initialValue: _selectedImage,
            decoration: InputDecoration(
              labelText: 'Container Image',
              labelStyle: labelStyle,
              floatingLabelBehavior: FloatingLabelBehavior.always,
              border: const OutlineInputBorder(),
            ),
            items: widget.allowedImages
                .map((img) => DropdownMenuItem(value: img, child: Text(img)))
                .toList(),
            onChanged: (v) =>
                setState(() => _selectedImage = v ?? widget.defaultImage),
          ),
        if (widget.allowAutostart) ...[
          const SizedBox(height: 8),
          // Wrap in a transparent Material so the CheckboxListTile's ink
          // splash paints above this card's opaque background surface
          // (the _card() Container would otherwise hide it).
          Material(
            type: MaterialType.transparency,
            child: CheckboxListTile(
              value: _autoStart,
              onChanged: (v) => setState(() => _autoStart = v ?? false),
              title: const Text('Auto start'),
              subtitle: const Text(
                'Start this workspace when the server starts',
              ),
              controlAffinity: ListTileControlAffinity.leading,
              contentPadding: EdgeInsets.zero,
            ),
          ),
        ],
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
      ],
    );
  }

  Widget _buildMountsEditor(TextStyle labelStyle) {
    return _buildEditableList(
      label: 'Mounts',
      labelStyle: labelStyle,
      hint: '/host/path:/container/path',
      controller: _mountCtrl,
      error: _mountError,
      onAdd: _tryAddMount,
      items: _mounts.asMap().entries.map(
            (e) => _buildEditableListItem(
              text: e.value,
              onCopy: e.value,
              onRemove: () => setState(() => _mounts.removeAt(e.key)),
            ),
          ),
    );
  }

  Widget _buildEnvVarsEditor(TextStyle labelStyle) {
    return _buildEditableList(
      label: 'Environment Variables',
      labelStyle: labelStyle,
      hint: 'KEY=VALUE',
      controller: _envCtrl,
      error: _envError,
      onAdd: _tryAddEnv,
      items: _envVars.entries.toList().asMap().entries.map(
            (e) => _buildEditableListItem(
              text: '${e.value.key}=${e.value.value}',
              onCopy: '${e.value.key}=${e.value.value}',
              onRemove: () => setState(() => _envVars.remove(e.value.key)),
            ),
          ),
    );
  }

  /// A list of editable text items (mounts / env vars) with an add row and
  /// inline error. The two editors only differ in label, hint, and the
  /// item text/remove callback.
  Widget _buildEditableList({
    required String label,
    required TextStyle labelStyle,
    required String hint,
    required TextEditingController controller,
    required String? error,
    required void Function() onAdd,
    required Iterable<Widget> items,
  }) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: labelStyle),
        const SizedBox(height: 8),
        ...items,
        if (error != null) ...[
          Text(
            error,
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
                controller: controller,
                decoration: InputDecoration(
                  hintText: hint,
                  isDense: true,
                  border: const OutlineInputBorder(),
                ),
                style: const TextStyle(fontSize: 13),
                onSubmitted: (_) => onAdd(),
              ),
            ),
            const SizedBox(width: 8),
            IconButton(icon: const Icon(Icons.add), onPressed: onAdd),
          ],
        ),
      ],
    );
  }

  /// One row of an editable list: the text, a copy button, and a remove
  /// button. ``onCopy`` is the exact string placed on the clipboard.
  Widget _buildEditableListItem({
    required String text,
    required String onCopy,
    required void Function() onRemove,
  }) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 4),
      child: Row(
        children: [
          Expanded(
            child: SelectableText(text, style: const TextStyle(fontSize: 13)),
          ),
          IconButton(
            icon: const Icon(Icons.copy, size: 16),
            tooltip: 'Copy',
            onPressed: () => Clipboard.setData(ClipboardData(text: onCopy)),
            padding: EdgeInsets.zero,
            constraints: const BoxConstraints(),
          ),
          const SizedBox(width: 4),
          IconButton(
            icon: const Icon(Icons.close, size: 18),
            onPressed: onRemove,
            padding: EdgeInsets.zero,
            constraints: const BoxConstraints(),
          ),
        ],
      ),
    );
  }

  Widget _buildExportCard() {
    return _card(
      icon: Icons.download,
      title: 'Export',
      children: [
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
      ],
    );
  }

  Widget _buildDangerZoneCard() {
    return _card(
      icon: Icons.warning_amber,
      title: 'Danger Zone',
      titleColor: Colors.red,
      children: [
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
    );
  }

  Future<void> _exportWorkspace() async {
    setState(() => _exporting = true);
    try {
      final auth = context.read<AuthService>();
      final name = widget.workspace['name'] as String? ?? 'workspace';
      final filename = '$name.tar.gz';
      final exportPath = '/api/v1/workspaces/${widget.workspaceId}/export';

      // Prefer streaming straight to disk (no in-memory buffering) when the
      // browser supports the File System Access API. Falls back to the
      // buffered path on Firefox/Safari/older browsers (#700).
      final streamed = await downloadStreamedUrl(
        exportPath,
        filename: filename,
        headers: auth.authHeaders,
      );
      if (!streamed) {
        final resp = await auth.authGet(exportPath);
        if (resp.statusCode == 200) {
          downloadBytes(resp.bodyBytes, filename);
        } else {
          if (mounted) {
            ScaffoldMessenger.of(context).showSnackBar(
              SnackBar(content: Text('Export failed: ${resp.statusCode}')),
            );
          }
        }
      }
    } catch (_) {
      // coverage:ignore-start
      if (mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(const SnackBar(content: Text('Export failed')));
      }
    } finally {
      // coverage:ignore-end
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
