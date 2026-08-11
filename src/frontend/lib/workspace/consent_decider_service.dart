/// Interactive egress-consent decider service for the web UI (#2246).
///
/// Mirrors the standalone ``klangk consent-decide`` TUI
/// (``cli/tui/consent.py``): connects to the server's ``/ws/consent-decider``
/// stream for a workspace, holds the snapshot + live pending egress requests,
/// and sends ``verdict`` frames so the deciding user can allow/deny each held
/// connection *while the sidecar holds it* (#2311). The verdict protocol +
/// frame shapes are duplicated from the server (``model/egress_consent.py``)
/// per the isolation boundary the web client shares with the CLI.
///
/// A decider that goes silent is reaped by the server after
/// ``consent_decider_timeout`` (45s), reverting the workspace to static
/// allow-list (fail-closed); this client pings every [_pingInterval] to stay
/// registered. On disconnect it reconnects with backoff; while disconnected it
/// is deregistered server-side, so held requests auto-deny on their own
/// timeout -- never silently allowed.
library;

import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

/// Decision + duration tokens mirror the server (``model/egress_consent.py``);
/// duplicated here per the client isolation boundary (#2309 rule).
const String kDecisionAllowed = 'allowed';
const String kDecisionDenied = 'denied';

/// Ordered for the duration selector; the default is `restart` (#2328).
const List<String> kConsentDurations = [
  'once',
  '5m',
  '15m',
  '1h',
  '1d',
  '1w',
  'restart',
  'forever',
];
const String kConsentDurationDefault = 'restart';

/// Inbound-frame application outcomes returned by [ConsentDeciderService.applyFrame].
enum ConsentFrameOutcome {
  /// A new held request (snapshot or live); [ConsentFrameResult.request] is set.
  added,

  /// A request resolved (decided/timed out); [ConsentFrameResult.resolvedId]
  /// is set.
  resolved,

  /// A liveness pong (no state change).
  pong,

  /// The server rejected a verdict; [ConsentFrameResult.message] is set.
  error,

  /// Non-JSON / unknown frame (ignored).
  ignored,
}

/// The payload of an applied frame (see [ConsentFrameOutcome] for which fields
/// are set per outcome).
class ConsentFrameResult {
  final ConsentFrameOutcome outcome;
  final PendingRequest? request;
  final String? resolvedId;
  final String? message;

  const ConsentFrameResult(this.outcome,
      {this.request, this.resolvedId, this.message});

  static const ignored = ConsentFrameResult(ConsentFrameOutcome.ignored);
}

/// One held egress request awaiting a verdict.
@immutable
class PendingRequest {
  final String id;
  final String destHost;
  final int? destPort;
  final String? processName;
  final double requestedAt;

  const PendingRequest({
    required this.id,
    required this.destHost,
    this.destPort,
    this.processName,
    required this.requestedAt,
  });

  /// Parse a frame's ``request`` object. Returns `null` on a shape that
  /// cannot be acted on (missing id / non-numeric port handled defensively).
  static PendingRequest? fromJson(Object? obj) {
    if (obj is! Map<String, dynamic>) return null;
    final id = obj['id'];
    if (id is! String) return null;
    final port = obj['dest_port'];
    final requestedAt = obj['requested_at'];
    return PendingRequest(
      id: id,
      destHost: obj['dest_host']?.toString() ?? '',
      destPort: port is int ? port : (port is num ? port.toInt() : null),
      processName: obj['process_name']?.toString(),
      requestedAt: requestedAt is num
          ? requestedAt.toDouble()
          : (requestedAt is int ? requestedAt.toDouble() : 0.0),
    );
  }

  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      other is PendingRequest &&
          id == other.id &&
          destHost == other.destHost &&
          destPort == other.destPort &&
          processName == other.processName &&
          requestedAt == other.requestedAt;

  @override
  int get hashCode =>
      Object.hash(id, destHost, destPort, processName, requestedAt);
}

/// Manages the WebSocket to ``/ws/consent-decider`` for one workspace.
///
/// The frame protocol (parse + verdict builder) lives in the pure static
/// [applyFrame] / [buildVerdict] so it is unit-testable without a transport;
/// this class is a thin owner of the socket + the pending-request map.
class ConsentDeciderService extends ChangeNotifier {
  ConsentDeciderService({
    required this.workspaceId,
    required this.token,
    this.holdTimeout = const Duration(seconds: 120),
    this.pingInterval = const Duration(seconds: 15),
    this.reconnectDelays = const [
      Duration(seconds: 1),
      Duration(seconds: 2),
      Duration(seconds: 5),
    ],
    this.clock = _wallClock,
  });

  final String workspaceId;
  String token;
  final Duration holdTimeout;
  final Duration pingInterval;
  final List<Duration> reconnectDelays;
  final DateTime Function() clock;

  WebSocketChannel? _channel;
  StreamSubscription? _sub;
  Timer? _pingTimer;
  Timer? _reconnectTimer;
  bool _connected = false;
  bool _stopped = false;
  int _attempt = 0;

  /// Whether the last disconnect was an auth failure (close codes 4001/4002).
  /// While true the service stops reconnecting -- the app surfaces re-login.
  bool _authFailed = false;
  bool get authFailed => _authFailed;

  /// Transient status flash (server `error` frame / verdict send failure /
  /// verdict attempted while disconnected), shown by the banner until
  /// [_flashUntil]. Mirrors the TUI's status-line flash (cli/tui/consent.py
  /// `_flash`) so the user is never left wondering whether a verdict landed.
  String? _flashMessage;
  DateTime? _flashUntil;

  /// The active flash message, or null once it has expired. Clock-based (not a
  /// real timer) so it is unit-testable by advancing the injected [clock]; the
  /// banner's 1s tick repaint re-reads this and the flash visually clears.
  String? get flashMessage {
    final msg = _flashMessage;
    final until = _flashUntil;
    if (msg == null || until == null) return null;
    return clock().isBefore(until) ? msg : null;
  }

  /// Surface a transient message to the user. Used for server error frames,
  /// verdict send failures, and verdicts attempted while disconnected --
  /// cases the TUI flashes to its status line rather than failing silently.
  void _flash(String message, {Duration ttl = const Duration(seconds: 5)}) {
    _flashMessage = message;
    _flashUntil = clock().add(ttl);
    notifyListeners();
  }

  final Map<String, PendingRequest> _pending = {};

  /// Pending requests, oldest-first (stable UI ordering by requested_at).
  List<PendingRequest> get pending => _pending.values.toList()
    ..sort((a, b) => a.requestedAt.compareTo(b.requestedAt));

  bool get connected => _connected;

  /// Override for testing to inject a fake channel factory.
  @visibleForTesting
  static WebSocketChannel Function(Uri uri)? testChannelFactory;

  /// The consent-decider WS URL for this workspace (token as a query param,
  /// mirroring [WsClient]).
  String get wsUrl {
    final loc = Uri.base;
    final wsScheme = loc.scheme == 'https' ? 'wss' : 'ws';
    final base =
        '$wsScheme://${loc.host}:${loc.port}$baseUrl/ws/consent-decider';
    return '$base?workspace=$workspaceId&token=$token';
  }

  /// Open the connection (idempotent: a no-op if already connected/connecting).
  void connect() {
    if (_channel != null || _stopped) return;
    final factory = testChannelFactory;
    WebSocketChannel ch;
    if (factory != null) {
      ch = factory(Uri.parse(wsUrl));
    } else {
      // coverage:ignore-start
      ch = WebSocketChannel.connect(Uri.parse(wsUrl));
      // coverage:ignore-end
    }
    _channel = ch;
    // The server's snapshot (sent immediately on connect) is authoritative
    // for currently-held requests, so rows that resolved while we were
    // disconnected -- and thus never sent us an `egress_resolved` -- must not
    // linger. The snapshot `egress_request` frames that follow repopulate.
    _pending.clear();
    _connected = true;
    _attempt = 0;
    _authFailed = false;
    _flashMessage = null;
    _flashUntil = null;
    _startPing();
    notifyListeners();
    _sub = ch.stream.listen(
      (raw) {
        if (raw is String) _onMessage(raw);
      },
      onError: (Object e) => debugPrint('[ConsentDecider] stream error: $e'),
      onDone: () => _onClosed(ch),
      cancelOnError: true,
    );
  }

  void _onMessage(String raw) {
    final res = applyFrame(_pending, raw);
    switch (res.outcome) {
      case ConsentFrameOutcome.added:
      case ConsentFrameOutcome.resolved:
        notifyListeners();
        break;
      case ConsentFrameOutcome.error:
        // Surface the rejection to the user (the TUI flashes it); a verdict
        // the server refused must not vanish silently.
        final m = res.message;
        _flash(m != null && m.isNotEmpty ? m : 'server error');
        break;
      case ConsentFrameOutcome.pong:
      case ConsentFrameOutcome.ignored:
        break;
    }
  }

  void _onClosed(WebSocketChannel ch) {
    if (!identical(ch, _channel))
      return; // a stale callback from a prior socket
    _stopPing();
    _connected = false;
    final code = ch.closeCode;
    if (code == 4001 || code == 4002) {
      _authFailed = true;
      notifyListeners();
      return; // stop reconnecting; the app surfaces re-login
    }
    notifyListeners();
    if (!_stopped) _scheduleReconnect();
  }

  void _scheduleReconnect() {
    if (_stopped || reconnectDelays.isEmpty) return;
    _attempt += 1;
    final delay =
        reconnectDelays[(_attempt - 1).clamp(0, reconnectDelays.length - 1)];
    _reconnectTimer?.cancel();
    // coverage:ignore-start
    _reconnectTimer = Timer(delay, () {
      _channel = null;
      _sub?.cancel();
      _sub = null;
      connect();
    });
    // coverage:ignore-end
  }

  void _startPing() {
    _stopPing();
    // coverage:ignore-start
    _pingTimer = Timer.periodic(pingInterval, (_) {
      final ch = _channel;
      if (ch == null) return;
      try {
        ch.sink.add(jsonEncode({'type': 'ping'}));
      } catch (e) {
        debugPrint('[ConsentDecider] ping failed: $e');
      }
    });
    // coverage:ignore-end
  }

  void _stopPing() {
    _pingTimer?.cancel();
    _pingTimer = null;
  }

  /// Send a verdict for a held request. No-op if disconnected (the caller's
  /// UI should reflect the disconnected state; the server auto-denies on the
  /// hold timeout, fail-closed).
  void sendVerdict(String requestId, String decision, String duration) {
    final ch = _channel;
    if (ch == null || !_connected) {
      // A verdict attempted while disconnected flashes (mirrors the TUI)
      // instead of failing silently; the server auto-denies on the hold
      // timeout -- fail-closed.
      _flash('disconnected — reconnecting');
      return;
    }
    try {
      ch.sink.add(buildVerdict(requestId, decision, duration));
    } catch (e) {
      debugPrint('[ConsentDecider] verdict send failed: $e');
      _flash('verdict send failed — reconnecting');
    }
  }

  @override
  void dispose() {
    _stopped = true;
    _stopPing();
    _reconnectTimer?.cancel();
    _reconnectTimer = null;
    _sub?.cancel();
    _sub = null;
    // coverage:ignore-start
    _channel?.sink.close(1000, 'dispose');
    // coverage:ignore-end
    _channel = null;
    _pending.clear();
    super.dispose();
  }

  // -- pure protocol logic (unit-testable without a transport) ----------------

  /// Parse + apply one inbound server frame to [pending] (mutating).
  ///
  /// Mirrors `cli/tui/consent.py`'s `ConsentDeciderController.apply_frame`:
  /// `egress_request` adds the request, `egress_resolved` removes it, `pong`
  /// and `error` are non-mutating signals, anything else is ignored. Malformed
  /// / non-JSON / unknown frames leave [pending] untouched.
  @visibleForTesting
  static ConsentFrameResult applyFrame(
      Map<String, PendingRequest> pending, String raw) {
    Map<String, dynamic> msg;
    try {
      final decoded = jsonDecode(raw);
      if (decoded is! Map<String, dynamic>) return ConsentFrameResult.ignored;
      msg = decoded;
    } catch (e) {
      return ConsentFrameResult.ignored;
    }
    final mtype = msg['type'];
    if (mtype == 'egress_request') {
      final req = PendingRequest.fromJson(msg['request']);
      if (req == null) return ConsentFrameResult.ignored;
      pending[req.id] = req;
      return ConsentFrameResult(ConsentFrameOutcome.added, request: req);
    }
    if (mtype == 'egress_resolved') {
      final rid = msg['request_id'];
      if (rid is String) pending.remove(rid);
      return ConsentFrameResult(ConsentFrameOutcome.resolved, resolvedId: rid);
    }
    if (mtype == 'pong') {
      return const ConsentFrameResult(ConsentFrameOutcome.pong);
    }
    if (mtype == 'error') {
      return ConsentFrameResult(ConsentFrameOutcome.error,
          message: msg['message']?.toString() ?? '');
    }
    return ConsentFrameResult.ignored;
  }

  /// Build an outbound verdict frame (JSON string) for a held request.
  @visibleForTesting
  static String buildVerdict(
      String requestId, String decision, String duration) {
    return jsonEncode({
      'type': 'verdict',
      'request_id': requestId,
      'decision': decision,
      'scope': 'once',
      'duration': duration,
    });
  }

  /// Seconds until this hold's countdown hits zero (clamped at 0). The server
  /// is the source of truth (it auto-denies at the real timeout); this is only
  /// a UX hint.
  int remainingSeconds(PendingRequest req) {
    final expire = req.requestedAt + holdTimeout.inSeconds;
    final now = clock().millisecondsSinceEpoch / 1000.0;
    final left = expire - now;
    return left < 0 ? 0 : left.round();
  }
}

DateTime _wallClock() => DateTime.now();
