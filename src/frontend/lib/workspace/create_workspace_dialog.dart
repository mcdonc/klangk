import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import '../auth/auth_service.dart';
import '../theme/colors.dart';
import 'marking_banner.dart' show classificationBannerMaxLength;
import 'workspace_list_page.dart'
    show validateMountSpec, validateAllowedDomainSpec;
import 'workspace_section_nav.dart';

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

  /// #2202: whether the server can serve the per-workspace nix /nix snapshot
  /// (btrfs seed configured). When false the nix toggle is hidden; nix is then
  /// image-only (the user picks the nix image themselves).
  final bool nixAvailable;

  /// #2017: whether the deploy allows sudo at all (KLANGKD_ALLOW_SUDO).
  /// The per-workspace knob may only lock a workspace down below that
  /// (the deploy setting is a ceiling), so the toggle is hidden when the
  /// deploy forbids sudo — it could only ever be a no-op.
  final bool sudoAvailable;

  /// #2721: deploy default home layout (KLANGKD_PER_HANDLE_HOME). The
  /// Per-handle home checkbox starts on this, so an untouched form
  /// submits exactly what a silent POST would get. Null = unknown (old
  /// server / fetch failure): the toggle is hidden and the field omitted,
  /// so the server applies its own default (#2737 review).
  final bool? defaultPerHandleHome;

  const CreateWorkspaceDialog({
    super.key,
    required this.auth,
    required this.defaultImage,
    required this.allowedImages,
    this.allowAutostart = false,
    this.defaultAllowedDomains = const [],
    this.netfilterEnabled = false,
    this.nixAvailable = false,
    this.defaultPerHandleHome,
    this.sudoAvailable = false,
  });

  @override
  State<CreateWorkspaceDialog> createState() => _CreateWorkspaceDialogState();
}

class _CreateWorkspaceDialogState extends State<CreateWorkspaceDialog> {
  final _nameController = TextEditingController();
  final _cmdController = TextEditingController();
  final _healthCheckController = TextEditingController();
  // #2768: free-text classification marking; empty = the server applies
  // the deploy default (KLANGKD_CLASSIFICATION_BANNER).
  final _classificationBannerController = TextEditingController();
  final _mountController = TextEditingController();
  final _envController = TextEditingController();
  final _allowedDomainsController = TextEditingController();
  final _rejectedDomainsController = TextEditingController();
  final _idleTimeoutController = TextEditingController();
  final _cpuLimitController = TextEditingController();
  final _memoryLimitController = TextEditingController();
  final _pidsLimitController = TextEditingController();
  final _tmpSizeController = TextEditingController();
  late String _selectedImage;
  final _mounts = <String>[];
  final _envVars = <String, String>{};
  final _allowedDomains = <String>[];
  final _rejectedDomains = <String>[];
  bool _autoStart = false;
  bool _nixEnabled = false;

  // #2017: per-workspace sudo posture. Starts unchecked = locked down
  // (#3046); checking opts this workspace in (the deploy setting stays
  // the ceiling). Only sent when unchecked (True is the bag's default).
  bool _sudoEnabled = false;
  // #2721: home layout. Starts on the deploy default when known; null
  // (unknown) hides the toggle and omits the field.
  bool? _perHandleHome;
  // #2409: per-workspace egress mode. 'interactive' is the server default for
  // new workspaces (consent-gated egress on out of the box).
  String _egressMode = 'interactive';
  String? _errorMessage;
  String? _mountError;
  String? _envError;
  String? _allowedDomainsError;
  String? _rejectedDomainsError;

  // Section anchors for the section-nav strip (#2229): each key is attached
  // to the pane that opens the section so tapping a nav label scrolls it
  // into view via Scrollable.ensureVisible.
  final _generalKey = GlobalKey();
  final _mountsKey = GlobalKey();
  final _envKey = GlobalKey();
  final _netfilterKey = GlobalKey();
  final _resourcesKey = GlobalKey();
  final _advancedKey = GlobalKey();

  final _labelStyle = TextStyle(
    color: KColors.textPrimary,
    fontWeight: FontWeight.bold,
  );

  @override
  void initState() {
    super.initState();
    _selectedImage = widget.defaultImage;
    _perHandleHome = widget.defaultPerHandleHome;
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
    _classificationBannerController.dispose();
    _mountController.dispose();
    _envController.dispose();
    _allowedDomainsController.dispose();
    _rejectedDomainsController.dispose();
    _idleTimeoutController.dispose();
    _cpuLimitController.dispose();
    _memoryLimitController.dispose();
    _pidsLimitController.dispose();
    _tmpSizeController.dispose();
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

  void _tryAddRejectedDomain() {
    final v = _rejectedDomainsController.text.trim();
    if (v.isEmpty) return;
    // CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
    final err = validateAllowedDomainSpec(v, allowCidr: false);
    if (err != null) {
      setState(() => _rejectedDomainsError = err);
      return;
    }
    setState(() {
      if (!_rejectedDomains.contains(v)) _rejectedDomains.add(v);
      _rejectedDomainsController.clear();
      _rejectedDomainsError = null;
    });
  }

  static String? _validateEnvEntry(String input) {
    if (!input.contains('=')) return 'Expected KEY=VALUE format';
    final key = input.substring(0, input.indexOf('='));
    if (key.isEmpty) return 'Key cannot be empty';
    return null;
  }

  Map<String, dynamic> _collectSettings() {
    final s = <String, dynamic>{};
    final idle = _idleTimeoutController.text.trim();
    if (idle.isNotEmpty) s['idle_timeout'] = int.parse(idle);
    final cpu = _cpuLimitController.text.trim();
    if (cpu.isNotEmpty) s['cpu_limit'] = double.parse(cpu);
    final mem = _memoryLimitController.text.trim();
    if (mem.isNotEmpty) s['memory_limit'] = mem;
    final pids = _pidsLimitController.text.trim();
    if (pids.isNotEmpty) s['pids_limit'] = int.parse(pids);
    final tmp = _tmpSizeController.text.trim();
    if (tmp.isNotEmpty) s['tmp_size'] = tmp;
    return s;
  }

  Future<void> _submit() async {
    final name = _nameController.text.trim();
    if (name.isEmpty) return;
    final command = _cmdController.text.trim();
    final healthCheck = _healthCheckController.text.trim();
    final body = <String, dynamic>{'name': name};
    if (command.isNotEmpty) body['service_command'] = command;
    final classificationBanner = _classificationBannerController.text.trim();
    if (classificationBanner.isNotEmpty) {
      body['classification_banner'] = classificationBanner;
    }
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
    if (_rejectedDomains.isNotEmpty) {
      body['rejected_domains'] = List<String>.from(_rejectedDomains);
    }
    body['egress_mode'] = _egressMode;
    // #2721: sent only when the deploy default was known — the toggle's
    // initial state IS that default, so an untouched form submits it
    // unchanged. Unknown: omitted, and the server applies its own.
    if (_perHandleHome != null) body['per_handle_home'] = _perHandleHome!;
    if (widget.allowAutostart && _autoStart) {
      body['auto_start'] = true;
    }
    final settings = _collectSettings();
    if (widget.nixAvailable && _nixEnabled) settings['nix'] = true;
    if (widget.sudoAvailable && !_sudoEnabled) {
      settings['allow_sudo'] = false;
    }
    if (settings.isNotEmpty) body['settings'] = settings;

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
        width: 1040,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (_errorMessage != null) ...[
              Text(
                _errorMessage!,
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
              const SizedBox(height: 12),
            ],
            WorkspaceSectionNav(
              sections: [
                WorkspaceSection('General', _generalKey),
                WorkspaceSection('Mounts', _mountsKey),
                WorkspaceSection('Environment', _envKey),
                WorkspaceSection('Netfilter', _netfilterKey),
                WorkspaceSection('Resources', _resourcesKey),
                WorkspaceSection('Advanced', _advancedKey),
              ],
            ),
            Expanded(
              child: SingleChildScrollView(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    WorkspaceSectionPane(
                      key: _generalKey,
                      icon: Icons.tune,
                      title: 'General',
                      children: [
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
                        if (widget.allowAutostart) ...[
                          const SizedBox(height: 8),
                          // Wrap in a transparent Material so the
                          // CheckboxListTile's ink splash paints above the pane's
                          // opaque background surface.
                          Material(
                            type: MaterialType.transparency,
                            child: CheckboxListTile(
                              value: _autoStart,
                              onChanged: (v) =>
                                  setState(() => _autoStart = v ?? false),
                              title: const Text('Auto start'),
                              subtitle: const Text(
                                'Start this workspace when the server starts',
                              ),
                              controlAffinity: ListTileControlAffinity.leading,
                              contentPadding: EdgeInsets.zero,
                            ),
                          ),
                        ],
                        // #2017/#3046: per-workspace sudo posture (only
                        // when the deploy allows sudo — it's a ceiling,
                        // so the toggle can only opt in below it).
                        if (widget.sudoAvailable) ...[
                          const SizedBox(height: 16),
                          Material(
                            type: MaterialType.transparency,
                            child: CheckboxListTile(
                              value: _sudoEnabled,
                              onChanged: (v) =>
                                  setState(() => _sudoEnabled = v ?? false),
                              title: const Text('Allow sudo'),
                              subtitle: const Text(
                                'Check to allow passwordless sudo '
                                '(off by default); applies at the next '
                                'container start',
                              ),
                              controlAffinity: ListTileControlAffinity.leading,
                              contentPadding: EdgeInsets.zero,
                            ),
                          ),
                        ],
                      ],
                    ),
                    const SizedBox(height: 16),
                    WorkspaceSectionPane(
                      key: _mountsKey,
                      icon: Icons.folder_open,
                      title: 'Mounts',
                      children: _buildMountsEditor(),
                    ),
                    const SizedBox(height: 16),
                    WorkspaceSectionPane(
                      key: _envKey,
                      icon: Icons.code,
                      title: 'Environment',
                      children: _buildEnvVarsEditor(),
                    ),
                    const SizedBox(height: 16),
                    WorkspaceSectionPane(
                      key: _netfilterKey,
                      icon: Icons.shield,
                      title: 'Netfilter',
                      children: [
                        DropdownButtonFormField<String>(
                          initialValue: _egressMode,
                          decoration: InputDecoration(
                            labelText: 'Egress Mode',
                            labelStyle: _labelStyle,
                            floatingLabelStyle: _labelStyle,
                            floatingLabelBehavior: FloatingLabelBehavior.always,
                            border: const OutlineInputBorder(),
                          ),
                          items: const [
                            DropdownMenuItem(
                              value: 'interactive',
                              child: Text('interactive (ask first)'),
                            ),
                            DropdownMenuItem(
                              value: 'static',
                              child: Text('static (deny + record)'),
                            ),
                            DropdownMenuItem(
                              value: 'allow',
                              child: Text('allow (default-permit)'),
                            ),
                          ],
                          onChanged: (v) => setState(
                            () => _egressMode = v ?? 'interactive',
                          ),
                        ),
                        const SizedBox(height: 16),
                        ..._buildAllowedDomainsEditor(),
                        const SizedBox(height: 16),
                        ..._buildRejectedDomainsEditor(),
                      ],
                    ),
                    const SizedBox(height: 16),
                    WorkspaceSectionPane(
                      key: _resourcesKey,
                      icon: Icons.speed,
                      title: 'Resources',
                      children: [
                        Row(
                          children: [
                            Expanded(
                              child: TextField(
                                controller: _idleTimeoutController,
                                decoration: InputDecoration(
                                  labelText: 'Idle Timeout (s)',
                                  labelStyle: _labelStyle,
                                  floatingLabelStyle: _labelStyle,
                                  floatingLabelBehavior:
                                      FloatingLabelBehavior.always,
                                  border: const OutlineInputBorder(),
                                  hintText: '0 = never',
                                ),
                                keyboardType: TextInputType.number,
                              ),
                            ),
                            const SizedBox(width: 12),
                            Expanded(
                              child: TextField(
                                controller: _cpuLimitController,
                                decoration: InputDecoration(
                                  labelText: 'CPU Limit',
                                  labelStyle: _labelStyle,
                                  floatingLabelStyle: _labelStyle,
                                  floatingLabelBehavior:
                                      FloatingLabelBehavior.always,
                                  border: const OutlineInputBorder(),
                                  hintText: 'e.g. 2.0',
                                ),
                                keyboardType: TextInputType.number,
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 12),
                        Row(
                          children: [
                            Expanded(
                              child: TextField(
                                controller: _memoryLimitController,
                                decoration: InputDecoration(
                                  labelText: 'Memory Limit',
                                  labelStyle: _labelStyle,
                                  floatingLabelStyle: _labelStyle,
                                  floatingLabelBehavior:
                                      FloatingLabelBehavior.always,
                                  border: const OutlineInputBorder(),
                                  hintText: 'e.g. 4g',
                                ),
                              ),
                            ),
                            const SizedBox(width: 12),
                            Expanded(
                              child: TextField(
                                controller: _pidsLimitController,
                                decoration: InputDecoration(
                                  labelText: 'PIDs Limit',
                                  labelStyle: _labelStyle,
                                  floatingLabelStyle: _labelStyle,
                                  floatingLabelBehavior:
                                      FloatingLabelBehavior.always,
                                  border: const OutlineInputBorder(),
                                  hintText: 'e.g. 512',
                                ),
                                keyboardType: TextInputType.number,
                              ),
                            ),
                          ],
                        ),
                        const SizedBox(height: 12),
                        Row(
                          children: [
                            Expanded(
                              child: TextField(
                                controller: _tmpSizeController,
                                decoration: InputDecoration(
                                  labelText: '/tmp size',
                                  labelStyle: _labelStyle,
                                  floatingLabelStyle: _labelStyle,
                                  floatingLabelBehavior:
                                      FloatingLabelBehavior.always,
                                  border: const OutlineInputBorder(),
                                  hintText: 'e.g. 2g, 512m',
                                ),
                              ),
                            ),
                          ],
                        ),
                      ],
                    ),
                    const SizedBox(height: 16),
                    WorkspaceSectionPane(
                      key: _advancedKey,
                      icon: Icons.build,
                      title: 'Advanced',
                      children: [
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
                            hintText:
                                'Optional — polled to gauge service health',
                          ),
                          onSubmitted: (_) => _submit(),
                        ),
                        const SizedBox(height: 16),
                        // #2768: classification marking. Free text — the
                        // persistent banner label this workspace's page
                        // will carry. Empty inherits the deploy default.
                        TextField(
                          controller: _classificationBannerController,
                          maxLength: classificationBannerMaxLength,
                          decoration: InputDecoration(
                            counterText: '',
                            labelText: 'Classification Banner',
                            labelStyle: _labelStyle,
                            floatingLabelStyle: _labelStyle,
                            floatingLabelBehavior: FloatingLabelBehavior.always,
                            border: const OutlineInputBorder(),
                            hintText:
                                'e.g. UNCLASSIFIED, CUI (empty = server default)',
                          ),
                          onSubmitted: (_) => _submit(),
                        ),
                        // #2721: home layout. Pre-reflects the deploy
                        // default when known; hidden (and the field
                        // omitted) when it couldn't be fetched — an
                        // offered choice we can't pre-reflect would pin a
                        // possibly-wrong value.
                        if (_perHandleHome != null) ...[
                          const SizedBox(height: 16),
                          Material(
                            type: MaterialType.transparency,
                            child: CheckboxListTile(
                              value: _perHandleHome,
                              onChanged: (v) => setState(
                                () => _perHandleHome = v ?? true,
                              ),
                              title: const Text('Per-handle home'),
                              subtitle: const Text(
                                'Each member gets a private /home/<handle>; '
                                'off = everyone shares /home/klangk',
                              ),
                              controlAffinity: ListTileControlAffinity.trailing,
                              contentPadding: EdgeInsets.zero,
                            ),
                          ),
                        ],
                        // #2202: per-workspace nix flag (only when the
                        // server has a btrfs seed subvolume). Independent
                        // of the image choice — it mounts a shared /nix
                        // snapshot into whatever image the user selected.
                        if (widget.nixAvailable) ...[
                          const SizedBox(height: 16),
                          // Wrap in a transparent Material so the
                          // CheckboxListTile's ink splash paints above the
                          // pane's opaque background surface.
                          Material(
                            type: MaterialType.transparency,
                            child: CheckboxListTile(
                              value: _nixEnabled,
                              onChanged: (v) =>
                                  setState(() => _nixEnabled = v ?? false),
                              title: const Text('Mount /nix dir'),
                              subtitle: const Text(
                                'Mount a shared, writable /nix into this workspace',
                              ),
                              controlAffinity: ListTileControlAffinity.leading,
                              contentPadding: EdgeInsets.zero,
                            ),
                          ),
                        ],
                      ],
                    ),
                  ],
                ),
              ),
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
        FilledButton(onPressed: _submit, child: const Text('Create')),
      ],
    );
  }

  List<Widget> _buildMountsEditor() {
    return [
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
      Text('Allowed Domains', style: _labelStyle),
      const SizedBox(height: 4),
      Text(
        'Hosts the workspace may always contact (egress allowlist).',
        style: const TextStyle(fontSize: 12, color: KColors.textSecondary),
      ),
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

  List<Widget> _buildRejectedDomainsEditor() {
    return [
      Text('Rejected Domains', style: _labelStyle),
      const SizedBox(height: 4),
      Text(
        'Hosts blocked unconditionally (never resolved, no consent asked).',
        style: const TextStyle(fontSize: 12, color: KColors.textSecondary),
      ),
      const SizedBox(height: 8),
      ..._rejectedDomains.asMap().entries.map(
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
                    icon: const Icon(Icons.close, size: 18),
                    onPressed: () =>
                        setState(() => _rejectedDomains.removeAt(e.key)),
                    padding: EdgeInsets.zero,
                    constraints: const BoxConstraints(),
                  ),
                ],
              ),
            ),
          ),
      if (_rejectedDomainsError != null) ...[
        Text(
          _rejectedDomainsError!,
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
              controller: _rejectedDomainsController,
              decoration: const InputDecoration(
                hintText: 'evil.example.com',
                isDense: true,
                border: OutlineInputBorder(),
              ),
              style: const TextStyle(fontSize: 13),
              onSubmitted: (_) => _tryAddRejectedDomain(),
            ),
          ),
          const SizedBox(width: 8),
          IconButton(
            icon: const Icon(Icons.add),
            onPressed: _tryAddRejectedDomain,
          ),
        ],
      ),
      if (_rejectedDomains.isNotEmpty && !widget.netfilterEnabled) ...[
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
                  'rejected-domains list will NOT be enforced until an '
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
