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
import '../utils/suppress_browser_menu.dart';
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

  /// Tracks which shared terminal (from another user) we're viewing.
  /// null means we're on our own isolated terminal.
  Map<String, String>? _activeSharedTerminal;

  /// Locally-tracked selected own-window ID.  When null, the first
  /// window in the list is considered selected (initial state).
  String? _selectedOwnWindowId;
  String _stopReason = '';
  List<String> _workspacePermissions = [];
  BrowserDelegate? _browserDelegate;
  StreamSubscription<Map<String, dynamic>>? _customEventSub;
  StreamSubscription<String>? _errorSub;
  StreamSubscription<Map<String, dynamic>>? _sharedDeletedSub;
  late final ToolPluginRegistry _pluginRegistry;
  late final List<ToolPlugin> _plugins;
  late final FileRendererRegistry _fileRenderers;

  /// Resolves a ⌘/Ctrl-clicked terminal token and opens it: external `http(s)`
  /// URLs in a new tab; workspace files (after existence-verify) in the file
  /// view via the `?file=` deep-link. All untrusted-input handling lives in
  /// [TerminalLinkActions]/[classifyTerminalLink].
  void _handleTerminalPathTap(
    ({String token, String? uri, String pwd, String tail}) e,
  ) {
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
      openFile: (rel) => context.go(
        '/workspace/${widget.workspaceId}'
        '?file=${Uri.encodeQueryComponent(rel)}',
      ),
      openDirectory: (rel) => context.go(
        '/workspace/${widget.workspaceId}'
        '?dir=${Uri.encodeQueryComponent(rel)}',
      ),
    );
    unawaited(
      actions.handle(token: e.token, uri: e.uri, pwd: e.pwd, tail: e.tail),
    );
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
      final response = await auth.authGet('/api/v1/workspaces');
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
    } catch (e) {
      debugPrint('[WorkspacePage] fetch workspace name failed: $e');
    }
    // Fetch per-resource permissions for tab visibility
    debugPrint('[WorkspacePage] fetching workspace permissions');
    try {
      final resource = '/workspaces/${widget.workspaceId}';
      final permResp = await auth.authGet(
        '/api/v1/my-permissions?resource=${Uri.encodeQueryComponent(resource)}',
      );
      if (permResp.statusCode == 200 && mounted) {
        final data = jsonDecode(permResp.body) as Map<String, dynamic>;
        final permsMap = data['permissions'] as Map<String, dynamic>? ?? {};
        final perms = permsMap[resource] as List? ?? [];
        setState(() {
          _workspacePermissions = List<String>.from(perms);
        });
      }
    } catch (e) {
      debugPrint('[WorkspacePage] fetch permissions failed: $e');
    }
  }

  bool _hasPerm(String perm) =>
      _workspacePermissions.contains(perm) ||
      _workspacePermissions.contains('*');

  Future<void> _connectToWorkspace() async {
    debugPrint('[WorkspacePage] _connectToWorkspace called: ${DateTime.now()}');
    final wsClient = context.read<WsClient>();

    if (!wsClient.connected) {
      debugPrint(
          '[WorkspacePage] calling wsClient.connect(): ${DateTime.now()}');
      await wsClient.connect();
      debugPrint(
          '[WorkspacePage] wsClient.connect() returned: ${DateTime.now()}');
    } else {
      debugPrint('[WorkspacePage] already connected, skipping connect()');
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
    _sharedDeletedSub = wsClient.sharedTerminalDeleted.listen((msg) {
      if (!mounted) return;
      final deletedUserId = msg['user_id'] as String? ?? '';
      final deletedWindow = msg['window_name'] as String? ?? '';
      final deletedWid = msg['window_id'] as String? ?? '';
      final wasViewing = _activeSharedTerminal != null &&
          _activeSharedTerminal!['user_id'] == deletedUserId &&
          _activeSharedTerminal!['window_id'] == deletedWid;
      if (wasViewing) {
        setState(() => _activeSharedTerminal = null);
      }
      final last = wsClient.lastDeletedSharedTerminal;
      if (last != null &&
          last['user_id'] == deletedUserId &&
          last['window_id'] == deletedWid) {
        wsClient.lastDeletedSharedTerminal = null;
      } else if (wasViewing) {
        ScaffoldMessenger.of(context)
          ..hideCurrentSnackBar()
          ..showSnackBar(
            SnackBar(
              content: Text('Shared terminal "$deletedWindow" was removed'),
              duration: const Duration(days: 1),
              showCloseIcon: true,
            ),
          );
      } else {
        ScaffoldMessenger.of(context)
          ..hideCurrentSnackBar()
          ..showSnackBar(
            SnackBar(
              content: Text('Shared terminal "$deletedWindow" was removed'),
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
          ..showSnackBar(
            const SnackBar(
              content: Text('Reconnected'),
              duration: Duration(seconds: 3),
              behavior: SnackBarBehavior.floating,
              width: 200,
            ),
          );
      }
    }
    // Rebuild only when terminal/shared tab lists actually change.
    if (!identical(wsClient.terminalWindows, _prevTerminalWindows) ||
        !identical(wsClient.sharedTerminals, _prevSharedTerminals)) {
      _prevTerminalWindows = wsClient.terminalWindows;
      _prevSharedTerminals = wsClient.sharedTerminals;
      // Track selected own-window: initialize on first message, or
      // reset if the selected window was closed.
      if (wsClient.terminalWindows.isNotEmpty) {
        final ids =
            wsClient.terminalWindows.map((w) => w['id'] as String?).toSet();
        if (_selectedOwnWindowId == null) {
          // First load — select window 0 (grouped sessions start there).
          _selectedOwnWindowId = wsClient.terminalWindows[0]['id'] as String?;
        } else if (!ids.contains(_selectedOwnWindowId)) {
          // Selected window was closed — fall back to first window.
          _selectedOwnWindowId = wsClient.terminalWindows[0]['id'] as String?;
        }
      }
      // Auto-join the first shared terminal for spectators (no
      // code-in-isolation) so they don't see a blank cursor.
      if (_activeSharedTerminal == null &&
          !_hasPerm('code-in-isolation') &&
          wsClient.sharedTerminals.isNotEmpty) {
        final first = wsClient.sharedTerminals[0];
        final userId = first['user_id'] as String?;
        final windowId = first['window_id'] as String?;
        if (userId != null && windowId != null) {
          _activeSharedTerminal = {
            'user_id': userId,
            'window_id': windowId,
          };
          wsClient.sendJoinSharedTerminal(userId, windowId);
        }
      }
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

  void _switchToIsolated(WsClient wsClient, String windowId) {
    final wasShared = _activeSharedTerminal != null;
    setState(() {
      _activeSharedTerminal = null;
      _selectedOwnWindowId = windowId;
    });
    if (wasShared) {
      // Clear stale shared terminal content before reattaching.
      _terminalKey.currentState?.clearScreen();
      // Restart the isolated terminal session — the shared terminal
      // handler stopped it.  terminal_start uses -A to reattach to
      // the existing tmux session, preserving all windows.
      wsClient.sendTerminalStart();
    }
    wsClient.sendTerminalSelectWindow(windowId);
  }

  void _joinShared(WsClient wsClient, String userId, String windowId) {
    setState(
      () => _activeSharedTerminal = {
        'user_id': userId,
        'window_id': windowId,
      },
    );
    // Clear the terminal so stale content from the previous session
    // doesn't linger while the join is in progress.
    _terminalKey.currentState?.clearScreen();
    wsClient.sendJoinSharedTerminal(userId, windowId);
  }

  /// Check if a window is shared by looking it up in the shared terminals list.
  bool _isWindowShared(WsClient wsClient, String windowId) {
    final myUserId = wsClient.currentUserId;
    if (myUserId == null) return false;
    return wsClient.sharedTerminals.any(
      (s) => s['user_id'] == myUserId && s['window_id'] == windowId,
    );
  }

  List<Map<String, dynamic>> _getViewers(
    WsClient wsClient,
    String ownerUserId,
    String windowId,
  ) {
    for (final s in wsClient.sharedTerminals) {
      if (s['user_id'] == ownerUserId && s['window_id'] == windowId) {
        final viewers = s['viewers'];
        if (viewers is List) {
          return viewers.cast<Map<String, dynamic>>();
        }
      }
    }
    return [];
  }

  Widget _buildTerminalWithTabs(WsClient wsClient) {
    debugPrint(
      '[WorkspacePage] _buildTerminalWithTabs: ${DateTime.now()} windows=${wsClient.terminalWindows.length}',
    );
    final windows = wsClient.terminalWindows;
    final shared = wsClient.sharedTerminals;
    // Shared terminals from OTHER users (not ours)
    final myUserId = wsClient.currentUserId;
    final othersShared = shared.where((s) => s['user_id'] != myUserId).toList();
    final hasContent = windows.isNotEmpty || othersShared.isNotEmpty;
    return Column(
      children: [
        if (hasContent)
          Container(
            height: 32,
            decoration: const BoxDecoration(
              color: KColors.bgAppBar,
              border: Border(bottom: BorderSide(color: KColors.borderMuted)),
            ),
            child: Row(
              children: [
                const SizedBox(width: 4),
                Expanded(
                  child: ListView(
                    scrollDirection: Axis.horizontal,
                    children: [
                      // Own terminal tabs with share/unshare toggle
                      if (_hasPerm('code-in-isolation'))
                        for (final w in windows)
                          _TerminalTab(
                            name: w['name'] as String? ?? '?',
                            tooltip: w['name'] as String? ?? '?',
                            active: _activeSharedTerminal == null &&
                                (_selectedOwnWindowId != null
                                    ? (w['id'] as String?) ==
                                        _selectedOwnWindowId
                                    : (w['active'] as bool? ?? false)),
                            isShared: _isWindowShared(
                              wsClient,
                              w['id'] as String? ?? '',
                            ),
                            viewers: _getViewers(
                              wsClient,
                              myUserId ?? '',
                              w['id'] as String? ?? '',
                            ),
                            onTap: () => _switchToIsolated(
                              wsClient,
                              w['id'] as String? ?? '',
                            ),
                            onClose: windows.length > 1
                                ? () => wsClient.sendTerminalCloseWindow(
                                      w['index'] as int,
                                    )
                                : null,
                            onRename: (newName) =>
                                wsClient.sendTerminalRenameWindow(
                              w['index'] as int,
                              newName,
                            ),
                            onToggleShare: _hasPerm('share-terminals')
                                ? () {
                                    final wid = w['id'] as String? ?? '';
                                    if (_isWindowShared(wsClient, wid)) {
                                      wsClient.sendUnshareWindow(wid);
                                    } else {
                                      wsClient.sendShareWindow(wid);
                                    }
                                  }
                                : null,
                          ),
                      // "+" for new terminal
                      if (_hasPerm('code-in-isolation'))
                        _TabIconButton(
                          icon: Icons.add,
                          tooltip: 'New terminal',
                          onTap: () => wsClient.sendTerminalNewWindow(),
                        ),
                      // Shared terminals from OTHER users
                      for (final s in othersShared)
                        _TerminalTab(
                          name: () {
                            final h = s['handle'] as String? ?? '?';
                            final w = s['window_name'] as String? ?? '?';
                            final he =
                                h.length > 5 ? '${h.substring(0, 5)}…' : h;
                            final we =
                                w.length > 3 ? '${w.substring(0, 3)}…' : w;
                            return '$he:$we';
                          }(),
                          tooltip:
                              '${s['handle'] ?? '?'}:${s['window_name'] ?? '?'}',
                          active: _activeSharedTerminal != null &&
                              _activeSharedTerminal!['user_id'] ==
                                  s['user_id'] &&
                              _activeSharedTerminal!['window_id'] ==
                                  s['window_id'],
                          shared: true,
                          readOnly: !_hasPerm('code-in-shared-terminals') &&
                              !_hasPerm('share-terminals'),
                          viewers: _getViewers(
                            wsClient,
                            s['user_id'] as String? ?? '',
                            s['window_id'] as String? ?? '',
                          ),
                          onTap: () => _joinShared(
                            wsClient,
                            s['user_id'] as String,
                            s['window_id'] as String,
                          ),
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
        appBar: AppBar(
          title: const AppBarTitle(title: 'Connecting...'),
          actions: const [AppBarActions()],
        ),
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
          title: _workspaceName.isNotEmpty ? _workspaceName : 'Workspace',
        ),
        actions: const [AppBarActions()],
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
                ? WorkspaceSettingsPanel(workspaceId: widget.workspaceId)
                : null,
            sharing: _hasPerm('share')
                ? WorkspaceSharingPanel(workspaceId: widget.workspaceId)
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
                          Text(
                            'Restarting...',
                            style: TextStyle(color: Colors.white),
                          ),
                        ],
                      )
                    : Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text(
                            _stopReason,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 16,
                            ),
                          ),
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
                          const SizedBox(height: 12),
                          TextButton(
                            onPressed: () => context.go('/'),
                            child: const Text(
                              'Back to workspaces',
                              style: TextStyle(color: Colors.white54),
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
                          const SizedBox(height: 12),
                          TextButton(
                            onPressed: () => context.go('/'),
                            child: const Text(
                              'Back to workspaces',
                              style: TextStyle(color: Colors.white54),
                            ),
                          ),
                        ],
                      )
                    : Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          const Text(
                            'Connection lost',
                            style: TextStyle(color: Colors.white, fontSize: 16),
                          ),
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
                          const SizedBox(height: 12),
                          TextButton(
                            onPressed: () => context.go('/'),
                            child: const Text(
                              'Back to workspaces',
                              style: TextStyle(color: Colors.white54),
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
  final String? tooltip;
  final bool active;
  final bool shared;
  final bool readOnly;
  final bool isShared;
  final List<Map<String, dynamic>> viewers;
  final VoidCallback onTap;
  final VoidCallback? onClose;
  final VoidCallback? onToggleShare;
  final void Function(String newName)? onRename;

  const _TerminalTab({
    required this.name,
    required this.active,
    required this.onTap,
    this.tooltip,
    this.shared = false,
    this.readOnly = false,
    this.isShared = false,
    this.viewers = const [],
    this.onClose,
    this.onToggleShare,
    this.onRename,
  });

  @override
  State<_TerminalTab> createState() => _TerminalTabState();
}

class _TerminalTabState extends State<_TerminalTab> {
  bool _hovered = false;
  Offset? _tapPosition;

  void _showContextMenu() {
    final pos = _tapPosition;
    if (pos == null) return;
    final items = <PopupMenuEntry<String>>[
      if (widget.onRename != null)
        const PopupMenuItem(
          value: 'rename',
          child: ListTile(
            dense: true,
            leading: Icon(Icons.edit, size: 18),
            title: Text('Rename'),
          ),
        ),
      if (widget.onToggleShare != null)
        PopupMenuItem(
          value: 'share',
          child: ListTile(
            dense: true,
            leading: Icon(
              widget.isShared ? Icons.cell_tower : Icons.share_outlined,
              size: 18,
            ),
            title: Text(widget.isShared ? 'Unshare' : 'Share'),
          ),
        ),
    ];
    if (items.isEmpty) return;
    showMenu<String>(
      context: context,
      position: RelativeRect.fromLTRB(pos.dx, pos.dy, pos.dx, pos.dy),
      items: items,
    ).then((action) {
      if (action == 'rename') {
        _showRenameDialog();
      } else if (action == 'share') {
        widget.onToggleShare?.call();
      }
    });
  }

  void _showRenameDialog() {
    final controller = TextEditingController(text: widget.name);
    showDialog<String>(
      context: context,
      builder: (ctx) => AlertDialog(
        title: const Text('Rename terminal'),
        content: TextField(
          controller: controller,
          autofocus: true,
          decoration: const InputDecoration(hintText: 'Terminal name'),
          onSubmitted: (v) => Navigator.of(ctx).pop(v.trim()),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(),
            child: const Text('Cancel'),
          ),
          TextButton(
            onPressed: () => Navigator.of(ctx).pop(controller.text.trim()),
            child: const Text('OK'),
          ),
        ],
      ),
    ).then((newName) {
      if (newName != null && newName.isNotEmpty && newName != widget.name) {
        widget.onRename?.call(newName);
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    return _maybeTooltip(Padding(
      padding: const EdgeInsets.symmetric(horizontal: 1, vertical: 3),
      child: MouseRegion(
        onEnter: (_) => setState(() => _hovered = true),
        onExit: (_) => setState(() => _hovered = false),
        cursor: SystemMouseCursors.click,
        child: SuppressBrowserContextMenu(
          child: GestureDetector(
            onTap: widget.onTap,
            onSecondaryTapDown: (details) {
              _tapPosition = details.globalPosition;
            },
            onSecondaryTap: _showContextMenu,
            child: SizedBox(
              width: 120,
              child: Container(
                padding:
                    const EdgeInsets.symmetric(horizontal: 10, vertical: 3),
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
                  children: [
                    // Icon for other users' shared tabs (left of name)
                    if (widget.shared) ...[
                      Icon(
                        widget.readOnly
                            ? Icons.lock_outlined
                            : Icons.edit_outlined,
                        size: 12,
                        color: widget.active
                            ? KColors.accentAmber
                            : Colors.white38,
                      ),
                      const SizedBox(width: 4),
                    ],
                    // Broadcast icon for own tabs that are actively shared
                    // — click to unshare
                    if (!widget.shared && widget.isShared) ...[
                      GestureDetector(
                        onTap: widget.onToggleShare,
                        child: const MouseRegion(
                          cursor: SystemMouseCursors.click,
                          child: Tooltip(
                            message: 'Unshare',
                            child: Icon(
                              Icons.cell_tower,
                              size: 12,
                              color: KColors.accentCyan,
                            ),
                          ),
                        ),
                      ),
                      const SizedBox(width: 4),
                    ],
                    Expanded(
                      child: Text(
                        widget.name,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: 12,
                          fontWeight: widget.active
                              ? FontWeight.w600
                              : FontWeight.normal,
                          color: widget.active
                              ? KColors.textPrimary
                              : _hovered
                                  ? Colors.white70
                                  : KColors.textSecondary,
                        ),
                      ),
                    ),
                    // Viewer count
                    if (widget.viewers.isNotEmpty) ...[
                      const SizedBox(width: 4),
                      Icon(
                        Icons.visibility,
                        size: 10,
                        color: Colors.white38,
                      ),
                      const SizedBox(width: 2),
                      Text(
                        '${widget.viewers.length}',
                        style: const TextStyle(
                          fontSize: 10,
                          color: Colors.white38,
                        ),
                      ),
                    ],
                    if (widget.onClose != null) ...[
                      const SizedBox(width: 4),
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
        ),
      ),
    ));
  }

  Widget _maybeTooltip(Widget child) {
    final parts = <String>[];
    if (widget.tooltip != null) {
      parts.add(widget.tooltip!);
    }
    if (widget.viewers.isNotEmpty) {
      final names = widget.viewers
          .map((v) => (v['email'] as String?)?.split('@').first ?? '?')
          .join(', ');
      parts.add('👁 $names');
    }
    if (parts.isNotEmpty) {
      return Tooltip(message: parts.join('\n'), child: child);
    }
    return child;
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
