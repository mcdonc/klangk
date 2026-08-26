import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../theme/colors.dart';
import 'workspace_section_nav.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
import 'workspace_list_page.dart' show validateAllowedDomainSpec;

/// Workspace settings panel: config editing only.
/// Used as a tab in the IDE layout.
class WorkspaceSettingsPanel extends StatefulWidget {
  final String workspaceId;

  /// Invoked when the user accepts the "restart needed" notice from inside
  /// the panel. Routed through the workspace page so it owns the restart
  /// lifecycle (in-flight indicator + container_ready handling) (#1780).
  final VoidCallback onRestart;

  const WorkspaceSettingsPanel({
    super.key,
    required this.workspaceId,
    required this.onRestart,
  });

  @override
  State<WorkspaceSettingsPanel> createState() => WorkspaceSettingsPanelState();
}

class WorkspaceSettingsPanelState extends State<WorkspaceSettingsPanel> {
  Map<String, dynamic>? _workspace;
  List<String> _allowedImages = [];
  String _defaultImage = 'klangk-pi';
  // #2233: whether the server has a nix backend configured (the create
  // dialog reads the same /api/v1/images field). Gates the "Mount /nix
  // dir" toggle in the General pane.
  bool _nixAvailable = false;

  // #2017: whether the deploy allows sudo at all (sudo_available on
  // /api/v1/images) — gates the settings form's lock-down toggle.
  bool _sudoAvailable = false;
  bool _loading = true;
  String? _error;
  String? _saveMessage;
  Timer? _saveMessageTimer;
  // Set when a successful save changed a create-time field (image, mounts,
  // env, service_command, allowed_domains) on a running workspace.  These
  // fields are baked into the container at create time, so the change won't
  // take effect until the container is restarted (#1749, #1365).
  bool _pendingRestart = false;

  @override
  void initState() {
    super.initState();
    _loadData();
  }

  @override
  void dispose() {
    _saveMessageTimer?.cancel();
    super.dispose();
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
        _nixAvailable = imgData['nix_available'] == true;
        _sudoAvailable = imgData['sudo_available'] == true;
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
      // Create-time fields (image, mounts, env, service_command,
      // allowed_domains) are baked into the container at creation.  Detect
      // changes before _loadData() reassigns _workspace, and only nag when
      // a container is actually running (#1749, #1365).
      final running = (_workspace?['running'] as bool?) ?? false;
      final createTimeChanged =
          running && (_hasCreateTimeFieldChanged(_workspace, fields));
      setState(() {
        _saveMessage = 'Settings saved';
        _pendingRestart = createTimeChanged;
      });
      _loadData();
      _saveMessageTimer?.cancel();
      _saveMessageTimer = Timer(const Duration(seconds: 2), () {
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

  /// The user accepted the restart-needed notice: delegate to the workspace
  /// page's restart (it owns the in-flight indicator + container_ready
  /// handling) and clear the notice (#1780).
  void _restartNow() {
    widget.onRestart();
    setState(() => _pendingRestart = false);
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
      nixAvailable: _nixAvailable,
      sudoAvailable: _sudoAvailable,
      allowAutostart:
          context.select<AuthService, bool>((a) => a.allowAutostart),
      saveMessage: _saveMessage,
      pendingRestart: _pendingRestart,
      netfilterEnabled:
          context.select<AuthService, bool>((a) => a.netfilterEnabled),
      onSave: _saveSettings,
      onRestart: _restartNow,
    );
  }
}

/// Compare two domain-list values (``allowed_domains`` or ``rejected_domains``,
/// each a ``List?`` of ``String``) for order-independent equality, so the
/// restart notice fires only on a real change — not a harmless reorder or the
/// null/empty equivalence (#1365, #2386).
bool _domainListsEqual(Object? a, Object? b) {
  final la = (a is List ? a.cast<String>() : const <String>[]);
  final lb = (b is List ? b.cast<String>() : const <String>[]);
  if (la.length != lb.length) return false;
  // Order-independent: the server de-dupes + may reorder on round-trip, so
  // compare as sets to avoid a spurious "changed" on a save that didn't.
  return <String>{...la}.difference(<String>{...lb}).isEmpty;
}

/// Return ``true`` when any create-time field in *fields* differs from the
/// previous workspace snapshot *prev*.  Only meaningful when the workspace is
/// running — callers gate on that before invoking this (#1749).
bool _hasCreateTimeFieldChanged(
  Map<String, dynamic>? prev,
  Map<String, dynamic> fields,
) {
  if (prev == null) return false;
  // image
  if ((fields['image'] ?? '') != (prev['image'] ?? '')) return true;
  // service_command (null ↔ empty treated as equal)
  if ((fields['service_command'] ?? '') != (prev['service_command'] ?? '')) {
    return true;
  }
  // mounts — ordered list comparison (null ↔ empty)
  if (!_stringListsEqual(prev['mounts'], fields['mounts'])) return true;
  // env — map comparison (null ↔ empty)
  if (!_envMapsEqual(prev['env'], fields['env'])) return true;
  // allowed_domains — set comparison
  if (!_domainListsEqual(prev['allowed_domains'], fields['allowed_domains'])) {
    return true;
  }
  // rejected_domains — set comparison (#2386)
  if (!_domainListsEqual(
      prev['rejected_domains'], fields['rejected_domains'])) {
    return true;
  }
  // egress_mode — the network sidecar is set up at container create
  // time, so switching modes takes effect on the next start/restart (#2409).
  if ((fields['egress_mode'] ?? 'interactive') !=
      (prev['egress_mode'] ?? 'interactive')) {
    return true;
  }
  // nix — the per-workspace /nix mount is set up at container create
  // time, so toggling it won't take effect until restart (#2233). Only
  // compare when this save actually emitted a nix value (the toggle was
  // shown); when nix isn't available we never emit nix, so a stale bag
  // value must not trigger a spurious restart.
  final newSettings = (fields['settings'] as Map?) ?? const {};
  if (newSettings.containsKey('nix')) {
    final prevSettings = (prev['settings'] as Map?) ?? const {};
    final prevNix = (prevSettings['nix'] as bool?) ?? false;
    final newNix = (newSettings['nix'] as bool?) ?? false;
    if (prevNix != newNix) return true;
  }
  // allow_sudo — the sudoers rule is written at container-create time,
  // so a posture flip needs a restart to take effect (#2017). Same
  // emit-gating as nix: only compare when this save emitted the key.
  if (newSettings.containsKey('allow_sudo')) {
    final prevSettings = (prev['settings'] as Map?) ?? const {};
    final prevSudo = (prevSettings['allow_sudo'] as bool?) ?? true;
    final newSudo = (newSettings['allow_sudo'] as bool?) ?? true;
    if (prevSudo != newSudo) return true;
  }
  return false;
}

bool _stringListsEqual(Object? a, Object? b) {
  final la = (a is List ? a.cast<String>() : const <String>[]);
  final lb = (b is List ? b.cast<String>() : const <String>[]);
  if (la.length != lb.length) return false;
  for (var i = 0; i < la.length; i++) {
    if (la[i] != lb[i]) return false;
  }
  return true;
}

bool _envMapsEqual(Object? a, Object? b) {
  final ma = (a is Map ? a.cast<String, String>() : const <String, String>{});
  final mb = (b is Map ? b.cast<String, String>() : const <String, String>{});
  if (ma.length != mb.length) return false;
  for (final key in ma.keys) {
    if (ma[key] != mb[key]) return false;
  }
  return true;
}

class _SettingsForm extends StatefulWidget {
  final String workspaceId;
  final Map<String, dynamic> workspace;
  final List<String> allowedImages;
  final String defaultImage;
  final bool nixAvailable;

  /// #2017: whether the deploy allows sudo at all. The per-workspace
  /// toggle can only lock a workspace down below that ceiling, so it's
  /// hidden when the deploy forbids sudo.
  final bool sudoAvailable;
  final bool allowAutostart;
  final String? saveMessage;
  final bool pendingRestart;
  final bool netfilterEnabled;
  final Future<void> Function(Map<String, dynamic>) onSave;
  final VoidCallback onRestart;

  const _SettingsForm({
    required this.workspaceId,
    required this.workspace,
    required this.allowedImages,
    required this.defaultImage,
    required this.nixAvailable,
    this.sudoAvailable = false,
    required this.allowAutostart,
    required this.saveMessage,
    required this.pendingRestart,
    required this.netfilterEnabled,
    required this.onSave,
    required this.onRestart,
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
  final _allowedDomainsCtrl = TextEditingController();
  final _rejectedDomainsCtrl = TextEditingController();
  late TextEditingController _idleTimeoutCtrl;
  late TextEditingController _cpuLimitCtrl;
  late TextEditingController _memoryLimitCtrl;
  late TextEditingController _pidsLimitCtrl;
  late TextEditingController _tmpSizeCtrl;
  late String _selectedImage;
  late List<String> _mounts;
  late Map<String, String> _envVars;
  late List<String> _allowedDomains;
  late List<String> _rejectedDomains;
  // #2409: per-workspace egress mode, seeded from the workspace.
  late String _egressMode;
  bool _autoStart = false;
  // #2233: per-workspace nix toggle (Mount /nix dir). Only meaningful
  // when the server has a nix backend (widget.nixAvailable).
  bool _nixEnabled = false;

  // #2017: per-workspace sudo posture, seeded from the bag (absent =
  // true = follow the deploy posture).
  bool _sudoEnabled = true;
  // #2721: home layout, seeded from the workspace. Mutable (#2719): a
  // flip applies from the next connect/start.
  bool _perHandleHome = true;
  String? _mountError;
  String? _envError;
  String? _allowedDomainsError;
  String? _rejectedDomainsError;
  bool _saving = false;
  bool _exporting = false;

  // Section anchors for the config section-nav strip (#2229): each key is
  // attached to the pane that opens the section so a nav-label tap scrolls
  // it into view.
  final _generalKey = GlobalKey();
  final _mountsKey = GlobalKey();
  final _envKey = GlobalKey();
  final _netfilterKey = GlobalKey();
  final _resourcesKey = GlobalKey();
  final _advancedKey = GlobalKey();
  late final List<WorkspaceSection> _sections = [
    WorkspaceSection('General', _generalKey),
    WorkspaceSection('Mounts', _mountsKey),
    WorkspaceSection('Environment', _envKey),
    WorkspaceSection('Netfilter', _netfilterKey),
    WorkspaceSection('Resources', _resourcesKey),
    WorkspaceSection('Advanced', _advancedKey),
  ];

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
    _allowedDomains = List<String>.from(
      (widget.workspace['allowed_domains'] as List?)?.cast<String>() ??
          <String>[],
    );
    _rejectedDomains = List<String>.from(
      (widget.workspace['rejected_domains'] as List?)?.cast<String>() ??
          <String>[],
    );
    _egressMode = (widget.workspace['egress_mode'] as String?) ?? 'interactive';
    _autoStart = (widget.workspace['auto_start'] as bool?) ?? false;
    _perHandleHome = (widget.workspace['per_handle_home'] as bool?) ?? true;
    final settings =
        (widget.workspace['settings'] as Map<String, dynamic>?) ?? {};
    _idleTimeoutCtrl = TextEditingController(
      text: settings['idle_timeout']?.toString() ?? '',
    );
    _nixEnabled = (settings['nix'] as bool?) ?? false;
    _sudoEnabled = (settings['allow_sudo'] as bool?) ?? true;
    _cpuLimitCtrl = TextEditingController(
      text: settings['cpu_limit']?.toString() ?? '',
    );
    _memoryLimitCtrl = TextEditingController(
      text: (settings['memory_limit'] as String?) ?? '',
    );
    _pidsLimitCtrl = TextEditingController(
      text: settings['pids_limit']?.toString() ?? '',
    );
    _tmpSizeCtrl = TextEditingController(
      text: (settings['tmp_size'] as String?) ?? '',
    );
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
    if (old.workspace['per_handle_home'] !=
        widget.workspace['per_handle_home']) {
      // coverage:ignore-start
      _perHandleHome = (widget.workspace['per_handle_home'] as bool?) ?? true;
    } // coverage:ignore-end
    final oldSettings = (old.workspace['settings'] as Map<String, dynamic>?) ??
        const <String, dynamic>{};
    final newSettings =
        (widget.workspace['settings'] as Map<String, dynamic>?) ??
            const <String, dynamic>{};
    if (oldSettings['nix'] != newSettings['nix']) {
      // coverage:ignore-start
      _nixEnabled = (newSettings['nix'] as bool?) ?? false;
    } // coverage:ignore-end
    if (oldSettings['allow_sudo'] != newSettings['allow_sudo']) {
      // coverage:ignore-start
      _sudoEnabled = (newSettings['allow_sudo'] as bool?) ?? true;
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
    if (old.workspace['allowed_domains'] !=
        widget.workspace['allowed_domains']) {
      _allowedDomains = List<String>.from(
        (widget.workspace['allowed_domains'] as List?)?.cast<String>() ??
            <String>[],
      );
    }
    if (old.workspace['rejected_domains'] !=
        widget.workspace['rejected_domains']) {
      _rejectedDomains = List<String>.from(
        (widget.workspace['rejected_domains'] as List?)?.cast<String>() ??
            <String>[],
      );
    }
    if (old.workspace['egress_mode'] != widget.workspace['egress_mode']) {
      // coverage:ignore-start
      _egressMode =
          (widget.workspace['egress_mode'] as String?) ?? 'interactive';
    } // coverage:ignore-end
  }

  @override
  void dispose() {
    _nameCtrl.dispose();
    _cmdCtrl.dispose();
    _healthCheckCtrl.dispose();
    _mountCtrl.dispose();
    _envCtrl.dispose();
    _allowedDomainsCtrl.dispose();
    _rejectedDomainsCtrl.dispose();
    _idleTimeoutCtrl.dispose();
    _cpuLimitCtrl.dispose();
    _memoryLimitCtrl.dispose();
    _pidsLimitCtrl.dispose();
    _tmpSizeCtrl.dispose();
    super.dispose();
  }

  Map<String, dynamic> _collectSettings() {
    final s = <String, dynamic>{};
    final idle = _idleTimeoutCtrl.text.trim();
    if (idle.isNotEmpty) s['idle_timeout'] = int.parse(idle);
    final cpu = _cpuLimitCtrl.text.trim();
    if (cpu.isNotEmpty) s['cpu_limit'] = double.parse(cpu);
    final mem = _memoryLimitCtrl.text.trim();
    if (mem.isNotEmpty) s['memory_limit'] = mem;
    final pids = _pidsLimitCtrl.text.trim();
    if (pids.isNotEmpty) s['pids_limit'] = int.parse(pids);
    final tmp = _tmpSizeCtrl.text.trim();
    if (tmp.isNotEmpty) s['tmp_size'] = tmp;
    return s;
  }

  Future<void> _save() async {
    setState(() => _saving = true);
    final formSettings = _collectSettings();
    // PUT settings is a full-replace bag, so seed from the existing bag
    // unconditionally — API-only keys the form does not represent (e.g.
    // bridge_timeout) and toggle-gated keys (nix, allow_sudo) whose
    // toggles are hidden on this deploy must survive the save instead of
    // being silently wiped (#2017 review).
    final bag = (widget.workspace['settings'] as Map<String, dynamic>?) ??
        const <String, dynamic>{};
    final Map<String, dynamic> settings = {...bag, ...formSettings};
    // #2233: emit an explicit nix value (true or false) whenever the
    // toggle is shown — including false, to actually turn the mount off
    // (omitting the key leaves the stale bag untouched).
    if (widget.nixAvailable) settings['nix'] = _nixEnabled;
    // #2017: same for the sudo posture — an explicit value whenever the
    // toggle is shown, so an uncheck-to-revert actually clears a stored
    // lock-down. True follows the deploy posture (the server setting
    // remains the ceiling).
    if (widget.sudoAvailable) settings['allow_sudo'] = _sudoEnabled;
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
      'allowed_domains': _allowedDomains.isNotEmpty ? _allowedDomains : null,
      'rejected_domains': _rejectedDomains.isNotEmpty ? _rejectedDomains : null,
      'egress_mode': _egressMode,
      'per_handle_home': _perHandleHome,
      if (widget.allowAutostart) 'auto_start': _autoStart,
      if (settings.isNotEmpty) 'settings': settings,
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

  void _tryAddAllowedDomain() {
    final v = _allowedDomainsCtrl.text.trim();
    if (v.isEmpty) return;
    final err = validateAllowedDomainSpec(v);
    if (err != null) {
      setState(() => _allowedDomainsError = err);
      return;
    }
    setState(() {
      if (!_allowedDomains.contains(v)) _allowedDomains.add(v);
      _allowedDomainsCtrl.clear();
      _allowedDomainsError = null;
    });
  }

  void _tryAddRejectedDomain() {
    final v = _rejectedDomainsCtrl.text.trim();
    if (v.isEmpty) return;
    // CIDR is meaningless for a name-level NXDOMAIN deny-list (#2367).
    final err = validateAllowedDomainSpec(v, allowCidr: false);
    if (err != null) {
      setState(() => _rejectedDomainsError = err);
      return;
    }
    setState(() {
      if (!_rejectedDomains.contains(v)) _rejectedDomains.add(v);
      _rejectedDomainsCtrl.clear();
      _rejectedDomainsError = null;
    });
  }

  @override
  Widget build(BuildContext context) {
    final labelStyle = TextStyle(
      color: KColors.textPrimary,
      fontWeight: FontWeight.bold,
    );

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (widget.saveMessage != null)
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 16, 16, 0),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                _buildSaveMessage(),
                if (widget.pendingRestart) ...[
                  const SizedBox(height: 8),
                  _buildRestartNotice(),
                ],
              ],
            ),
          ),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
          child: WorkspaceSectionNav(sections: _sections),
        ),
        Expanded(
          child: SingleChildScrollView(
            padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
            child: Center(
              child: ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 1500),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    _buildGeneralPane(labelStyle),
                    const SizedBox(height: 16),
                    _buildMountsPane(labelStyle),
                    const SizedBox(height: 16),
                    _buildEnvPane(labelStyle),
                    const SizedBox(height: 16),
                    _buildNetfilterPane(labelStyle),
                    const SizedBox(height: 16),
                    _buildResourcesPane(labelStyle),
                    const SizedBox(height: 16),
                    _buildAdvancedPane(labelStyle),
                    const SizedBox(height: 16),
                    Align(
                      alignment: Alignment.centerRight,
                      child: _buildSaveButton(),
                    ),
                    const SizedBox(height: 16),
                    _buildExportCard(),
                    const SizedBox(height: 16),
                    _buildTransferCard(),
                    const SizedBox(height: 16),
                    _buildDangerZoneCard(),
                  ],
                ),
              ),
            ),
          ),
        ),
      ],
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

  /// Shown under the save message when a create-time field (image, mounts,
  /// env, service_command, allowed_domains) was changed on a running
  /// workspace — the change won't take effect until the container is
  /// restarted (#1749, #1365).
  Widget _buildRestartNotice() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: KColors.accentAmber.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.restart_alt, size: 18),
          const SizedBox(width: 8),
          const Expanded(
            child: Text(
              'Restart the workspace to apply these changes — '
              'they take effect at container create time.',
            ),
          ),
          TextButton(
            onPressed: widget.onRestart,
            child: const Text('Restart now'),
          ),
        ],
      ),
    );
  }

  /// A titled surface card used to group related controls.
  Widget _card({
    required IconData icon,
    required String title,
    Color? titleColor,
    required List<Widget> children,
  }) =>
      WorkspaceSectionPane(
        icon: icon,
        title: title,
        titleColor: titleColor,
        children: children,
      );

  Widget _buildGeneralPane(TextStyle labelStyle) {
    return WorkspaceSectionPane(
      key: _generalKey,
      icon: Icons.tune,
      title: 'General',
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
          // splash paints above the pane's opaque background surface.
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
        // #2233: per-workspace nix toggle. Gated on the server having a
        // nix backend (nix_seed), matching the create dialog. The /nix
        // mount is set up at container create time, so toggling it on a
        // running workspace fires the restart-needed notice below.
        if (widget.nixAvailable) ...[
          const SizedBox(height: 8),
          Material(
            type: MaterialType.transparency,
            child: CheckboxListTile(
              value: _nixEnabled,
              onChanged: (v) => setState(() => _nixEnabled = v ?? false),
              title: const Text('Mount /nix dir'),
              subtitle: const Text(
                'Mount a shared, writable /nix into this workspace',
              ),
              controlAffinity: ListTileControlAffinity.leading,
              contentPadding: EdgeInsets.zero,
            ),
          ),
        ],
        // #2017: per-workspace sudo lock-down. Gated on the deploy
        // allowing sudo (the setting is a ceiling); the sudoers rule is
        // written at container-create time, so a flip on a running
        // workspace fires the restart-needed notice.
        if (widget.sudoAvailable) ...[
          const SizedBox(height: 8),
          Material(
            type: MaterialType.transparency,
            child: CheckboxListTile(
              value: _sudoEnabled,
              onChanged: (v) => setState(() => _sudoEnabled = v ?? true),
              title: const Text('Allow sudo'),
              subtitle: const Text(
                'Uncheck to lock this workspace down (no passwordless '
                'sudo) even when the server allows it; applies at the '
                'next container start',
              ),
              controlAffinity: ListTileControlAffinity.leading,
              contentPadding: EdgeInsets.zero,
            ),
          ),
        ],
      ],
    );
  }

  Widget _buildMountsPane(TextStyle labelStyle) {
    return WorkspaceSectionPane(
      key: _mountsKey,
      icon: Icons.folder_open,
      title: 'Mounts',
      children: [_buildMountsEditor(labelStyle)],
    );
  }

  Widget _buildEnvPane(TextStyle labelStyle) {
    return WorkspaceSectionPane(
      key: _envKey,
      icon: Icons.code,
      title: 'Environment',
      children: [_buildEnvVarsEditor(labelStyle)],
    );
  }

  Widget _buildNetfilterPane(TextStyle labelStyle) {
    return WorkspaceSectionPane(
      key: _netfilterKey,
      icon: Icons.shield,
      title: 'Netfilter',
      children: [
        DropdownButtonFormField<String>(
          initialValue: _egressMode,
          decoration: InputDecoration(
            labelText: 'Egress Mode',
            labelStyle: labelStyle,
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
        _buildAllowedDomainsEditor(labelStyle),
        const SizedBox(height: 16),
        _buildRejectedDomainsEditor(labelStyle),
      ],
    );
  }

  Widget _buildResourcesPane(TextStyle labelStyle) {
    return WorkspaceSectionPane(
      key: _resourcesKey,
      icon: Icons.speed,
      title: 'Resources',
      children: [
        Row(
          children: [
            Expanded(
              child: TextField(
                controller: _idleTimeoutCtrl,
                decoration: InputDecoration(
                  labelText: 'Idle Timeout (s)',
                  labelStyle: labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
                  border: const OutlineInputBorder(),
                  hintText: '0 = never',
                ),
                keyboardType: TextInputType.number,
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: TextField(
                controller: _cpuLimitCtrl,
                decoration: InputDecoration(
                  labelText: 'CPU Limit',
                  labelStyle: labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
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
                controller: _memoryLimitCtrl,
                decoration: InputDecoration(
                  labelText: 'Memory Limit',
                  labelStyle: labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
                  border: const OutlineInputBorder(),
                  hintText: 'e.g. 4g',
                ),
              ),
            ),
            const SizedBox(width: 12),
            Expanded(
              child: TextField(
                controller: _pidsLimitCtrl,
                decoration: InputDecoration(
                  labelText: 'PIDs Limit',
                  labelStyle: labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
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
                controller: _tmpSizeCtrl,
                decoration: InputDecoration(
                  labelText: '/tmp size',
                  labelStyle: labelStyle,
                  floatingLabelBehavior: FloatingLabelBehavior.always,
                  border: const OutlineInputBorder(),
                  hintText: 'e.g. 2g, 512m',
                ),
              ),
            ),
          ],
        ),
      ],
    );
  }

  Widget _buildAdvancedPane(TextStyle labelStyle) {
    return WorkspaceSectionPane(
      key: _advancedKey,
      icon: Icons.build,
      title: 'Advanced',
      children: [
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
        // #2721: home layout. Mutable (#2719) — a flip applies from the
        // next connect/start, never to open sessions, so it is NOT a
        // restart-needed field.
        const SizedBox(height: 16),
        Material(
          type: MaterialType.transparency,
          child: CheckboxListTile(
            value: _perHandleHome,
            onChanged: (v) => setState(() => _perHandleHome = v ?? true),
            title: const Text('Per-handle home'),
            subtitle: const Text(
              'Each member gets a private /home/<handle>; '
              'off = everyone shares /home/klangk (applies from the next '
              'connect)',
            ),
            controlAffinity: ListTileControlAffinity.trailing,
            contentPadding: EdgeInsets.zero,
          ),
        ),
      ],
    );
  }

  Widget _buildSaveButton() {
    return FilledButton.icon(
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
    );
  }

  Widget _buildMountsEditor(TextStyle labelStyle) {
    return _buildEditableList(
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

  Widget _buildAllowedDomainsEditor(TextStyle labelStyle) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Allowed Domains', style: labelStyle),
        const SizedBox(height: 4),
        _buildEditableList(
          labelStyle: labelStyle,
          hint: 'github.com:443',
          controller: _allowedDomainsCtrl,
          error: _allowedDomainsError,
          onAdd: _tryAddAllowedDomain,
          items: _allowedDomains.asMap().entries.map(
                (e) => _buildEditableListItem(
                  text: e.value,
                  onCopy: e.value,
                  onRemove: () =>
                      setState(() => _allowedDomains.removeAt(e.key)),
                ),
              ),
        ),
        const SizedBox(height: 4),
        Text(
          'Restricts outbound network to these hosts (host or host:port). '
          'Requires netfilter to be enabled on the server; empty '
          'means unrestricted.',
          style: TextStyle(
            color: KColors.textSecondary,
            fontSize: 12,
          ),
        ),
        if (_allowedDomains.isNotEmpty && !widget.netfilterEnabled) ...[
          const SizedBox(height: 8),
          _buildEgressNotEnforcedNotice(
            listLabel: 'allowed-domains list',
            consequence: 'This workspace will start with unrestricted '
                'outbound network until an operator enables netfilter on the '
                'server.',
          ),
        ],
      ],
    );
  }

  Widget _buildRejectedDomainsEditor(TextStyle labelStyle) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('Rejected Domains', style: labelStyle),
        const SizedBox(height: 4),
        _buildEditableList(
          labelStyle: labelStyle,
          hint: 'evil.example.com',
          controller: _rejectedDomainsCtrl,
          error: _rejectedDomainsError,
          onAdd: _tryAddRejectedDomain,
          items: _rejectedDomains.asMap().entries.map(
                (e) => _buildEditableListItem(
                  text: e.value,
                  onCopy: e.value,
                  onRemove: () =>
                      setState(() => _rejectedDomains.removeAt(e.key)),
                ),
              ),
        ),
        const SizedBox(height: 4),
        Text(
          'Hosts blocked unconditionally (never resolved, no consent asked). '
          'CIDR ranges are not supported.',
          style: TextStyle(
            color: KColors.textSecondary,
            fontSize: 12,
          ),
        ),
        if (_rejectedDomains.isNotEmpty && !widget.netfilterEnabled) ...[
          const SizedBox(height: 8),
          _buildEgressNotEnforcedNotice(
            listLabel: 'rejected-domains list',
            consequence: 'Hosts on this list will be reachable until an '
                'operator enables netfilter on the server.',
          ),
        ],
      ],
    );
  }

  /// #1769: this workspace declares allowed_domains but the deploy has
  /// netfilter disabled, so the allow-list is NOT being enforced — the
  /// container starts with unrestricted egress (deliberate fail-open).
  /// Surface the gap to the user who set the list (the party at risk);
  /// the server only logs the warning to operator logs otherwise.
  Widget _buildEgressNotEnforcedNotice({
    required String listLabel,
    required String consequence,
  }) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: KColors.accentAmber.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(4),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.warning_amber, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              'Egress filtering is not active on this server — the '
              '$listLabel above is NOT being enforced. $consequence',
            ),
          ),
        ],
      ),
    );
  }

  /// A list of editable text items (mounts / env vars) with an add row and
  /// inline error. The two editors only differ in label, hint, and the
  /// item text/remove callback.
  Widget _buildEditableList({
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

  Widget _buildTransferCard() {
    return _card(
      icon: Icons.swap_horiz,
      title: 'Transfer Ownership',
      children: [
        const SizedBox(height: 4),
        const Text(
          'Transfer this workspace to another user. '
          'You will lose owner access.',
          style: TextStyle(fontSize: 13),
        ),
        const SizedBox(height: 12),
        OutlinedButton.icon(
          onPressed: () => _showTransferDialog(context),
          icon: const Icon(Icons.swap_horiz, size: 18),
          label: const Text('Transfer Ownership'),
        ),
      ],
    );
  }

  void _showTransferDialog(BuildContext context) {
    final controller = TextEditingController();
    final searchResults = ValueNotifier<List<Map<String, dynamic>>>([]);
    Timer? debounce;

    showDialog(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Transfer Ownership'),
        content: SizedBox(
          width: 400,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'Search for the user to transfer this workspace to:',
                style: TextStyle(fontSize: 13),
              ),
              const SizedBox(height: 12),
              TextField(
                controller: controller,
                autofocus: true,
                decoration: const InputDecoration(
                  hintText: 'Type email...',
                  border: OutlineInputBorder(),
                  prefixIcon: Icon(Icons.person, size: 18),
                ),
                onChanged: (q) {
                  debounce?.cancel();
                  if (q.trim().isEmpty) {
                    searchResults.value = [];
                    return;
                  }
                  debounce = Timer(const Duration(milliseconds: 300), () async {
                    final auth = context.read<AuthService>();
                    try {
                      final resp = await auth.authGet(
                        '/api/v1/users/search?q=${Uri.encodeQueryComponent(q.trim())}',
                      );
                      if (resp.statusCode == 200) {
                        searchResults.value = List<Map<String, dynamic>>.from(
                          jsonDecode(resp.body) as List,
                        );
                      }
                    } catch (e) {
                      // coverage:ignore-start
                      debugPrint(
                        '[WorkspaceSettingsPanel] user search failed: $e',
                      );
                    } // coverage:ignore-end
                  });
                },
                onSubmitted: (value) {
                  final email = value.trim();
                  if (email.isNotEmpty) {
                    Navigator.of(ctx).pop();
                    _confirmTransfer(context, email);
                  }
                },
              ),
              const SizedBox(height: 8),
              ValueListenableBuilder<List<Map<String, dynamic>>>(
                valueListenable: searchResults,
                builder: (_, results, __) => Column(
                  mainAxisSize: MainAxisSize.min,
                  children: results
                      .map(
                        (r) => ListTile(
                          dense: true,
                          title: Text(
                            r['email'] as String,
                            style: const TextStyle(fontSize: 13),
                          ),
                          onTap: () {
                            Navigator.of(ctx).pop();
                            _confirmTransfer(
                              context,
                              r['email'] as String,
                            );
                          },
                        ),
                      )
                      .toList(),
                ),
              ),
            ],
          ),
        ),
        actions: [
          TextButton(
            onPressed: () {
              debounce?.cancel();
              Navigator.of(ctx).pop();
            },
            child: const Text('Cancel'),
          ),
        ],
      ),
    );
  }

  void _confirmTransfer(BuildContext context, String email) {
    showDialog<bool>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Confirm Transfer'),
        content: Text(
          'Transfer this workspace to $email? '
          'You will lose owner access.',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              Navigator.of(ctx).pop();
              _executeTransfer(email);
            },
            style: FilledButton.styleFrom(backgroundColor: Colors.orange),
            child: const Text('Transfer'),
          ),
        ],
      ),
    );
  }

  Future<void> _executeTransfer(String email) async {
    final auth = context.read<AuthService>();
    final resp = await auth.authPost(
      '/api/v1/workspaces/${widget.workspaceId}/transfer',
      body: jsonEncode({'email': email}),
    );
    if (!mounted) return;
    if (resp.statusCode == 200) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Workspace transferred to $email')),
      );
    } else {
      String detail;
      try {
        detail = (jsonDecode(resp.body) as Map)['detail'] ?? resp.body;
      } catch (e) {
        debugPrint('[WorkspaceSettingsPanel] parse transfer error: $e');
        detail = 'Error: ${resp.statusCode}';
      }
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('Transfer failed: $detail')),
      );
    }
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
              _shutdownContainer();
            },
            style: FilledButton.styleFrom(backgroundColor: Colors.red),
            child: const Text('Shut Down'),
          ),
        ],
      ),
    );
  }

  Future<void> _shutdownContainer() async {
    // Route through REST /stop (the WS shutdown_container handler was
    // retired). The backend broadcasts container_stopped so the workspace
    // page shows the "stopped" overlay. Surface failures (non-2xx or
    // network errors) as a snackbar rather than silently swallowing them.
    final messenger = ScaffoldMessenger.of(context);
    final auth = context.read<AuthService>();
    try {
      final resp = await auth.authPost(
        '/api/v1/workspaces/${widget.workspaceId}/stop',
      );
      if (resp.statusCode >= 400) {
        messenger.showSnackBar(
          SnackBar(content: Text('Shut down failed (${resp.statusCode})')),
        );
      }
    } catch (_) {
      messenger.showSnackBar(
        const SnackBar(content: Text('Shut down failed: network error')),
      );
    }
  }
}
