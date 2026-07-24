import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:provider/provider.dart';
import '../auth/auth_service.dart';
import '../theme/colors.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
import '../ws/ws_client.dart';
import 'workspace_list_page.dart' show validateAllowedDomainSpec;

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
  Timer? _saveMessageTimer;
  // #1365: set when a successful save changed allowed_domains on a
  // workspace whose container is running — the egress filter is baked at
  // container create time, so the new ruleset won't take effect until the
  // container is restarted. Surfaced as a notice under the save message.
  bool _pendingEgressRestart = false;

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
      // #1365: the egress filter is applied at container create time (the
      // OCI hook runs at createContainer), so a change to allowed_domains
      // only takes effect on the next (re)start. Detect the change before
      // _loadData() reassigns _workspace, and only nag when a container is
      // actually running (a stopped workspace picks the new rules up on
      // its next start — no action needed).
      final prevDomains = _workspace?['allowed_domains'];
      final egressChanged =
          !_domainListsEqual(prevDomains, fields['allowed_domains']);
      final running = (_workspace?['running'] as bool?) ?? false;
      setState(() {
        _saveMessage = 'Settings saved';
        _pendingEgressRestart = egressChanged && running;
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
      pendingEgressRestart: _pendingEgressRestart,
      netfilterEnabled:
          context.select<AuthService, bool>((a) => a.netfilterEnabled),
      onSave: _saveSettings,
    );
  }
}

/// Compare two allowed_domains values (each a ``List?`` of ``String``) for
/// order-independent equality, so the restart notice fires only on a real
/// change — not a harmless reorder or the null/empty equivalence (#1365).
bool _domainListsEqual(Object? a, Object? b) {
  final la = (a is List ? a.cast<String>() : const <String>[]);
  final lb = (b is List ? b.cast<String>() : const <String>[]);
  if (la.length != lb.length) return false;
  // Order-independent: the server de-dupes + may reorder on round-trip, so
  // compare as sets to avoid a spurious "changed" on a save that didn't.
  return <String>{...la}.difference(<String>{...lb}).isEmpty;
}

class _SettingsForm extends StatefulWidget {
  final String workspaceId;
  final Map<String, dynamic> workspace;
  final List<String> allowedImages;
  final String defaultImage;
  final bool allowAutostart;
  final String? saveMessage;
  final bool pendingEgressRestart;
  final bool netfilterEnabled;
  final Future<void> Function(Map<String, dynamic>) onSave;

  const _SettingsForm({
    required this.workspaceId,
    required this.workspace,
    required this.allowedImages,
    required this.defaultImage,
    required this.allowAutostart,
    required this.saveMessage,
    required this.pendingEgressRestart,
    required this.netfilterEnabled,
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
  final _allowedDomainsCtrl = TextEditingController();
  late String _selectedImage;
  late List<String> _mounts;
  late Map<String, String> _envVars;
  late List<String> _allowedDomains;
  bool _autoStart = false;
  String? _mountError;
  String? _envError;
  String? _allowedDomainsError;
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
    _allowedDomains = List<String>.from(
      (widget.workspace['allowed_domains'] as List?)?.cast<String>() ??
          <String>[],
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
    if (old.workspace['allowed_domains'] !=
        widget.workspace['allowed_domains']) {
      _allowedDomains = List<String>.from(
        (widget.workspace['allowed_domains'] as List?)?.cast<String>() ??
            <String>[],
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
    _allowedDomainsCtrl.dispose();
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
      'allowed_domains': _allowedDomains.isNotEmpty ? _allowedDomains : null,
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
                if (widget.pendingEgressRestart) ...[
                  const SizedBox(height: 8),
                  _buildEgressRestartNotice(),
                ],
                const SizedBox(height: 16),
              ],
              _buildConfigCard(labelStyle),
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

  /// #1365: the egress filter is baked into the container at create time
  /// (the OCI createContainer hook installs the iptables ruleset before the
  /// entrypoint runs), so a saved allowed_domains change has no effect on a
  /// running container until it's restarted. Shown under the save message
  /// only when the change landed and a container is live.
  Widget _buildEgressRestartNotice() {
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
              'Restart the workspace container to apply the new egress '
              'filter — the ruleset is set at container create time.',
            ),
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
        _buildAllowedDomainsEditor(labelStyle),
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

  Widget _buildAllowedDomainsEditor(TextStyle labelStyle) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _buildEditableList(
          label: 'Allowed Domains',
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
          _buildEgressNotEnforcedNotice(),
        ],
      ],
    );
  }

  /// #1769: this workspace declares allowed_domains but the deploy has
  /// netfilter disabled, so the allow-list is NOT being enforced — the
  /// container starts with unrestricted egress (deliberate fail-open).
  /// Surface the gap to the user who set the list (the party at risk);
  /// the server only logs the warning to operator logs otherwise.
  Widget _buildEgressNotEnforcedNotice() {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
      decoration: BoxDecoration(
        color: KColors.accentAmber.withValues(alpha: 0.12),
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
              'allowed-domains list above is NOT being enforced. This '
              'workspace will start with unrestricted outbound network '
              'until an operator enables netfilter on the server.',
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
