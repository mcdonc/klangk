import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:provider/provider.dart';
import '../ws/ws_client.dart';
import '../auth/auth_service.dart';
import 'package:bark_plugin_api/bark_plugin_api.dart';
import 'package:bark_plugins/bark_plugins.dart';
import '../utils/page_title.dart';
import '../widgets/bark_logo.dart';
import '../widgets/app_bar_actions.dart';
import '../file_viewer/file_viewer_panel.dart';
import '../layout/ide_layout.dart';
import '../terminal/container_terminal.dart';
import '../browser/browser_delegate.dart';
import '../debug/debug_panel.dart';

class WorkspacePage extends StatefulWidget {
  final String workspaceId;

  const WorkspacePage({super.key, required this.workspaceId});

  @override
  State<WorkspacePage> createState() => _WorkspacePageState();
}

class _WorkspacePageState extends State<WorkspacePage> {
  final _terminalKey = GlobalKey<ContainerTerminalState>();
  final _fileViewerKey = GlobalKey<FileViewerPanelState>();
  bool _connecting = true;
  String? _error;
  String _workspaceName = '';
  bool _containerStopped = false;
  bool _restarting = false;
  String _stopReason = '';
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
    try {
      final response = await context.read<AuthService>().authGet('/workspaces');
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
  }

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
      setState(() => _connecting = false);
      WidgetsBinding.instance.addPostFrameCallback((_) {
        wsClient.sendUiReady();
      });
    }
  }

  void _restartContainer() {
    setState(() => _restarting = true);
    final wsClient = context.read<WsClient>();
    wsClient.sendRestartContainer();
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
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: Color(0xFF1A237E)),
          onPressed: () => context.go('/workspaces'),
        ),
        title: MouseRegion(
          cursor: SystemMouseCursors.click,
          child: GestureDetector(
            onTap: () => context.go('/'),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                const BarkLogo(height: 36),
                if (_workspaceName.isNotEmpty) ...[
                  const SizedBox(width: 12),
                  Text(_workspaceName, style: const TextStyle(fontSize: 16)),
                ],
              ],
            ),
          ),
        ),
        actions: [
          if (context.watch<AuthService>().email != null)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              child: Center(
                child: Text(
                  context.watch<AuthService>().email!,
                  style: const TextStyle(fontSize: 13),
                ),
              ),
            ),
          const AppBarActions(),
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
            terminal: ContainerTerminal(key: _terminalKey, wsClient: wsClient),
            terminalKey: _terminalKey,
            fileViewerKey: _fileViewerKey,
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
                              backgroundColor: const Color(0xFF5B8C5A),
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
