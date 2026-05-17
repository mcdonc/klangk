import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import 'package:http/http.dart' as http;
import 'package:provider/provider.dart';
import '../agui/agui_client.dart';
import '../agui/agui_events.dart';
import '../auth/auth_service.dart';
import '../utils/backend_url.dart';
import '../utils/page_title.dart';
import '../widgets/bark_logo.dart';
import '../file_viewer/file_viewer_panel.dart';
import '../layout/ide_layout.dart';
import '../output/output_panel.dart';
import '../terminal/container_terminal.dart';
import '../terminal/chat_panel.dart';
import '../tools/tool_plugin.dart';
import '../tools/plugins_generated.dart';

class WorkspacePage extends StatefulWidget {
  final String workspaceId;

  const WorkspacePage({super.key, required this.workspaceId});

  @override
  State<WorkspacePage> createState() => _WorkspacePageState();
}

class _WorkspacePageState extends State<WorkspacePage> {
  String get _baseUrl => baseUrl;
  final _terminalKey = GlobalKey<ContainerTerminalState>();
  final _fileViewerKey = GlobalKey<FileViewerPanelState>();
  bool _connecting = true;
  String? _error;
  String _workspaceName = '';
  bool _agentRunning = false;
  StreamSubscription? _eventSub;
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
    // Delay connect until after first frame so child widgets (OutputPanel etc.) are subscribed
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
    final aguiClient = context.read<AguiClient>();

    // Connect WebSocket if not already connected
    if (!aguiClient.connected) {
      await aguiClient.connect();
    }

    if (!aguiClient.connected) {
      setState(() {
        _connecting = false;
        _error = 'Failed to connect to server';
      });
      return;
    }

    // Connect to workspace
    aguiClient.connectWorkspace(widget.workspaceId);

    // Listen for workspace_ready
    aguiClient.addListener(_onClientUpdate);

    // Track agent running state and extension UI requests
    _eventSub = aguiClient.events.listen((event) {
      if (event.type == AguiEventType.runStarted) {
        if (mounted) setState(() => _agentRunning = true);
      } else if (event.type == AguiEventType.runFinished ||
          event.type == AguiEventType.runError) {
        if (mounted) setState(() => _agentRunning = false);
      } else if (event.type == AguiEventType.custom &&
          event.customName == 'extension_ui_request') {
        _handleExtensionUiRequest(event);
      }
    });

    // Also listen for errors
    aguiClient.errors.listen((error) {
      if (mounted) {
        setState(() => _error = error);
      }
    });
  }

  void _onClientUpdate() {
    final aguiClient = context.read<AguiClient>();
    if (aguiClient.currentWorkspaceId == widget.workspaceId) {
      setState(() => _connecting = false);
      // Send ui_ready after the IDE layout renders so debug pane receives events
      WidgetsBinding.instance.addPostFrameCallback((_) {
        aguiClient.sendUiReady();
      });
    }
  }

  Future<void> _handleExtensionUiRequest(AguiEvent event) async {
    final value = event.customValue as Map<String, dynamic>?;
    if (value == null) return;

    final id = value['id'] as String?;
    final method = value['method'] as String?;
    final title = value['title'] as String?;

    if (id == null || method == null) return;

    final aguiClient = context.read<AguiClient>();

    // Handle HOST_TOOL_REQUEST: extensions use ctx.ui.input("HOST_TOOL_REQUEST", jsonPayload)
    // to delegate actions to the browser. Dispatch to plugin registry.
    if (method == 'input' && title == 'HOST_TOOL_REQUEST') {
      final payload = value['placeholder'] as String? ?? '{}';
      try {
        final request = Map<String, dynamic>.from(
          json.decode(payload) as Map,
        );
        final action = request['action'] as String? ?? '';
        final responseText = await _pluginRegistry.dispatch(action, request);
        aguiClient.sendExtensionUiResponse(id, value: responseText);
      } catch (e) {
        aguiClient.sendExtensionUiResponse(id, value: 'Error: $e');
      }
      return;
    }

    // For other extension UI requests (select, confirm, etc.), cancel them
    // since we don't have a UI for them yet
    aguiClient.sendExtensionUiResponse(id, cancelled: true);
  }

  @override
  void deactivate() {
    // deactivate is called reliably even on browser back
    _eventSub?.cancel();
    _eventSub = null;
    final aguiClient = context.read<AguiClient>();
    aguiClient.removeListener(_onClientUpdate);
    aguiClient.disconnectWorkspace();
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

    final aguiClient = context.read<AguiClient>();
    final authToken = context.read<AuthService>().token;

    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          icon: const Icon(Icons.arrow_back, color: Color(0xFF1A237E)),
          onPressed: () => context.go('/workspaces'),
        ),
        title: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            const BarkLogo(height: 36),
            if (_workspaceName.isNotEmpty) ...[
              const SizedBox(width: 12),
              Text(_workspaceName, style: const TextStyle(fontSize: 16)),
            ],
          ],
        ),
        actions: [
          IconButton(
            icon: const Icon(Icons.logout, color: Color(0xFF1A237E)),
            tooltip: 'Logout',
            onPressed: () async {
              await context.read<AuthService>().logout();
              if (mounted) context.go('/login');
            },
          ),
        ],
      ),
      body: Stack(
        children: [
          IdeLayout(
            chat: ChatPanel(
              aguiClient: aguiClient,
              workspaceId: widget.workspaceId,
              authToken: authToken,
            ),
            fileViewer: FileViewerPanel(
              key: _fileViewerKey,
              aguiClient: aguiClient,
              workspaceId: widget.workspaceId,
              authToken: authToken,
            ),
            terminal:
                ContainerTerminal(key: _terminalKey, aguiClient: aguiClient),
            terminalKey: _terminalKey,
            fileViewerKey: _fileViewerKey,
            output: OutputPanel(aguiClient: aguiClient),
          ),
          for (final plugin in _plugins)
            if (plugin.buildOverlay(context) != null)
              plugin.buildOverlay(context)!,
        ],
      ),
    );
  }
}
