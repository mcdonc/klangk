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
import '../widgets/confetti.dart';
import '../file_viewer/file_viewer_panel.dart';
import '../layout/ide_layout.dart';
import '../output/output_panel.dart';
import '../terminal/chat_panel.dart';

class WorkspacePage extends StatefulWidget {
  final String workspaceId;

  const WorkspacePage({super.key, required this.workspaceId});

  @override
  State<WorkspacePage> createState() => _WorkspacePageState();
}

class _WorkspacePageState extends State<WorkspacePage> {
  String get _baseUrl => baseUrl;
  bool _connecting = true;
  String? _error;
  String _workspaceName = '';
  bool _agentRunning = false;
  bool _showConfetti = false;
  StreamSubscription? _eventSub;

  @override
  void initState() {
    super.initState();
    _fetchWorkspaceName();
    // Delay connect until after first frame so child widgets (OutputPanel etc.) are subscribed
    WidgetsBinding.instance.addPostFrameCallback((_) => _connectToWorkspace());
  }

  Future<void> _fetchWorkspaceName() async {
    final token = context.read<AuthService>().token;
    try {
      final response = await http.get(
        Uri.parse('$_baseUrl/workspaces'),
        headers: {if (token != null) 'Authorization': 'Bearer $token'},
      );
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

    // Track agent running state and celebrate events
    _eventSub = aguiClient.events.listen((event) {
      if (event.type == AguiEventType.runStarted) {
        if (mounted) setState(() => _agentRunning = true);
      } else if (event.type == AguiEventType.runFinished || event.type == AguiEventType.runError) {
        if (mounted) setState(() => _agentRunning = false);
      } else if (event.type == AguiEventType.toolCallStart && event.toolCallName == 'celebrate') {
        if (mounted) setState(() => _showConfetti = true);
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
            terminal: ChatPanel(
              aguiClient: aguiClient,
              workspaceId: widget.workspaceId,
              authToken: authToken,
            ),
            fileViewer: FileViewerPanel(
              aguiClient: aguiClient,
              workspaceId: widget.workspaceId,
              authToken: authToken,
            ),
            output: OutputPanel(aguiClient: aguiClient),
          ),
          if (_showConfetti)
            Positioned.fill(
              child: ConfettiOverlay(
                onComplete: () => setState(() => _showConfetti = false),
              ),
            ),
        ],
      ),
    );
  }
}
