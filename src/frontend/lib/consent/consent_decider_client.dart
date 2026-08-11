/// Egress-consent decider client for the Flutter workspace page (#2333).
///
/// The in-app mirror of the TUI `consent-decide` client
/// (`src/klangk/klangk/cli/tui/consent.py`, PR #2320). Opens its own WebSocket
/// to the server's `/ws/consent-decider?token=<JWT>&workspace=<id>` endpoint
/// (#2244), stays registered via pings (must beat `consent_decider_timeout`,
/// 45s), surfaces held egress requests, and sends allow/deny verdicts that the
/// coordinator applies to the held sidecar connection (#2311).
///
/// This is a **separate** WebSocket from the main [WsClient] (which speaks
/// `/ws` for terminal/chat/etc). The decider stream is its own endpoint with
/// its own authz + frame protocol, so — like the TUI — it gets its own
/// connection. We reuse the WsClient *patterns* (JWT from AuthService,
/// reconnect with backoff, auth-failure close-code handling, test channel
/// factory) rather than a bespoke transport.
///
/// Fail-closed: while disconnected the decider is deregistered server-side,
/// so in-flight holds auto-deny on their own timeout — never silently
/// allowed. The UI reflects this by showing a disconnected/warning state.
///
/// The client is a [ChangeNotifier]; the overlay widget listens and rebuilds
/// on connection/data changes. A 1s tick (active only while requests are
/// pending) drives the auto-deny countdown display.
library;

import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import '../auth/auth_service.dart';
import 'consent_request.dart';

/// Verdict decision values (mirror the server's
/// `model/egress_consent.py`). v1 sends `scope=once` only (#2333 non-goal:
/// richer scope/duration is tracked in #2328 for the TUI; the Flutter client
/// can follow once that lands).
const _kDecisionAllowed = 'allowed';
const _kDecisionDenied = 'denied';
const _kScopeOnce = 'once';

/// Server default `egress_consent_timeout` (settings.py), in seconds. The
/// client cannot learn the server's value, so it defaults to the server
/// default for the countdown display. The server is the source of truth —
/// it auto-denies at the real timeout regardless of what this shows.
const double kDefaultHoldTimeoutSeconds = 120.0;

/// Pings must beat the server's `consent_decider_timeout` (45s) or the
/// decider is reaped and the workspace reverts to static. 15s leaves margin,
/// matching the TUI client's interval.
const Duration _kPingInterval = Duration(seconds: 15);

/// Reconnect backoff caps (seconds), matching the TUI. Caps the spin on a
/// repeatedly-dropping server.
const List<double> _kReconnectDelays = [1.0, 2.0, 5.0];

/// A live consent decider for one workspace, owning its WS lifecycle.
///
/// Construct with the workspace id + an [AuthService] (read live for the
/// token so reconnects pick up a refreshed JWT). Call [connect] to open the
/// socket, [dispose] to tear it down. Listen via [addListener].
class ConsentDeciderClient extends ChangeNotifier {
  ConsentDeciderClient({
    required this.workspaceId,
    required AuthService auth,
    double holdTimeoutSeconds = kDefaultHoldTimeoutSeconds,
    Duration pingInterval = _kPingInterval,
    Duration countdownInterval = const Duration(seconds: 1),
    List<double> reconnectDelays = _kReconnectDelays,
  })  : _auth = auth,
        _holdTimeoutSeconds = holdTimeoutSeconds,
        _pingInterval = pingInterval,
        _countdownInterval = countdownInterval,
        _reconnectDelays = reconnectDelays;

  final String workspaceId;
  final AuthService _auth;
  final double _holdTimeoutSeconds;
  final Duration _pingInterval;
  final Duration _countdownInterval;
  final List<double> _reconnectDelays;

  WebSocketChannel? _channel;
  bool _connected = false;
  bool _connecting = false;
  bool _manualDisconnect = false;
  bool _authFailed = false;
  bool _disposed = false;
  Timer? _pingTimer;
  Timer? _reconnectTimer;
  Timer? _countdownTimer;
  int _reconnectAttempt = 0;
  StreamSubscription? _sub;

  /// Override for testing to inject a fake channel factory (mirrors
  /// [WsClient.testChannelFactory]).
  @visibleForTesting
  static WebSocketChannel Function(Uri uri)? testChannelFactory;

  /// Override for testing to control reconnect backoff (mirrors
  /// [WsClient.testBackoffOverride]).
  @visibleForTesting
  static Duration Function(int attempt)? testBackoffOverride;

  /// Pending held requests keyed by id.
  final Map<String, ConsentRequest> _pending = {};

  /// Whether egress filtering is paused for the workspace, from the
  /// `egress_rules` frame's `paused` field. `null` = not yet received (or the
  /// server sent `null`, which it always does today — #2332 pause control is
  /// not yet landed).
  bool? _paused;

  // -- public read-only state ------------------------------------------------

  bool get connected => _connected;
  bool get connecting => _connecting;
  bool get authFailed => _authFailed;

  /// Pending held requests, oldest-first.
  List<ConsentRequest> get pending => _pending.values.toList()
    ..sort((a, b) => a.requestedAt.compareTo(b.requestedAt));

  /// Whether at least one held request is awaiting a verdict.
  bool get hasPending => _pending.isNotEmpty;

  /// Paused state from the last `egress_rules` frame (null = unknown).
  bool? get paused => _paused;

  /// Seconds until a hold's countdown hits zero (clamped at 0). UX hint only
  /// — the server auto-denies at the real timeout regardless.
  double remaining(ConsentRequest req) {
    final now = DateTime.now().millisecondsSinceEpoch / 1000.0;
    final left = req.requestedAt + _holdTimeoutSeconds - now;
    return left < 0 ? 0.0 : left;
  }

  // -- lifecycle -------------------------------------------------------------

  /// Open the decider socket. Safe to call once (the overlay calls it on
  /// mount). Reconnects are handled internally on drop.
  Future<void> connect() async {
    if (_disposed || _connecting || _connected) return;
    final token = _auth.token;
    if (token == null) return;
    _connecting = true;
    notifyListeners();
    try {
      await _openSocket(token);
    } finally {
      _connecting = false;
    }
    if (!_disposed) notifyListeners();
  }

  Future<void> _openSocket(String token) async {
    final channel = testChannelFactory != null
        ? testChannelFactory!(Uri())
        // coverage:ignore-start
        : WebSocketChannel.connect(_deciderUri(token));
    // coverage:ignore-end
    _channel = channel;
    try {
      await channel.ready;
    } catch (_) {
      // Ready failed (connect refused / auth close). Inspect the close code
      // to decide between auth-failure (stop) and transient (reconnect).
      _handleClose(channel.closeCode);
      if (!_authFailed && !_disposed) {
        // A connect-time failure never opened the stream, so its onDone won't
        // fire — schedule the reconnect ourselves.
        _scheduleReconnect();
      }
      return;
    }
    _authFailed = false;
    _connected = true;
    _reconnectAttempt = 0;
    // The server's snapshot (sent immediately on connect) is authoritative
    // for currently-held requests: drop anything stale from a prior session
    // so rows that resolved while we were disconnected — and thus never sent
    // us an egress_resolved — don't linger as ghosts. Matches the TUI's
    // reset() on each pump start.
    _pending.clear();
    _paused = null;
    _startPing();
    _sub = channel.stream.listen(
      _onData,
      onError: (Object e) {
        debugPrint('[ConsentDecider] stream error: $e');
      },
      onDone: () {
        _teardownConnection();
        _handleClose(channel.closeCode);
        if (!_manualDisconnect && !_authFailed && !_disposed) {
          _scheduleReconnect();
        }
      },
    );
  }

  // coverage:ignore-start
  Uri _deciderUri(String token) {
    final loc = Uri.base;
    final wsScheme = loc.scheme == 'https' ? 'wss' : 'ws';
    final base = baseUrl; // from klangk_plugin_api ('' or '/klangk')
    return Uri.parse(
      '$wsScheme://${loc.host}:${loc.port}$base/ws/consent-decider'
      '?token=${Uri.encodeQueryComponent(token)}'
      '&workspace=${Uri.encodeQueryComponent(workspaceId)}',
    );
    // coverage:ignore-end
  }

  void _onData(dynamic data) {
    if (data is! String) return;
    Map<String, dynamic> msg;
    try {
      final decoded = jsonDecode(data);
      if (decoded is! Map<String, dynamic>) return;
      msg = decoded;
    } catch (_) {
      return; // malformed frame — ignore
    }
    final type = msg['type'];
    var changed = false;
    switch (type) {
      case 'egress_request':
        final req = parseConsentRequest(msg['request']);
        if (req != null) {
          _pending[req.id] = req;
          _ensureCountdownTimer();
          changed = true;
        }
        break;
      case 'egress_resolved':
        final rid = msg['request_id'];
        if (rid is String) {
          _pending.remove(rid);
          changed = true;
        }
        break;
      case 'egress_rules':
        // paused is always null today (#2332 not landed); track anyway for
        // forward-compat with a future pause-control backend.
        _paused = msg['paused'] as bool?;
        changed = true;
        break;
      case 'pong':
      case 'error':
        // pong confirms liveness (no state change); error is a rejected
        // verdict (server already logged it). Neither needs UI action.
        break;
      default:
        break; // unknown frame — ignore
    }
    if (changed && !_disposed) notifyListeners();
  }

  // -- verdicts --------------------------------------------------------------

  /// Send an allow verdict for [requestId] (scope=once). No-op if not
  /// connected; the UI shows a disconnected warning in that case.
  void allow(String requestId) => _sendVerdict(requestId, _kDecisionAllowed);

  /// Send a deny verdict for [requestId] (scope=once).
  void deny(String requestId) => _sendVerdict(requestId, _kDecisionDenied);

  void _sendVerdict(String requestId, String decision) {
    final channel = _channel;
    if (channel == null) return;
    try {
      channel.sink.add(jsonEncode({
        'type': 'verdict',
        'request_id': requestId,
        'decision': decision,
        'scope': _kScopeOnce,
      }));
    } catch (e) {
      debugPrint('[ConsentDecider] verdict send failed: $e');
    }
  }

  // -- liveness / reconnect --------------------------------------------------

  void _startPing() {
    _pingTimer?.cancel();
    _pingTimer = Timer.periodic(_pingInterval, (_) {
      final channel = _channel;
      if (channel == null) return;
      try {
        channel.sink.add(jsonEncode({'type': 'ping'}));
      } catch (e) {
        debugPrint('[ConsentDecider] ping send failed: $e');
      }
    });
  }

  void _ensureCountdownTimer() {
    // Only run the 1s countdown tick while there are holds to count down;
    // idle workspaces pay nothing. The tick just refreshes the display —
    // the server's egress_resolved frame (or the next snapshot) removes
    // rows; we don't prune locally, matching the TUI.
    if (_countdownTimer != null) return;
    _countdownTimer = Timer.periodic(_countdownInterval, (_) {
      if (_disposed) return;
      if (_pending.isEmpty) {
        _countdownTimer?.cancel();
        _countdownTimer = null;
        return;
      }
      notifyListeners();
    });
  }

  void _handleClose(int? code) {
    if (code == 4001 || code == 4002) {
      // Auth-related close (missing/expired token). Stop reconnecting — the
      // app's auth guard redirects to login via the main WsClient's logout.
      _authFailed = true;
    }
  }

  void _scheduleReconnect() {
    if (_disposed || _authFailed) return;
    _reconnectAttempt++;
    final delay = testBackoffOverride != null
        ? testBackoffOverride!(_reconnectAttempt)
        : _backoffDelay(_reconnectAttempt);
    _reconnectTimer?.cancel();
    _reconnectTimer = Timer(delay, () {
      _reconnectTimer = null;
      if (_disposed) return;
      // Re-read the token: it may have refreshed while we were backed off.
      if (_auth.token == null) return;
      connect();
    });
  }

  Duration _backoffDelay(int attempt) {
    // coverage:ignore-start
    final idx = min(attempt - 1, _reconnectDelays.length - 1);
    final base = _reconnectDelays[idx < 0 ? 0 : idx];
    return Duration(milliseconds: (base * 1000).round());
    // coverage:ignore-end
  }

  void _teardownConnection() {
    _pingTimer?.cancel();
    _pingTimer = null;
    _sub?.cancel();
    _sub = null;
    _channel?.sink.close();
    _channel = null;
    final wasConnected = _connected;
    _connected = false;
    // While disconnected the server has deregistered us, so any holds we
    // still show will auto-deny on their own timeout. Keep them visible
    // (with a disconnected warning) rather than clearing — the user can see
    // what's pending. They clear on reconnect snapshot or via egress_resolved.
    if (wasConnected && !_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    _manualDisconnect = true;
    _connected = false;
    _pingTimer?.cancel();
    _reconnectTimer?.cancel();
    _countdownTimer?.cancel();
    _sub?.cancel();
    _channel?.sink.close(1000, 'client dispose');
    _channel = null;
    _pending.clear();
    super.dispose();
  }
}
