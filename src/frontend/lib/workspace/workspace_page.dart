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
import '../layout/ide_layout.dart';
import '../terminal/ghostty_terminal.dart';
import '../browser/browser_delegate.dart';
import '../chat/workspace_chat.dart';
import '../debug/debug_panel.dart';
import 'workspace_settings_panel.dart';
import 'workspace_sharing_panel.dart';

class WorkspacePage extends StatefulWidget {
  final String workspaceId;

  const WorkspacePage({super.key, required this.workspaceId});

  @override
  State<WorkspacePage> createState() => _WorkspacePageState();
}

class _WorkspacePageState extends State<WorkspacePage> {
  final _terminalKey = GlobalKey<GhosttyTerminalState>();
  final _fileViewerKey = GlobalKey<FileViewerPanelState>();
  final _chatKey = GlobalKey<WorkspaceChatState>();
  bool _connecting = true;
  String? _error;
  String _workspaceName = '';
  int _chatUnread = 0;
  bool _containerStopped = false;
  bool _restarting = false;
  bool _disconnected = false;
  String _stopReason = '';
  List<String> _workspacePermissions = [];
  BrowserDelegate? _browserDelegate;
  StreamSubscription<Map<String, dynamic>>? _customEventSub;
  StreamSubscription<String>? _errorSub;
  late final ToolPluginRegistry _pluginRegistry;
  late final List<ToolPlugin> _plugins;

  @override
  void initState() {
    super.initState();
    _pluginRegistry = ToolPluginRegistry();
    _plugins = createAllPlugins();
    for (final plugin in _plugins) {
      _pluginRegistry.register(plugin);
    }
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

    // Listen for errors
    _errorSub = wsClient.errors.listen((error) {
      if (mounted) {
        setState(() => _error = error);
      }
    });
  }

  void _onClientUpdate() {
    final wsClient = context.read<WsClient>();
    if (wsClient.currentWorkspaceId == widget.workspaceId) {
      setState(() {
        _connecting = false;
        _disconnected = false;
      });
      WidgetsBinding.instance.addPostFrameCallback((_) {
        wsClient.sendUiReady();
      });
    }
    // Detect WebSocket disconnect after we were connected
    if (!wsClient.connected && !_connecting && !_disconnected) {
      setState(() => _disconnected = true);
    }
  }

  void _restartContainer() {
    setState(() => _restarting = true);
    final wsClient = context.read<WsClient>();
    wsClient.sendRestartContainer();
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
            ),
            terminal: GhosttyTerminal(key: _terminalKey, wsClient: wsClient),
            chat: _hasPerm('chat')
                ? WorkspaceChat(
                    key: _chatKey,
                    wsClient: wsClient,
                    onUnreadChanged: (count) {
                      if (mounted) setState(() => _chatUnread = count);
                    },
                  )
                : null,
            chatUnread: _chatUnread,
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
                child: _connecting
                    ? const Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          CircularProgressIndicator(color: Colors.white),
                          SizedBox(height: 12),
                          Text('Reconnecting...',
                              style: TextStyle(color: Colors.white)),
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
