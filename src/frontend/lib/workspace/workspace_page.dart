import 'dart:async';
// ignore: unused_import
import '../theme/colors.dart';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import '../ws/ws_client.dart';
import '../auth/auth_service.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:klangk_plugins/klangk_plugins.dart';
import '../utils/page_title.dart';
import '../widgets/app_bar_actions.dart';
import '../widgets/app_bar_title.dart';
import '../file_viewer/file_viewer_panel.dart';
import '../file_viewer/file_renderer_wiring.dart';
import '../layout/ide_layout.dart';
import '../terminal/ghostty_terminal.dart';
import '../terminal/terminal_link.dart';
import 'workspace_file_api.dart';
import 'package:http/http.dart' as http;
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
import '../browser/browser_delegate.dart';
import '../chat/workspace_chat.dart';
import '../debug/debug_panel.dart';
import 'workspace_settings_panel.dart';
import 'workspace_sharing_panel.dart';

class WorkspacePage extends StatefulWidget {
  final String workspaceId;

  /// Deep-linked workspace-relative file to open in the Files tab on load
  /// (from the `?file=` query param on the workspace route).
  final String? initialFile;

  /// Deep-linked workspace-relative directory to browse in the Files tab on
  /// load (from the `?dir=` query param).
  final String? initialDir;

  const WorkspacePage({
    super.key,
    required this.workspaceId,
    this.initialFile,
    this.initialDir,
  });

  @override
  State<WorkspacePage> createState() => _WorkspacePageState();
}

class _WorkspacePageState extends State<WorkspacePage> {
  // TODO(config): hoist these container paths into workspace/container config.
  // They must match the container layout (the file API is relative to the
  // home; the shell cwd is `work/` under it). Containers may be configured
  // differently, so hardcoding is a stopgap — follow-up PR.
  static const _containerHome = '/home';
  static const _containerCwd = '/home/work';
  final _terminalKey = GlobalKey<GhosttyTerminalState>();
  final _fileViewerKey = GlobalKey<FileViewerPanelState>();
  final _chatKey = GlobalKey<WorkspaceChatState>();
  bool _connecting = true;
  String? _error;
  String _workspaceName = '';
  int _chatUnread = 0;
  bool _chatMentioned = false;
  bool _containerStopped = false;
  bool _restarting = false;
  bool _disconnected = false;
  String? _activeSharedTerminal;
  String _stopReason = '';
  List<String> _workspacePermissions = [];
  BrowserDelegate? _browserDelegate;
  StreamSubscription<Map<String, dynamic>>? _customEventSub;
  StreamSubscription<String>? _errorSub;
  StreamSubscription<String>? _sharedDeletedSub;
  late final ToolPluginRegistry _pluginRegistry;
  late final List<ToolPlugin> _plugins;
  late final FileRendererRegistry _fileRenderers;

  /// Resolves a ⌘/Ctrl-clicked terminal token and opens it: external `http(s)`
  /// URLs in a new tab; workspace files (after existence-verify) in the file
  /// view via the `?file=` deep-link. All untrusted-input handling lives in
  /// [TerminalLinkActions]/[classifyTerminalLink].
  void _handleTerminalPathTap(
      ({String token, String? uri, String pwd, String tail}) e) {
    final authToken = context.read<AuthService>().token;
    final actions = TerminalLinkActions(
      pathRoot: _containerHome,
      defaultCwd: _containerCwd,
      openExternalUrl: openUrl,
      statPath: (rel) => statWorkspacePath(
        client: http.Client(),
        baseUrl: baseUrl,
        workspaceId: widget.workspaceId,
        rel: rel,
        authToken: authToken,
      ),
      openFile: (rel) => context.go('/workspace/${widget.workspaceId}'
          '?file=${Uri.encodeQueryComponent(rel)}'),
      openDirectory: (rel) => context.go('/workspace/${widget.workspaceId}'
          '?dir=${Uri.encodeQueryComponent(rel)}'),
    );
    unawaited(
        actions.handle(token: e.token, uri: e.uri, pwd: e.pwd, tail: e.tail));
  }

  @override
  void initState() {
    super.initState();
    _pluginRegistry = ToolPluginRegistry();
    _plugins = createAllPlugins();
    for (final plugin in _plugins) {
      _pluginRegistry.register(plugin);
    }
    _fileRenderers = buildFileRendererRegistry(_plugins);
    _fetchWorkspaceName();
    WidgetsBinding.instance.addPostFrameCallback((_) => _connectToWorkspace());
  }

  Future<void> _fetchWorkspaceName() async {
    final auth = context.read<AuthService>();
    try {
      final response = await auth.authGet('/workspaces');
      if (response.statusCode == 200) {
        final workspaces = jsonDecode(response.body) as List;
        for (final ws in workspaces) {
          if (ws['id'] == widget.workspaceId) {
            if (mounted) {
              setState(() => _workspaceName = ws['name'] as String);
              setPageTitle(_workspaceName);
            }
            break;
          }
        }
      }
    } catch (_) {}
    // Fetch per-resource permissions for tab visibility
    debugPrint('[WorkspacePage] fetching workspace permissions');
    try {
      final resource = '/workspaces/${widget.workspaceId}';
      final permResp = await auth.authGet(
        '/api/my-permissions?resource=${Uri.encodeQueryComponent(resource)}',
      );
      if (permResp.statusCode == 200 && mounted) {
        final data = jsonDecode(permResp.body) as Map<String, dynamic>;
        final permsMap = data['permissions'] as Map<String, dynamic>? ?? {};
        final perms = permsMap[resource] as List? ?? [];
        setState(() {
          _workspacePermissions = List<String>.from(perms);
        });
      }
    } catch (_) {}
  }

  bool _hasPerm(String perm) =>
      _workspacePermissions.contains(perm) ||
      _workspacePermissions.contains('*');

  Future<void> _connectToWorkspace() async {
    final wsClient = context.read<WsClient>();

    if (!wsClient.connected) {
      await wsClient.connect();
    }

    if (!wsClient.connected) {
      setState(() {
        _connecting = false;
        _error = 'Failed to connect to server';
      });
      return;
    }

    wsClient.connectWorkspace(widget.workspaceId);
    wsClient.addListener(_onClientUpdate);

    // Start browser delegate for bridge requests
    _browserDelegate = BrowserDelegate(wsClient, registry: _pluginRegistry);
    _browserDelegate!.start();

    // Listen for container lifecycle events
    _customEventSub = wsClient.customEvents.listen((msg) {
      final event = msg['event'] as Map<String, dynamic>?;
      if (event == null) return;
      final name = event['name'] as String?;
      if (name == 'container_stopped' && !_containerStopped) {
        final value = event['value'] as Map<String, dynamic>?;
        final reason = value?['reason'] ?? '';
        if (mounted) {
          setState(() {
            _containerStopped = true;
            _stopReason = reason.toString().isNotEmpty
                ? 'Container stopped ($reason)'
                : 'Container stopped';
          });
        }
      } else if (name == 'container_ready' && _restarting) {
        if (mounted) {
          setState(() {
            _restarting = false;
            _containerStopped = false;
          });
        }
      }
    });

    // Listen for shared terminal deletions
    _sharedDeletedSub = wsClient.sharedTerminalDeleted.listen((name) {
      if (!mounted) return;
      // Switch away if we were viewing the deleted terminal.
      if (_activeSharedTerminal == name) {
        setState(() => _activeSharedTerminal = null);
      }
      // Show snackbar for everyone except the user who deleted it.
      if (wsClient.lastDeletedSharedTerminal == name) {
        wsClient.lastDeletedSharedTerminal = null;
      } else {
        ScaffoldMessenger.of(context)
          ..hideCurrentSnackBar()
          ..showSnackBar(
            SnackBar(
              content: Text('Shared terminal "$name" was deleted'),
              duration: const Duration(days: 1),
              showCloseIcon: true,
            ),
          );
      }
    });

    // Listen for errors — only show the full-page error screen for
    // permission/auth errors.  Connection errors are handled by the
    // reconnecting overlay (_disconnected path).
    _errorSub = wsClient.errors.listen((error) {
      if (mounted) {
        final lower = error.toLowerCase();
        if (lower.contains('permission') || lower.contains('denied')) {
          setState(() => _error = error);
        }
      }
    });
  }

  // Cache previous values to avoid unnecessary rebuilds.
  List<Map<String, dynamic>> _prevTerminalWindows = const [];
  List<Map<String, dynamic>> _prevSharedTerminals = const [];

  void _onClientUpdate() {
    final wsClient = context.read<WsClient>();
    if (wsClient.currentWorkspaceId == widget.workspaceId) {
      final wasDisconnected = _disconnected;
      final changed = _connecting || _disconnected;
      if (changed) {
        setState(() {
          _connecting = false;
          _disconnected = false;
        });
        // Only send ui_ready when transitioning to connected state.
        WidgetsBinding.instance.addPostFrameCallback((_) {
          wsClient.sendUiReady();
        });
      }
      if (wasDisconnected && mounted) {
        ScaffoldMessenger.of(context)
          ..hideCurrentSnackBar()
          ..showSnackBar(const SnackBar(
            content: Text('Reconnected'),
            duration: Duration(seconds: 3),
            behavior: SnackBarBehavior.floating,
            width: 200,
          ));
      }
    }
    // Rebuild only when terminal/shared tab lists actually change.
    if (!identical(wsClient.terminalWindows, _prevTerminalWindows) ||
        !identical(wsClient.sharedTerminals, _prevSharedTerminals)) {
      _prevTerminalWindows = wsClient.terminalWindows;
      _prevSharedTerminals = wsClient.sharedTerminals;
      if (mounted) setState(() {});
    }
    // Detect WebSocket disconnect after we were connected
    if (!wsClient.connected && !_connecting && !_disconnected) {
      setState(() => _disconnected = true);
    }
    // Rebuild when reconnecting state changes
    if (wsClient.reconnecting) {
      setState(() {});
    }
  }

  void _restartContainer() {
    setState(() => _restarting = true);
    final wsClient = context.read<WsClient>();
    wsClient.sendRestartContainer();
  }

  void _switchToIsolated(WsClient wsClient, int index) {
    final wasShared = _activeSharedTerminal != null;
    setState(() => _activeSharedTerminal = null);
    if (wasShared) {
      // Restart the isolated terminal session — the shared terminal
      // handler stopped it.  terminal_start uses -A to reattach to
      // the existing tmux session, preserving all windows.
      wsClient.sendTerminalStart();
    }
    wsClient.sendTerminalSelectWindow(index);
  }

  void _joinShared(WsClient wsClient, String name) {
    setState(() => _activeSharedTerminal = name);
    wsClient.sendJoinSharedTerminal(name);
  }

  void _createSharedTerminal(WsClient wsClient) {
    final controller = TextEditingController();
    showDialog<void>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Create Shared Terminal'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(
            labelText: 'Name',
            border: OutlineInputBorder(),
          ),
          onSubmitted: (value) {
            final name = value.trim();
            if (name.isNotEmpty) {
              Navigator.of(ctx).pop();
              wsClient.sendCreateSharedTerminal(name);
            }
          },
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () {
              final name = controller.text.trim();
              if (name.isNotEmpty) {
                Navigator.of(ctx).pop();
                wsClient.sendCreateSharedTerminal(name);
              }
            },
            child: const Text('Create'),
          ),
        ],
      ),
    );
  }

  Widget _buildTerminalWithTabs(WsClient wsClient) {
    debugPrint(
        '[WorkspacePage] _buildTerminalWithTabs: ${DateTime.now()} windows=${wsClient.terminalWindows.length}');
    final windows = wsClient.terminalWindows;
    final shared = wsClient.sharedTerminals;
    final hasContent = windows.isNotEmpty || shared.isNotEmpty;
    return Column(
      children: [
        if (hasContent)
          Container(
            height: 32,
            decoration: const BoxDecoration(
              color: KColors.bgAppBar,
              border: Border(
                bottom: BorderSide(color: KColors.borderMuted),
              ),
            ),
            child: Row(
              children: [
                const SizedBox(width: 4),
                // Isolated terminal tabs
                Expanded(
                  child: ListView(
                    scrollDirection: Axis.horizontal,
                    children: [
                      for (final w in windows)
                        _TerminalTab(
                          name: w['name'] as String? ?? '?',
                          active: _activeSharedTerminal == null &&
                              (w['active'] as bool? ?? false),
                          onTap: () => _switchToIsolated(
                            wsClient,
                            w['index'] as int,
                          ),
                          onClose: windows.length > 1
                              ? () => wsClient.sendTerminalCloseWindow(
                                    w['index'] as int,
                                  )
                              : null,
                        ),
                      // "+" for new isolated terminal
                      _TabIconButton(
                        icon: Icons.add,
                        tooltip: 'New terminal',
                        onTap: () => wsClient.sendTerminalNewWindow(),
                      ),
                      // Separator
                      if (shared.isNotEmpty || _hasPerm('share-terminals'))
                        Container(
                          width: 1,
                          height: 16,
                          margin: const EdgeInsets.symmetric(horizontal: 8),
                          color: KColors.borderDefault,
                        ),
                      // Shared terminal tabs
                      for (final s in shared)
                        _TerminalTab(
                          name: s['name'] as String? ?? '?',
                          active:
                              _activeSharedTerminal == (s['name'] as String?),
                          shared: true,
                          readOnly: !_hasPerm('code-in-shared-terminals') &&
                              !_hasPerm('share-terminals'),
                          onTap: () => _joinShared(
                            wsClient,
                            s['name'] as String,
                          ),
                          onClose: _hasPerm('share-terminals')
                              ? () => wsClient.sendDeleteSharedTerminal(
                                    s['name'] as String,
                                  )
                              : null,
                        ),
                      // "+shared" button
                      if (_hasPerm('share-terminals'))
                        _TabIconButton(
                          icon: Icons.screen_share,
                          tooltip: 'Share a terminal',
                          onTap: () => _createSharedTerminal(wsClient),
                        ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        Expanded(
          child: GhosttyTerminal(
            key: _terminalKey,
            wsClient: wsClient,
            onPathTap: _handleTerminalPathTap,
          ),
        ),
      ],
    );
  }

  Future<void> _reconnect() async {
    setState(() => _connecting = true);
    final wsClient = context.read<WsClient>();
    await wsClient.connect();
    if (wsClient.connected) {
      wsClient.connectWorkspace(widget.workspaceId);
    } else {
      setState(() => _connecting = false);
    }
  }

  @override
  void deactivate() {
    _customEventSub?.cancel();
    _customEventSub = null;
    _errorSub?.cancel();
    _errorSub = null;
    final wsClient = context.read<WsClient>();
    wsClient.removeListener(_onClientUpdate);
    wsClient.disconnectWorkspace();
    super.deactivate();
  }

  @override
  void dispose() {
    _sharedDeletedSub?.cancel();
    _browserDelegate?.stop();
    for (final plugin in _plugins) {
      plugin.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null) {
      return Scaffold(
        appBar: AppBar(title: const Text('Workspace')),
        body: Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text('Error: $_error'),
              const SizedBox(height: 16),
              FilledButton(
                onPressed: () => context.go('/workspaces'),
                child: const Text('Back to workspaces'),
              ),
            ],
          ),
        ),
      );
    }

    if (_connecting) {
      return Scaffold(
        appBar: AppBar(title: const Text('Connecting...')),
        body: const Center(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              CircularProgressIndicator(),
              SizedBox(height: 16),
              Text('Loading, please wait'),
            ],
          ),
        ),
      );
    }

    final wsClient = context.read<WsClient>();
    final authToken = context.read<AuthService>().token;

    return Scaffold(
      appBar: AppBar(
        title: AppBarTitle(
            title: _workspaceName.isNotEmpty ? _workspaceName : 'Workspace'),
        actions: const [
          AppBarActions(),
        ],
      ),
      body: Stack(
        children: [
          IdeLayout(
            fileViewer: FileViewerPanel(
              key: _fileViewerKey,
              wsClient: wsClient,
              workspaceId: widget.workspaceId,
              authToken: authToken,
              registry: _fileRenderers,
            ),
            terminal: _buildTerminalWithTabs(wsClient),
            chat: _hasPerm('chat')
                ? WorkspaceChat(
                    key: _chatKey,
                    wsClient: wsClient,
                    onUnreadChanged: (count) {
                      if (mounted) setState(() => _chatUnread = count);
                    },
                    onMentionChanged: (mentioned) {
                      if (mounted) setState(() => _chatMentioned = mentioned);
                    },
                  )
                : null,
            chatUnread: _chatUnread,
            chatMentioned: _chatMentioned,
            settings: _hasPerm('edit')
                ? WorkspaceSettingsPanel(
                    workspaceId: widget.workspaceId,
                  )
                : null,
            sharing: _hasPerm('share')
                ? WorkspaceSharingPanel(
                    workspaceId: widget.workspaceId,
                  )
                : null,
            terminalKey: _terminalKey,
            fileViewerKey: _fileViewerKey,
            chatKey: _chatKey,
            initialFile: widget.initialFile,
            initialDir: widget.initialDir,
            debug: DebugPanel(wsClient: wsClient),
          ),
          for (final plugin in _plugins)
            if (plugin.buildOverlay(context) != null)
              plugin.buildOverlay(context)!,
          if (_containerStopped)
            Container(
              color: Colors.black54,
              child: Center(
                child: _restarting
                    ? const Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          CircularProgressIndicator(color: Colors.white),
                          SizedBox(height: 12),
                          Text('Restarting...',
                              style: TextStyle(color: Colors.white)),
                        ],
                      )
                    : Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text(_stopReason,
                              style: const TextStyle(
                                  color: Colors.white, fontSize: 16)),
                          const SizedBox(height: 16),
                          ElevatedButton.icon(
                            onPressed: _restartContainer,
                            icon: const Icon(Icons.refresh, size: 18),
                            label: const Text('Restart'),
                            style: ElevatedButton.styleFrom(
                              backgroundColor: KColors.accentGreen,
                              foregroundColor: Colors.white,
                            ),
                          ),
                        ],
                      ),
              ),
            ),
          if (_disconnected && !_containerStopped)
            Container(
              color: Colors.black54,
              child: Center(
                child: wsClient.reconnecting
                    ? Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const CircularProgressIndicator(color: Colors.white),
                          const SizedBox(height: 12),
                          Text(
                            'Reconnecting (attempt ${wsClient.reconnectAttempt})...',
                            style: const TextStyle(color: Colors.white),
                          ),
                          const SizedBox(height: 16),
                          ElevatedButton.icon(
                            onPressed: _reconnect,
                            icon: const Icon(Icons.refresh, size: 18),
                            label: const Text('Reconnect now'),
                            style: ElevatedButton.styleFrom(
                              backgroundColor: KColors.accentGreen,
                              foregroundColor: Colors.white,
                            ),
                          ),
                        ],
                      )
                    : Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const Text('Disconnected from server',
                              style:
                                  TextStyle(color: Colors.white, fontSize: 16)),
                          const SizedBox(height: 16),
                          ElevatedButton.icon(
                            onPressed: _reconnect,
                            icon: const Icon(Icons.refresh, size: 18),
                            label: const Text('Reconnect'),
                            style: ElevatedButton.styleFrom(
                              backgroundColor: KColors.accentGreen,
                              foregroundColor: Colors.white,
                            ),
                          ),
                        ],
                      ),
              ),
            ),
        ],
      ),
    );
  }
}

class _TerminalTab extends StatefulWidget {
  final String name;
  final bool active;
  final bool shared;
  final bool readOnly;
  final VoidCallback onTap;
  final VoidCallback? onClose;

  const _TerminalTab({
    required this.name,
    required this.active,
    required this.onTap,
    this.shared = false,
    this.readOnly = false,
    this.onClose,
  });

  @override
  State<_TerminalTab> createState() => _TerminalTabState();
}

class _TerminalTabState extends State<_TerminalTab> {
  bool _hovered = false;

  @override
  Widget build(BuildContext context) {
    final accentColor =
        widget.shared ? KColors.accentCyan : KColors.accentGreen;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 1, vertical: 3),
      child: MouseRegion(
        onEnter: (_) => setState(() => _hovered = true),
        onExit: (_) => setState(() => _hovered = false),
        cursor: SystemMouseCursors.click,
        child: GestureDetector(
          onTap: widget.onTap,
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 3),
            decoration: BoxDecoration(
              color: widget.active
                  ? KColors.bgSurface
                  : _hovered
                      ? KColors.bgOverlay
                      : Colors.transparent,
              borderRadius: BorderRadius.circular(4),
              border: widget.active
                  ? Border.all(color: KColors.borderMuted, width: 0.5)
                  : null,
            ),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (widget.shared) ...[
                  Icon(
                    widget.readOnly
                        ? Icons.visibility_outlined
                        : Icons.people_outline,
                    size: 12,
                    color: widget.active ? accentColor : Colors.white38,
                  ),
                  const SizedBox(width: 4),
                ],
                Text(
                  widget.name,
                  style: TextStyle(
                    fontSize: 12,
                    fontWeight:
                        widget.active ? FontWeight.w600 : FontWeight.normal,
                    color: widget.active
                        ? KColors.textPrimary
                        : _hovered
                            ? Colors.white70
                            : KColors.textSecondary,
                  ),
                ),
                if (widget.onClose != null) ...[
                  const SizedBox(width: 6),
                  MouseRegion(
                    cursor: SystemMouseCursors.click,
                    child: GestureDetector(
                      onTap: widget.onClose,
                      child: Icon(
                        Icons.close,
                        size: 12,
                        color: _hovered
                            ? Colors.white70
                            : widget.active
                                ? Colors.white38
                                : Colors.transparent,
                      ),
                    ),
                  ),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _TabIconButton extends StatefulWidget {
  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;

  const _TabIconButton({
    required this.icon,
    required this.tooltip,
    required this.onTap,
  });

  @override
  State<_TabIconButton> createState() => _TabIconButtonState();
}

class _TabIconButtonState extends State<_TabIconButton> {
  bool _hovered = false;

  @override
  Widget build(BuildContext context) {
    return Tooltip(
      message: widget.tooltip,
      child: MouseRegion(
        onEnter: (_) => setState(() => _hovered = true),
        onExit: (_) => setState(() => _hovered = false),
        cursor: SystemMouseCursors.click,
        child: GestureDetector(
          onTap: widget.onTap,
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
            decoration: BoxDecoration(
              color: _hovered ? KColors.bgOverlay : Colors.transparent,
              borderRadius: BorderRadius.circular(4),
            ),
            child: Icon(
              widget.icon,
              size: 14,
              color: _hovered ? Colors.white : Colors.white54,
            ),
          ),
        ),
      ),
    );
  }
}
