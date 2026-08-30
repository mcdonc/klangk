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
import 'restart_flow.dart';
import 'workspace_overlays.dart';
import 'consent_banner.dart';
import 'marking_banner.dart';
import 'server_schedule_banner.dart';
import 'consent_decider_service.dart';
import 'consent_rules_panel.dart';
import 'package:http/http.dart' as http;
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';
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
  bool _connecting = true;
  String? _error;
  String _workspaceName = '';
  bool _containerStopped = false;
  // #2527: last host lifecycle notice rendered (identity-compared so a
  // notifyListeners without a change doesn't re-fire the snackbar).
  String? _lastHostNotice = 'sentinel';
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

  /// The workspace's egress_mode ('static' or 'interactive'), captured on
  /// load. When interactive, a [ConsentDeciderService] + [ConsentBanner] let
  /// the deciding user allow/deny held egress requests inline (#2246).
  String _egressMode = 'interactive';

  /// #2768: the effective classification marking — the workspace's own
  /// `classification_banner`, else the deploy default. Empty = no marking
  /// configured: no banner, no reserved screen space. Rendered as
  /// persistent top + bottom bars around the whole page (STIG: "top and
  /// the bottom of screens").
  String _marking = '';

  ConsentDeciderService? _consent;
  late final ToolPluginRegistry _featureRegistry;
  late final List<ToolPlugin> _features;
  late final List<WorkspaceTabPlugin> _featureTabs;
  late final FileRendererRegistry _fileRenderers;
  WorkspaceConnector? _connector;

  /// #2768: subscription to workspace-row changes (marking edits) so the
  /// classification banner re-resolves without re-entering the page.
  StreamSubscription<void>? _markingSub;

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
    _featureRegistry = ToolPluginRegistry();
    // Features are registered once in main() — reuse them here.
    _features = _featureRegistry.plugins.toList();
    // Feature-contributed workspace tabs are likewise registered once in
    // main() (active-filtered) — reuse the singleton registry (#1975).
    _featureTabs = WorkspaceTabRegistry().tabs;
    _fileRenderers = buildFileRendererRegistry(_features);
    _fetchWorkspaceName();
    // #2768: re-resolve the effective marking when the workspace row
    // changes (the server notifies on a classification_banner edit, so
    // the banner updates live after saving in the settings panel).
    _markingSub = context
        .read<WsClient>()
        .workspacesChanged
        .listen((_) => _fetchWorkspaceName());
    WidgetsBinding.instance.addPostFrameCallback((_) => _connectToWorkspace());
  }

  Future<void> _fetchWorkspaceName() async {
    final auth = context.read<AuthService>();
    // #2768 review: re-fetch the deploy config before resolving the
    // marking — KLANGKD_CLASSIFICATION_BANNER can change (SIGHUP reload)
    // under a live session, and the marking must re-resolve against the
    // current value, not the one cached at login. Runs on mount and on
    // every workspaces-changed push.
    await auth.refreshDeployConfig();
    // Fetch per-resource permissions BEFORE the workspace row: the
    // consent init below gates on egress-consent (#2883), so the
    // permission list must be loaded by then.
    await _fetchPermissions();
    try {
      final ws = await _findWorkspace(auth, '/api/v1/workspaces') ??
          await _findWorkspace(auth, '/api/v1/workspaces/shared');
      if (ws != null && mounted) {
        final name = ws['name'] as String?;
        setState(() {
          if (name != null) _workspaceName = name;
          _egressMode = ws['egress_mode']?.toString() ?? 'interactive';
          _marking = effectiveMarking(
            ws['classification_banner']?.toString(),
            auth.defaultClassificationBanner,
          );
        });
        if (name != null) setPageTitle(_workspaceName);
        _maybeInitConsent(auth.token);
      }
    } catch (e) {
      debugPrint('[WorkspacePage] fetch workspace name failed: $e');
    }
  }

  Future<void> _fetchPermissions() async {
    final auth = context.read<AuthService>();
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

  Future<Map<String, dynamic>?> _findWorkspace(
    AuthService auth,
    String url,
  ) async {
    final response = await auth.authGet(url);
    if (response.statusCode == 200) {
      final workspaces = jsonDecode(response.body) as List;
      for (final ws in workspaces) {
        if (ws['id'] == widget.workspaceId) {
          return ws as Map<String, dynamic>;
        }
      }
    }
    return null;
  }

  /// Create + connect the consent-decider service for an interactive-mode
  /// workspace (#2246). Static (or unknown) mode mounts no banner; nor
  /// does a member without `egress-consent` (#2883) — a spectator is
  /// watch-only and must not decide egress, so neither the banner nor
  /// the Network tab ever mount for them.
  void _maybeInitConsent(String? token) {
    if (_egressMode != 'interactive' || token == null) return;
    if (!_hasPerm('egress-consent')) return;
    _consent ??= ConsentDeciderService(
      workspaceId: widget.workspaceId,
      token: token,
    )..connect();
  }

  bool _hasPerm(String perm) =>
      _workspacePermissions.contains(perm) ||
      _workspacePermissions.contains('*');

  Future<void> _connectToWorkspace() async {
    final wsClient = context.read<WsClient>();

    _connector = WorkspaceConnector(
      wsClient: wsClient,
      workspaceId: widget.workspaceId,
      featureRegistry: _featureRegistry,
      // #2710: on a deploy that disabled the browser-delegate bridge, the
      // tab never subscribes to bridge requests.
      browserDelegateEnabled:
          context.read<AuthService>().browserDelegateEnabled,
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
          // #2661: a stop whose reason is the server's own recycle is
          // NOT user-actionable — the server stays up, the WebSocket
          // drops with 1012 and auto-reconnects, and auto-start brings
          // the workspace back. Raising the blocking "stopped — click
          // Restart" overlay here would demand a click for a cycle
          // that needs none; the reconnect overlay owns the gap.
          if (reason == 'server recycle') return;
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
      // #2676: the server refuses a failed restart with an error frame
      // (instead of dropping the socket), so the spinner must clear here —
      // no container_ready ever arrives for it.
      onRestartError: (error) {
        if (!mounted || !_restarting) return;
        setState(() => _restarting = false);
        ScaffoldMessenger.of(context)
          ..hideCurrentSnackBar()
          ..showSnackBar(
            SnackBar(
              content: Text('Restart failed: $error'),
              duration: const Duration(seconds: 6),
              behavior: SnackBarBehavior.floating,
            ),
          );
      },
      onSharedTerminalDeleted: (msg) {
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
      },
      onPageError: (error) {
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
    // #2527: host lifecycle notices (restart phases / shutdown) surface as
    // a transient floating snackbar — notification only; the reconnect
    // overlay/logic is untouched, so reconnection is never visually
    // impeded. host_started clears the notice (the "Reconnected" snackbar
    // above already covers the happy path).
    final notice = wsClient.hostNotice;
    if (!identical(notice, _lastHostNotice) && mounted) {
      _lastHostNotice = notice;
      if (notice != null) {
        ScaffoldMessenger.of(context)
          ..hideCurrentSnackBar()
          ..showSnackBar(
            SnackBar(
              content: Text(notice),
              duration: const Duration(seconds: 8),
              behavior: SnackBarBehavior.floating,
              width: 260,
            ),
          );
      } else {
        ScaffoldMessenger.of(context).hideCurrentSnackBar();
      }
    }
    // Rebuild only when terminal/shared tab lists actually change.
    if (!identical(wsClient.terminalWindows, _prevTerminalWindows) ||
        !identical(wsClient.sharedTerminals, _prevSharedTerminals)) {
      // Snapshot the previous window ids BEFORE reassigning, so we can tell a
      // switch to an existing window apart from a brand-new window becoming
      // active.
      final prevWindowIds =
          _prevTerminalWindows.map((w) => w['id'] as String?).toSet();
      _prevTerminalWindows = wsClient.terminalWindows;
      _prevSharedTerminals = wsClient.sharedTerminals;
      // Track selected own-window: initialize on first message, or
      // reset if the selected window was closed.
      if (wsClient.terminalWindows.isNotEmpty) {
        final ids =
            wsClient.terminalWindows.map((w) => w['id'] as String?).toSet();
        // Follow tmux's active window on a switch to an EXISTING window (or
        // the first load) so the Flutter tab matches the status-bar selection
        // (#2171) — but NOT when a brand-new window just became active. The
        // Flutter "+" creates a window that tmux selects; keep focus where the
        // user had it instead of stealing it to the new tab (#2176).
        String? activeId;
        for (final w in wsClient.terminalWindows) {
          if (w['active'] == true) {
            activeId = w['id'] as String?;
            break;
          }
        }
        final followActive = activeId != null &&
            (prevWindowIds.isEmpty || prevWindowIds.contains(activeId));
        if (followActive) {
          _selectedOwnWindowId = activeId;
        } else if (_selectedOwnWindowId == null ||
            !ids.contains(_selectedOwnWindowId)) {
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

  Future<void> _restartContainer() async {
    setState(() => _restarting = true);
    final wsClient = context.read<WsClient>();
    // #2674: with the WebSocket down (host restarted while this page was
    // open, auto-reconnect exhausted) a restart_container send would be
    // silently dropped and the spinner would never clear — reconnect
    // instead; the workspace_connect on reconnect auto-starts the
    // container and container_ready clears the overlay.
    final result = await requestContainerRestart(
      wsClient: wsClient,
      workspaceId: widget.workspaceId,
    );
    if (!mounted) return;
    if (result == RestartRequestResult.failed) {
      // Server still unreachable: restore the button so the user can
      // retry rather than spinning forever.
      setState(() => _restarting = false);
    }
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
    _markingSub?.cancel();
    _consent?.dispose();
    for (final feature in _features) {
      feature.dispose();
    }
    // Mirror tool plugins: release feature-tab resources on workspace close
    // (#1975). The registry is a singleton populated once in main(), so this
    // calls per-tab dispose() — not disposeAll() — to avoid clearing tabs
    // that the next workspace page (same app session) will reuse. Tab plugins
    // with real resources must
    // tolerate being re-registered, same as tool plugins already do.
    for (final tab in _featureTabs) {
      tab.dispose();
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null) return _buildErrorView();
    if (_connecting) return _buildConnectingView();

    final wsClient = context.read<WsClient>();
    final authToken = context.read<AuthService>().token;

    // #2768: the classification banner wraps the whole page (top + bottom)
    // — above the AppBar and outside the body, so it can never be
    // displaced by the transient banners (those stack below it, inside
    // the body). Empty marking renders nothing and reserves no space.
    return Column(
      children: [
        MarkingBanner(text: _marking),
        Expanded(
          child: Scaffold(
            appBar: AppBar(
              title: AppBarTitle(
                title: _workspaceName.isNotEmpty ? _workspaceName : 'Workspace',
              ),
              actions: [
                for (final feature in _features)
                  if (feature.buildAppBarAction(context) != null)
                    feature.buildAppBarAction(context)!,
                const AppBarActions(),
              ],
            ),
            body: Column(
              children: [
                if (_egressMode == 'interactive' && _consent != null)
                  ConsentBanner(service: _consent!),
                ServerScheduleBanner(),
                Expanded(
                  child: Stack(
                    children: [
                      _buildIdeLayout(wsClient, authToken),
                      for (final feature in _features)
                        if (feature.buildOverlay(context) != null)
                          feature.buildOverlay(context)!,
                      if (_containerStopped)
                        buildContainerStoppedOverlay(
                          restarting: _restarting,
                          stopReason: _stopReason,
                          onRestart: _restartContainer,
                          onBack: () => context.go('/workspaces'),
                        ),
                      if (_disconnected &&
                          !_containerStopped &&
                          !wsClient.authFailed)
                        buildDisconnectedOverlay(
                          reconnecting: wsClient.reconnecting,
                          reconnectAttempt: wsClient.reconnectAttempt,
                          onReconnect: _reconnect,
                          onBack: () => context.go('/workspaces'),
                        ),
                    ],
                  ),
                ),
              ],
            ),
          ),
        ),
        MarkingBanner(text: _marking),
      ],
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
      // #2886: no `files` permission → no Files tab at all (spectators,
      // terminal-only shares) — same my-permissions gate as Sharing/Network,
      // so the panel never fetches a listing the backend will 403.
      fileViewer: _hasPerm('files')
          ? FileViewerPanel(
              key: _fileViewerKey,
              wsClient: wsClient,
              workspaceId: widget.workspaceId,
              authToken: authToken,
              userHome: wsClient.userHome,
              registry: _fileRenderers,
              canDownload: _hasPerm('files-download'),
              canWrite: _hasPerm('files-write'),
            )
          : null,
      featureTabs: _featureTabs,
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
      settings: _hasPerm('edit')
          ? WorkspaceSettingsPanel(
              workspaceId: widget.workspaceId,
              canExport: _hasPerm('export'),
              onRestart: _restartContainer,
            )
          : null,
      // #2764: the Sharing tab serves both sharing powers — `share`
      // holders get the role buckets, `change-acls` holders (at least)
      // the Advanced ACL editor.
      sharing: _hasPerm('share') || _hasPerm('change-acls')
          ? WorkspaceSharingPanel(
              workspaceId: widget.workspaceId,
              canShare: _hasPerm('share'),
              canEditAcl: _hasPerm('change-acls'),
            )
          : null,
      consentRules:
          _consent != null ? ConsentRulesPanel(service: _consent!) : null,
      terminalKey: _terminalKey,
      fileViewerKey: _fileViewerKey,
      initialFile: widget.initialFile,
      initialDir: widget.initialDir,
      debug: DebugPanel(wsClient: wsClient),
    );
  }
}
