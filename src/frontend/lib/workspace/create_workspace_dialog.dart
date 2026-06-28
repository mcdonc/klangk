import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../auth/auth_service.dart';
import '../theme/colors.dart';
import 'workspace_list_page.dart' show validateMountSpec;

/// Dialog for creating a new workspace with name, image, command,
/// mounts, and environment variables.
class CreateWorkspaceDialog extends StatefulWidget {
  final AuthService auth;
  final String defaultImage;
  final List<String> allowedImages;

  const CreateWorkspaceDialog({
    super.key,
    required this.auth,
    required this.defaultImage,
    required this.allowedImages,
  });

  @override
  State<CreateWorkspaceDialog> createState() => _CreateWorkspaceDialogState();
}

class _CreateWorkspaceDialogState extends State<CreateWorkspaceDialog> {
  final _nameController = TextEditingController();
  final _cmdController = TextEditingController();
  final _mountController = TextEditingController();
  final _envController = TextEditingController();
  late String _selectedImage;
  final _mounts = <String>[];
  final _envVars = <String, String>{};
  String? _errorMessage;
  String? _mountError;
  String? _envError;

  final _labelStyle = TextStyle(
    color: KColors.textPrimary,
    fontWeight: FontWeight.bold,
  );

  @override
  void initState() {
    super.initState();
    _selectedImage = widget.defaultImage;
  }

  @override
  void dispose() {
    _nameController.dispose();
    _cmdController.dispose();
    _mountController.dispose();
    _envController.dispose();
    super.dispose();
  }

  void _tryAddMount() {
    final v = _mountController.text.trim();
    if (v.isEmpty) return;
    final err = validateMountSpec(v);
    if (err != null) {
      setState(() => _mountError = err);
      return;
    }
    setState(() {
      _mounts.add(v);
      _mountController.clear();
      _mountError = null;
    });
  }

  void _tryAddEnv() {
    final v = _envController.text.trim();
    if (v.isEmpty) return;
    final err = _validateEnvEntry(v);
    if (err != null) {
      setState(() => _envError = err);
      return;
    }
    final key = v.substring(0, v.indexOf('='));
    final value = v.substring(v.indexOf('=') + 1);
    setState(() {
      _envVars[key] = value;
      _envController.clear();
      _envError = null;
    });
  }

  static String? _validateEnvEntry(String input) {
    if (!input.contains('=')) return 'Expected KEY=VALUE format';
    final key = input.substring(0, input.indexOf('='));
    if (key.isEmpty) return 'Key cannot be empty';
    return null;
  }

  Future<void> _submit() async {
    final name = _nameController.text.trim();
    if (name.isEmpty) return;
    final command = _cmdController.text.trim();
    final body = <String, dynamic>{'name': name};
    if (command.isNotEmpty) body['default_command'] = command;
    if (_selectedImage != widget.defaultImage) {
      body['image'] = _selectedImage;
    }
    if (_mounts.isNotEmpty) body['mounts'] = List<String>.from(_mounts);
    if (_envVars.isNotEmpty) {
      body['env'] = Map<String, String>.from(_envVars);
    }

    try {
      final response = await widget.auth.authPost(
        '/api/v1/workspaces',
        body: jsonEncode(body),
      );
      if (response.statusCode == 200) {
        if (mounted) Navigator.pop(context, true);
      } else {
        final error = jsonDecode(response.body);
        setState(() {
          _errorMessage =
              error['detail'] as String? ?? 'Failed to create workspace';
        });
      }
    } catch (e) {
      setState(() => _errorMessage = 'Error: $e');
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
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
              if (_errorMessage != null) ...[
                Text(
                  _errorMessage!,
                  style: TextStyle(
                    color: Theme.of(context).colorScheme.error,
                  ),
                ),
                const SizedBox(height: 12),
              ],
              TextField(
                controller: _nameController,
                decoration: InputDecoration(
                  labelText: 'Name',
                  labelStyle: _labelStyle,
                  floatingLabelStyle: _labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
                  border: const OutlineInputBorder(),
                ),
                autofocus: true,
                onSubmitted: (_) => _submit(),
              ),
              const SizedBox(height: 16),
              DropdownButtonFormField<String>(
                value: _selectedImage,
                decoration: InputDecoration(
                  labelText: 'Container Image',
                  labelStyle: _labelStyle,
                  floatingLabelStyle: _labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
                  border: const OutlineInputBorder(),
                ),
                items: widget.allowedImages
                    .map(
                      (img) => DropdownMenuItem(value: img, child: Text(img)),
                    )
                    .toList(),
                onChanged: (v) => setState(
                  () => _selectedImage = v ?? widget.defaultImage,
                ),
              ),
              const SizedBox(height: 16),
              TextField(
                controller: _cmdController,
                decoration: InputDecoration(
                  labelText: 'Default shell command (optional)',
                  labelStyle: _labelStyle,
                  floatingLabelStyle: _labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
                  border: const OutlineInputBorder(),
                ),
                onSubmitted: (_) => _submit(),
              ),
              ..._buildMountsEditor(),
              ..._buildEnvVarsEditor(),
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
          onPressed: _submit,
          child: const Text('Create'),
        ),
      ],
    );
  }

  List<Widget> _buildMountsEditor() {
    return [
      const SizedBox(height: 16),
      Text('Mounts', style: _labelStyle),
      const SizedBox(height: 8),
      ..._mounts.asMap().entries.map(
            (e) => Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Row(
                children: [
                  Expanded(
                    child: SelectableText(
                      e.value,
                      style: const TextStyle(fontSize: 13),
                    ),
                  ),
                  IconButton(
                    icon: const Icon(Icons.copy, size: 16),
                    tooltip: 'Copy',
                    onPressed: () =>
                        Clipboard.setData(ClipboardData(text: e.value)),
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(),
                  ),
                  const SizedBox(width: 4),
                  IconButton(
                    icon: const Icon(Icons.close, size: 18),
                    onPressed: () => setState(() => _mounts.removeAt(e.key)),
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
              controller: _mountController,
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
          IconButton(icon: const Icon(Icons.add), onPressed: _tryAddMount),
        ],
      ),
    ];
  }

  List<Widget> _buildEnvVarsEditor() {
    return [
      const SizedBox(height: 16),
      Text('Environment Variables', style: _labelStyle),
      const SizedBox(height: 8),
      ..._envVars.entries.toList().asMap().entries.map(
            (e) => Padding(
              padding: const EdgeInsets.only(bottom: 4),
              child: Row(
                children: [
                  Expanded(
                    child: SelectableText(
                      '${e.value.key}=${e.value.value}',
                      style: const TextStyle(fontSize: 13),
                    ),
                  ),
                  IconButton(
                    icon: const Icon(Icons.copy, size: 16),
                    tooltip: 'Copy',
                    onPressed: () => Clipboard.setData(
                      ClipboardData(text: '${e.value.key}=${e.value.value}'),
                    ),
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(),
                  ),
                  const SizedBox(width: 4),
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
              controller: _envController,
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
          IconButton(icon: const Icon(Icons.add), onPressed: _tryAddEnv),
        ],
      ),
    ];
  }
}
