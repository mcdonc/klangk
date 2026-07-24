import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../auth/auth_service.dart';
import '../theme/colors.dart';
import 'workspace_list_page.dart'
    show validateMountSpec, validateAllowedDomainSpec;

/// Dialog for creating a new workspace. Fields, top to bottom:
/// Name, Mounts, Environment Variables, Service shell command, Health
/// check command, Container Image, and (when the server permits
/// auto-start) an Auto start checkbox. The same field set and order is
/// used by the Workspace Configuration card in the settings panel.
class CreateWorkspaceDialog extends StatefulWidget {
  final AuthService auth;
  final String defaultImage;
  final List<String> allowedImages;

  /// Whether to render the Auto start checkbox. The caller derives this
  /// from AuthService.allowAutostart (server's KLANGKD_ALLOW_AUTOSTART).
  final bool allowAutostart;

  /// #1365: deploy-wide netfilter default allow-list, surfaced via
  /// /api/v1/config (KLANGKD_NETFILTER_DEFAULT_DOMAINS). The editor is
  /// pre-filled with this so a new workspace inherits the deployer's floor;
  /// the creator's edits replace it (stored as the workspace's own
  /// allowed_domains). Empty when netfilter is unset/disabled on the server.
  final List<String> defaultAllowedDomains;

  /// #1365: whether netfilter is armed on the server. When false, the
  /// allowed-domains editor shows a "not enforced" notice so the creator
  /// knows the list won't take effect until an operator enables netfilter.
  final bool netfilterEnabled;

  const CreateWorkspaceDialog({
    super.key,
    required this.auth,
    required this.defaultImage,
    required this.allowedImages,
    this.allowAutostart = false,
    this.defaultAllowedDomains = const [],
    this.netfilterEnabled = false,
  });

  @override
  State<CreateWorkspaceDialog> createState() => _CreateWorkspaceDialogState();
}

class _CreateWorkspaceDialogState extends State<CreateWorkspaceDialog> {
  final _nameController = TextEditingController();
  final _cmdController = TextEditingController();
  final _healthCheckController = TextEditingController();
  final _mountController = TextEditingController();
  final _envController = TextEditingController();
  final _allowedDomainsController = TextEditingController();
  late String _selectedImage;
  final _mounts = <String>[];
  final _envVars = <String, String>{};
  final _allowedDomains = <String>[];
  bool _autoStart = false;
  String? _errorMessage;
  String? _mountError;
  String? _envError;
  String? _allowedDomainsError;

  final _labelStyle = TextStyle(
    color: KColors.textPrimary,
    fontWeight: FontWeight.bold,
  );

  @override
  void initState() {
    super.initState();
    _selectedImage = widget.defaultImage;
    // #1365: pre-fill the editor with the deploy-wide default so a new
    // workspace inherits it; the creator's edits replace (not merge with)
    // the default and are submitted as the workspace's allowed_domains.
    _allowedDomains.addAll(widget.defaultAllowedDomains);
  }

  @override
  void dispose() {
    _nameController.dispose();
    _cmdController.dispose();
    _healthCheckController.dispose();
    _mountController.dispose();
    _envController.dispose();
    _allowedDomainsController.dispose();
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

  void _tryAddAllowedDomain() {
    final v = _allowedDomainsController.text.trim();
    if (v.isEmpty) return;
    final err = validateAllowedDomainSpec(v);
    if (err != null) {
      setState(() => _allowedDomainsError = err);
      return;
    }
    setState(() {
      if (!_allowedDomains.contains(v)) _allowedDomains.add(v);
      _allowedDomainsController.clear();
      _allowedDomainsError = null;
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
    final healthCheck = _healthCheckController.text.trim();
    final body = <String, dynamic>{'name': name};
    if (command.isNotEmpty) body['service_command'] = command;
    if (_selectedImage != widget.defaultImage) {
      body['image'] = _selectedImage;
    }
    if (healthCheck.isNotEmpty) body['health_check'] = healthCheck;
    if (_mounts.isNotEmpty) body['mounts'] = List<String>.from(_mounts);
    if (_envVars.isNotEmpty) {
      body['env'] = Map<String, String>.from(_envVars);
    }
    if (_allowedDomains.isNotEmpty) {
      body['allowed_domains'] = List<String>.from(_allowedDomains);
    }
    if (widget.allowAutostart && _autoStart) {
      body['auto_start'] = true;
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
      debugPrint('Create workspace error: $e');
      setState(
        () => _errorMessage = 'Could not create workspace. Please try again.',
      );
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
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
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
              ..._buildMountsEditor(),
              ..._buildEnvVarsEditor(),
              ..._buildAllowedDomainsEditor(),
              const SizedBox(height: 16),
              TextField(
                controller: _cmdController,
                decoration: InputDecoration(
                  labelText: 'Service Shell Command',
                  labelStyle: _labelStyle,
                  floatingLabelStyle: _labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
                  border: const OutlineInputBorder(),
                  hintText: 'Optional — runs on terminal open',
                ),
                onSubmitted: (_) => _submit(),
              ),
              const SizedBox(height: 16),
              TextField(
                controller: _healthCheckController,
                decoration: InputDecoration(
                  labelText: 'Health Check Command',
                  labelStyle: _labelStyle,
                  floatingLabelStyle: _labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
                  border: const OutlineInputBorder(),
                  hintText: 'Optional — polled to gauge service health',
                ),
                onSubmitted: (_) => _submit(),
              ),
              const SizedBox(height: 16),
              DropdownButtonFormField<String>(
                initialValue: _selectedImage,
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
                onChanged: (v) =>
                    setState(() => _selectedImage = v ?? widget.defaultImage),
              ),
              if (widget.allowAutostart) ...[
                const SizedBox(height: 8),
                CheckboxListTile(
                  value: _autoStart,
                  onChanged: (v) => setState(() => _autoStart = v ?? false),
                  title: const Text('Auto start'),
                  subtitle: const Text(
                    'Start this workspace when the server starts',
                  ),
                  controlAffinity: ListTileControlAffinity.leading,
                  contentPadding: EdgeInsets.zero,
                ),
              ],
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
        FilledButton(onPressed: _submit, child: const Text('Create')),
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

  List<Widget> _buildAllowedDomainsEditor() {
    return [
      const SizedBox(height: 16),
      Text('Allowed Domains', style: _labelStyle),
      const SizedBox(height: 8),
      ..._allowedDomains.asMap().entries.map(
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
                    onPressed: () =>
                        setState(() => _allowedDomains.removeAt(e.key)),
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(),
                  ),
                ],
              ),
            ),
          ),
      if (_allowedDomainsError != null) ...[
        Text(
          _allowedDomainsError!,
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
              controller: _allowedDomainsController,
              decoration: const InputDecoration(
                hintText: 'github.com:443',
                isDense: true,
                border: OutlineInputBorder(),
              ),
              style: const TextStyle(fontSize: 13),
              onSubmitted: (_) => _tryAddAllowedDomain(),
            ),
          ),
          const SizedBox(width: 8),
          IconButton(
              icon: const Icon(Icons.add), onPressed: _tryAddAllowedDomain),
        ],
      ),
      if (_allowedDomains.isNotEmpty && !widget.netfilterEnabled) ...[
        const SizedBox(height: 8),
        Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            color: Colors.amber.withValues(alpha: 0.12),
            borderRadius: BorderRadius.circular(4),
          ),
          child: const Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Icon(Icons.warning_amber, size: 18),
              SizedBox(width: 8),
              Expanded(
                child: Text(
                  'Egress filtering is not active on this server — the '
                  'allowed-domains list will NOT be enforced until an '
                  'operator enables netfilter.',
                ),
              ),
            ],
          ),
        ),
      ],
    ];
  }
}
