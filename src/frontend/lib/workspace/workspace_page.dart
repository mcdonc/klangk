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
import '../utils/page_title.dart';
import '../widgets/app_bar_actions.dart';
import '../widgets/app_bar_title.dart';
import '../file_viewer/file_viewer_panel.dart';
import '../file_viewer/file_renderer_wiring.dart';
import '../layout/ide_layout.dart';
import '../terminal/ghostty_terminal.dart';
import '../terminal/terminal_link.dart';
import 'workspace_file_api.dart';
import 'workspace_overlays.dart';
import 'package:http/http.dart' as http;
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
import '../chat/workspace_chat.dart';
import '../debug/debug_panel.dart';
import 'workspace_settings_panel.dart';
import 'workspace_sharing_panel.dart';
import 'terminal_tabs_view.dart';
import 'workspace_connector.dart';

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
  // Fallback if userHome isn't provided by the backend.
  static const _defaultHome = '/';
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
  late final ToolPluginRegistry _pluginRegistry;
  late final List<ToolPlugin> _plugins;
  late final FileRendererRegistry _fileRenderers;
  WorkspaceConnector? _connector;

  /// Resolves a ⌘/Ctrl-clicked terminal token and opens it: external `http(s)`
  /// URLs in a new tab; workspace files (after existence-verify) in the file
  /// view via the `?file=` deep-link. All untrusted-input handling lives in
  /// [TerminalLinkActions]/[classifyTerminalLink].
  void _handleTerminalPathTap(
    ({String token, String? uri, String pwd, String tail}) e,
  ) {
    final authToken = context.read<AuthService>().token;
    final wsClient = context.read<WsClient>();
    final userHome = wsClient.userHome ?? _defaultHome;
    final actions = TerminalLinkActions(
      pathRoot: userHome,
      defaultCwd: userHome,
      openExternalUrl: openUrl,
      statPath: (path) => statWorkspacePath(
        client: http.Client(),
        baseUrl: baseUrl,
        workspaceId: widget.workspaceId,
        path: path,
        authToken: authToken,
      ),
      openFile: (path) {
        if (!mounted) return;
        context.go(
          '/workspace/${widget.workspaceId}'
          '?file=${Uri.encodeQueryComponent(path)}',
        );
      },
      openDirectory: (path) {
        if (!mounted) return;
        context.go(
          '/workspace/${widget.workspaceId}'
          '?dir=${Uri.encodeQueryComponent(path)}',
        );
      },
    );
    unawaited(
      actions.handle(token: e.token, uri: e.uri, pwd: e.pwd, tail: e.tail),
    );
  }

  @override
  void initState() {
    super.initState();
    _pluginRegistry = ToolPluginRegistry();
    // Plugins are registered once in main() — reuse them here.
    _plugins = _pluginRegistry.plugins.toList();
    _fileRenderers = buildFileRendererRegistry(_plugins);
    _fetchWorkspaceName();
    WidgetsBinding.instance.addPostFrameCallback((_) => _connectToWorkspace());
  }

  Future<void> _fetchWorkspaceName() async {
    final auth = context.read<AuthService>();
    try {
      final name =
          await _findWorkspaceName(auth, '/api/v1/workspaces') ??
          await _findWorkspaceName(auth, '/api/v1/workspaces/shared');
      if (name != null && mounted) {
        setState(() => _workspaceName = name);
        setPageTitle(_workspaceName);
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

  Future<String?> _findWorkspaceName(AuthService auth, String url) async {
    final response = await auth.authGet(url);
    if (response.statusCode == 200) {
      final workspaces = jsonDecode(response.body) as List;
      for (final ws in workspaces) {
        if (ws['id'] == widget.workspaceId) {
          return ws['name'] as String;
        }
      }
    }
    return null;
  }

  bool _hasPerm(String perm) =>
      _workspacePermissions.contains(perm) ||
      _workspacePermissions.contains('*');

  Future<void> _connectToWorkspace() async {
    final wsClient = context.read<WsClient>();

    _connector = WorkspaceConnector(
      wsClient: wsClient,
      workspaceId: widget.workspaceId,
      pluginRegistry: _pluginRegistry,
      onConnected: ({required bool connected, String? error}) {
        if (!mounted) return;
        if (!connected) {
          setState(() {
            _connecting = false;
            _error = error;
          });
          return;
        }
        wsClient.addListener(_onClientUpdate);
      },
      onContainerEvent: (name, value) {
        if (!mounted) return;
        if (name == 'container_stopped' && !_containerStopped) {
          final reason = value?['reason'] ?? '';
          setState(() {
            _containerStopped = true;
            _stopReason = reason.toString().isNotEmpty
                ? 'Container stopped ($reason)'
                : 'Container stopped';
          });
        } else if (name == 'container_ready' && _restarting) {
          setState(() {
            _restarting = false;
            _containerStopped = false;
          });
        }
      },
      onSharedTerminalDeleted: (msg) {
        if (!mounted) return;
        final deletedUserId = msg['user_id'] as String? ?? '';
        final deletedWindow = msg['window_name'] as String? ?? '';
        final deletedWid = msg['window_id'] as String? ?? '';
        final wasViewing =
            _activeSharedTerminal != null &&
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
      },
      onPermissionError: (error) {
        if (mounted) setState(() => _error = error);
      },
    );

    await _connector!.connect();
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
        final ids = wsClient.terminalWindows
            .map((w) => w['id'] as String?)
            .toSet();
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
          _activeSharedTerminal = {'user_id': userId, 'window_id': windowId};
          wsClient.sendJoinSharedTerminal(userId, windowId);
        }
      }
      if (mounted) setState(() {});
    }
    // Detect WebSocket disconnect after we were connected
    if (!wsClient.connected && !_connecting && !_disconnected) {
      setState(() => _disconnected = true);
    }
    // Rebuild when reconnecting state changes (including when it stops)
    if (_disconnected) {
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
      () => _activeSharedTerminal = {'user_id': userId, 'window_id': windowId},
    );
    // Clear the terminal so stale content from the previous session
    // doesn't linger while the join is in progress.
    _terminalKey.currentState?.clearScreen();
    wsClient.sendJoinSharedTerminal(userId, windowId);
  }

  Future<void> _reconnect() async {
    setState(() => _connecting = true);
    await _connector?.reconnect();
  }

  @override
  void deactivate() {
    final wsClient = context.read<WsClient>();
    wsClient.removeListener(_onClientUpdate);
    wsClient.disconnectWorkspace();
    _connector?.dispose();
    super.deactivate();
  }

  @override
  void dispose() {
    for (final plugin in _plugins) {
      plugin.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null) return _buildErrorView();
    if (_connecting) return _buildConnectingView();

    final wsClient = context.read<WsClient>();
    final authToken = context.read<AuthService>().token;

    return Scaffold(
      appBar: AppBar(
        title: AppBarTitle(
          title: _workspaceName.isNotEmpty ? _workspaceName : 'Workspace',
        ),
        actions: [
          for (final plugin in _plugins)
            if (plugin.buildAppBarAction(context) != null)
              plugin.buildAppBarAction(context)!,
          const AppBarActions(),
        ],
      ),
      body: Stack(
        children: [
          _buildIdeLayout(wsClient, authToken),
          for (final plugin in _plugins)
            if (plugin.buildOverlay(context) != null)
              plugin.buildOverlay(context)!,
          if (_containerStopped)
            buildContainerStoppedOverlay(
              restarting: _restarting,
              stopReason: _stopReason,
              onRestart: _restartContainer,
              onBack: () => context.go('/workspaces'),
            ),
          if (_disconnected && !_containerStopped)
            buildDisconnectedOverlay(
              reconnecting: wsClient.reconnecting,
              reconnectAttempt: wsClient.reconnectAttempt,
              onReconnect: _reconnect,
              onBack: () => context.go('/workspaces'),
            ),
        ],
      ),
    );
  }

  Widget _buildErrorView() {
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

  Widget _buildConnectingView() {
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

  Widget _buildIdeLayout(WsClient wsClient, String? authToken) {
    return IdeLayout(
      fileViewer: FileViewerPanel(
        key: _fileViewerKey,
        wsClient: wsClient,
        workspaceId: widget.workspaceId,
        authToken: authToken,
        userHome: wsClient.userHome,
        registry: _fileRenderers,
      ),
      terminal: TerminalTabsView(
        wsClient: wsClient,
        terminalKey: _terminalKey,
        onPathTap: _handleTerminalPathTap,
        selectedOwnWindowId: _selectedOwnWindowId,
        activeSharedTerminal: _activeSharedTerminal,
        hasPerm: _hasPerm,
        onSwitchToIsolated: _switchToIsolated,
        onJoinShared: _joinShared,
      ),
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
    );
  }
}
