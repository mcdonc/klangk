/// Encapsulates WebSocket connect / reconnect / lifecycle-event wiring that
/// was previously inlined in `_WorkspacePageState._connectToWorkspace` (#971).
import 'dart:async';
import 'package:flutter/material.dart';
import '../ws/ws_client.dart';
import '../browser/browser_delegate.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

/// Manages connecting a [WsClient] to a workspace and wiring up the
/// event subscriptions (custom events, errors, shared-terminal deletions).
///
/// Call [connect] once after construction (typically from
/// `addPostFrameCallback`).  Call [dispose] from the owning State's
/// `deactivate`/`dispose`.
class WorkspaceConnector {
  final WsClient wsClient;
  final String workspaceId;
  final ToolPluginRegistry featureRegistry;

  /// Called when the connector finishes (successfully or not).
  final void Function({required bool connected, String? error}) onConnected;

  /// Called when a container lifecycle event fires.
  final void Function(String eventName, Map<String, dynamic>? value)
      onContainerEvent;

  /// Called when a shared terminal is deleted.
  final void Function(Map<String, dynamic> msg) onSharedTerminalDeleted;

  /// Called when a permission/auth error arrives.
  final void Function(String error) onPermissionError;

  /// Called when a non-permission error arrives while a restart is in
  /// flight (#2676): the server now refuses a failed container restart
  /// with an `error` frame instead of dropping the socket, so the page
  /// must clear its restart spinner on this callback. May fire for
  /// unrelated errors too — the owner filters by its own state.
  final void Function(String error)? onRestartError;

  /// #2710: whether the deploy's browser-delegate bridge is enabled
  /// (KLANGKD_BROWSER_DELEGATE_ENABLED via /config). When false, no
  /// [BrowserDelegate] is started — this tab never answers bridge
  /// requests (the server refuses them anyway). Defaults to true.
  final bool browserDelegateEnabled;

  BrowserDelegate? _browserDelegate;
  StreamSubscription<Map<String, dynamic>>? _customEventSub;
  StreamSubscription<String>? _errorSub;
  StreamSubscription<Map<String, dynamic>>? _sharedDeletedSub;

  WorkspaceConnector({
    required this.wsClient,
    required this.workspaceId,
    required this.featureRegistry,
    required this.onConnected,
    required this.onContainerEvent,
    required this.onSharedTerminalDeleted,
    required this.onPermissionError,
    this.onRestartError,
    this.browserDelegateEnabled = true,
  });

  /// Set true when the connect subscriptions go live, false in [dispose].
  /// Distinct from the delegate: a disabled bridge (#2710) starts no
  /// [BrowserDelegate], but the connector is still active.
  bool _active = false;

  /// Whether a connect/reconnect is currently in progress.
  bool _inProgress = false;

  Future<void> connect() async {
    if (_inProgress) {
      debugPrint('[WorkspaceConnector] connect already in progress, skipping');
      return;
    }
    _inProgress = true;
    try {
      await _doConnect();
    } finally {
      _inProgress = false;
    }
  }

  /// Whether [connect] has been called and subscriptions are active.
  bool get isActive => _active;

  /// Reconnect after a disconnect.  Tears down existing subscriptions
  /// first so we don't end up with duplicate listeners.
  Future<void> reconnect() async {
    if (_inProgress) {
      debugPrint(
        '[WorkspaceConnector] reconnect already in progress, skipping',
      );
      return;
    }
    _inProgress = true;
    try {
      dispose();
      await _doConnect();
    } finally {
      _inProgress = false;
    }
  }

  Future<void> _doConnect() async {
    debugPrint(
      '[WorkspaceConnector] connect called: ${DateTime.now()}',
    );

    if (!wsClient.connected) {
      debugPrint(
        '[WorkspaceConnector] calling wsClient.connect(): ${DateTime.now()}',
      );
      await wsClient.connect();
      debugPrint(
        '[WorkspaceConnector] wsClient.connect() returned: ${DateTime.now()}',
      );
    } else {
      debugPrint('[WorkspaceConnector] already connected, skipping connect()');
    }

    if (!wsClient.connected) {
      onConnected(connected: false, error: 'Failed to connect to server');
      return;
    }

    wsClient.connectWorkspace(workspaceId);

    // Start browser delegate for bridge requests — unless the deploy
    // disabled the bridge (#2710); then this tab never becomes a bridge
    // target.
    if (browserDelegateEnabled) {
      _browserDelegate = BrowserDelegate(wsClient, registry: featureRegistry);
      _browserDelegate!.start();
    }

    // Listen for container lifecycle events
    _customEventSub = wsClient.customEvents.listen((msg) {
      final event = msg['event'] as Map<String, dynamic>?;
      if (event == null) return;
      final name = event['name'] as String?;
      if (name != null) {
        onContainerEvent(name, event['value'] as Map<String, dynamic>?);
      }
    });

    // Listen for shared terminal deletions
    _sharedDeletedSub = wsClient.sharedTerminalDeleted.listen(
      onSharedTerminalDeleted,
    );

    // Listen for errors — only surface permission/auth errors.
    _errorSub = wsClient.errors.listen((error) {
      final lower = error.toLowerCase();
      if (lower.contains('permission') || lower.contains('denied')) {
        onPermissionError(error);
      } else {
        // #2676: anything else (e.g. a refused container restart) goes to
        // the optional restart-error hook; the owner decides relevance.
        onRestartError?.call(error);
      }
    });

    _active = true;
    onConnected(connected: true, error: null);
  }

  /// Tear down subscriptions and the browser delegate.  Safe to call
  /// multiple times.
  void dispose() {
    _active = false;
    _customEventSub?.cancel();
    _customEventSub = null;
    _errorSub?.cancel();
    _errorSub = null;
    _sharedDeletedSub?.cancel();
    _sharedDeletedSub = null;
    _browserDelegate?.stop();
    _browserDelegate = null;
  }
}
