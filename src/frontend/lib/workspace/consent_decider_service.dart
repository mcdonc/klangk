/// Interactive egress-consent decider service for the web UI (#2246).
///
/// Mirrors the standalone ``klangk consent-decide`` TUI
/// (``cli/tui/consent.py``): connects to the server's ``/ws/consent-decider``
/// stream for a workspace, holds the snapshot + live pending egress requests,
/// and sends ``verdict`` frames so the deciding user can allow/deny each held
/// connection *while the sidecar holds it* (#2311), plus ``pause``/``unpause``
/// frames that silence prompts workspace-wide for a window (#2332). The
/// verdict protocol + frame shapes are duplicated from the server
/// (``model/egress_consent.py``, pause handling in ``wshandler/decider.py`` +
/// ``consent_coordinator.py``) per the isolation boundary the web client
/// shares with the CLI.
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

import '../auth/dpop.dart';

/// Decision + duration tokens mirror the server (``model/egress_consent.py``);
/// duplicated here per the client isolation boundary (#2309 rule).
const String kDecisionAllowed = 'allowed';
const String kDecisionDenied = 'denied';

/// Offered (in order) by the banner's per-row duration menus; the default
/// is `tilrestart` (#2328).
/// The test-only `5s` is NOT offered (#2487) -- it's not meant for end users --
/// but stays recognized for in-effect/countdown math and programmatic/test
/// callers (it remains in `kConsentDurationSeconds` below).
const List<String> kConsentDurations = [
  'once',
  '5m',
  '15m',
  '1h',
  '1d',
  '1w',
  'tilrestart',
  'forever',
];
const String kConsentDurationDefault = 'tilrestart';

/// Seconds each *timed* duration adds to `decided_at` (mirror of the server's
/// `_DURATION_SECONDS`; duplicated per client isolation). `once` is consumed by
/// the single connection and `tilrestart`/`forever` have no fixed expiry, so
/// they are absent -- a rule with one of those (or null) has no countdown.
const Map<String, int> kConsentDurationSeconds = {
  '5s': 5,
  '5m': 300,
  '15m': 900,
  '1h': 3600,
  '1d': 86400,
  '1w': 604800,
};

/// Duration tokens that carry no fixed expiry (open-ended) -- the rules view
/// renders a label, not a countdown, for these. Mirror of the TUI.
const String kConsentDurationForever = 'forever';
const String kConsentDurationTilrestart = 'tilrestart';

/// Pause-window tokens the server accepts for silencing prompts (#2332).
/// A focused set (not the full verdict-duration list); mirrors the server's
/// `PAUSE_DURATIONS` (`consent_coordinator.py`) and the TUI pause bar's
/// buttons (`cli/tui/consent.py`).
const List<String> kConsentPauseDurations = ['15m', '1h', '1d'];

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

  /// A refreshed in-effect rules snapshot (#2387); [ConsentFrameResult.rules]
  /// is set.
  rules,

  /// The server replied to a revoke (#2339/#2341); [ConsentFrameResult.revokeAckId]
  /// and [ConsentFrameResult.revokeOk] are set.
  revokeAck,

  /// The server replied to a pause/unpause (#2332); [ConsentFrameResult.pauseOk]
  /// and [ConsentFrameResult.pauseUntil] are set.
  pauseAck,

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

  /// Parsed `egress_rules` snapshot (outcome == [ConsentFrameOutcome.rules]).
  final EgressRules? rules;

  /// The request id a `revoke_ack` refers to (outcome ==
  /// [ConsentFrameOutcome.revokeAck]); null on a malformed frame.
  final String? revokeAckId;

  /// Whether a `revoke_ack` confirmed success (outcome ==
  /// [ConsentFrameOutcome.revokeAck]).
  final bool revokeOk;

  /// Whether a `pause_ack` confirmed success (outcome ==
  /// [ConsentFrameOutcome.pauseAck]).
  final bool pauseOk;

  /// The pause-window end (epoch seconds) a successful `pause_ack` carries,
  /// or null (outcome == [ConsentFrameOutcome.pauseAck]; null `until` on an
  /// ok ack means the pause was cleared).
  final double? pauseUntil;

  const ConsentFrameResult(
    this.outcome, {
    this.request,
    this.resolvedId,
    this.message,
    this.rules,
    this.revokeAckId,
    this.revokeOk = false,
    this.pauseOk = false,
    this.pauseUntil,
  });

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

/// One in-effect consent verdict (allow or deny) for the rules view (#2387).
/// Mirrors the TUI `ConsentRule` (``cli/tui/consent.py``).
@immutable
class ConsentRule {
  final String id;
  final String destHost;
  final int? destPort;
  final String? processName;

  /// [kDecisionAllowed] or [kDecisionDenied].
  final String decision;
  final String? duration;

  /// Epoch seconds the verdict was decided (for timed countdowns); null when
  /// the server didn't send one.
  final double? decidedAt;
  final String? decidedBy;

  /// True when this rule records an allow verdict ([kDecisionAllowed]).
  bool get isAllowed => decision == kDecisionAllowed;

  const ConsentRule({
    required this.id,
    required this.destHost,
    this.destPort,
    this.processName,
    required this.decision,
    this.duration,
    this.decidedAt,
    this.decidedBy,
  });

  /// Parse one row of an `egress_rules` frame. Returns null on a non-map;
  /// missing fields degrade to empty/null rather than failing the whole row.
  static ConsentRule? fromJson(Object? obj) {
    if (obj is! Map<String, dynamic>) return null;
    final port = obj['dest_port'];
    final decidedAt = obj['decided_at'];
    final duration = obj['duration'];
    return ConsentRule(
      id: obj['id']?.toString() ?? '',
      destHost: obj['dest_host']?.toString() ?? '',
      destPort: port is int ? port : (port is num ? port.toInt() : null),
      processName: obj['process_name']?.toString(),
      decision: obj['decision']?.toString() ?? '',
      duration: duration is String ? duration : null,
      decidedAt: decidedAt is num ? decidedAt.toDouble() : null,
      decidedBy: obj['decided_by']?.toString(),
    );
  }

  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      other is ConsentRule &&
          id == other.id &&
          destHost == other.destHost &&
          destPort == other.destPort &&
          processName == other.processName &&
          decision == other.decision &&
          duration == other.duration &&
          decidedAt == other.decidedAt &&
          decidedBy == other.decidedBy;

  @override
  int get hashCode => Object.hash(
        id,
        destHost,
        destPort,
        processName,
        decision,
        duration,
        decidedAt,
        decidedBy,
      );
}

/// Pause window from the `egress_rules` frame (#2332; absent today). `until`
/// is the epoch second the pause ends, or null for an indefinite pause (e.g.
/// until restart). Mirrors the TUI `PauseState`.
@immutable
class EgressPause {
  final double? until;

  const EgressPause({this.until});

  @override
  bool operator ==(Object other) =>
      identical(this, other) || other is EgressPause && until == other.until;

  @override
  int get hashCode => until.hashCode;
}

/// Parsed `egress_rules` frame: the workspace's in-effect decisions (#2387).
/// `allowed`/`denied` are newest-decided-first (matching the backend's
/// `list_active` `ORDER BY decided_at DESC`); `allowList` is the static
/// `allowed_domains` config, order preserved; `paused` is null unless
/// filtering is actually paused (#2332). Mirrors the TUI `EgressRules`.
@immutable
class EgressRules {
  final String workspaceId;
  final List<String> allowList;

  /// The workspace's static reject list (`rejected_domains`, #2370/#2503):
  /// names the sidecar NXDOMAINs unconditionally, order preserved like
  /// [allowList]. Grows when a forever deny lands (#2369) and shrinks when
  /// that deny is revoked. Not consent rows -- workspace config, so it is
  /// read-only in the rules view.
  final List<String> rejectList;
  final List<ConsentRule> allowed;
  final List<ConsentRule> denied;
  final EgressPause? paused;

  const EgressRules({
    required this.workspaceId,
    required this.allowList,
    required this.allowed,
    required this.denied,
    this.rejectList = const [],
    this.paused,
  });

  /// Build from an `egress_rules` frame. Returns null only if the frame lacks
  /// a `workspace_id`; a missing/malformed `allow_list`/`allowed`/`denied`
  /// degrades to empty rather than dropping the frame, and rows that fail to
  /// parse are skipped. Mirrors the TUI `_parse_rules`.
  static EgressRules? fromJson(Map<String, dynamic> msg) {
    final wid = msg['workspace_id'];
    if (wid is! String) return null;
    final rawAllow = msg['allow_list'];
    final allowList = <String>[
      for (final d in (rawAllow is List ? rawAllow : <Object>[])) d.toString(),
    ];
    final rawReject = msg['reject_list'];
    final rejectList = <String>[
      for (final d in (rawReject is List ? rawReject : <Object>[]))
        d.toString(),
    ];
    final allowed = <ConsentRule>[
      for (final o
          in (msg['allowed'] is List ? msg['allowed'] as List : <Object>[]))
        if (ConsentRule.fromJson(o) case final r?) r,
    ];
    final denied = <ConsentRule>[
      for (final o
          in (msg['denied'] is List ? msg['denied'] as List : <Object>[]))
        if (ConsentRule.fromJson(o) case final r?) r,
    ];
    // Newest-decided-first (mirror backend ORDER BY decided_at DESC); rows
    // with no decided_at sort last. List.sort isn't stable, so use a stable
    // helper that breaks ties by original index (matches the TUI's sorted()).
    _sortRulesStable(allowed);
    _sortRulesStable(denied);
    return EgressRules(
      workspaceId: wid,
      allowList: allowList,
      rejectList: rejectList,
      allowed: allowed,
      denied: denied,
      paused: _parsePause(msg['paused']),
    );
  }

  @override
  bool operator ==(Object other) =>
      identical(this, other) ||
      other is EgressRules &&
          workspaceId == other.workspaceId &&
          listEquals(allowList, other.allowList) &&
          listEquals(rejectList, other.rejectList) &&
          listEquals(allowed, other.allowed) &&
          listEquals(denied, other.denied) &&
          paused == other.paused;

  @override
  int get hashCode => Object.hash(
        workspaceId,
        Object.hashAll(allowList),
        Object.hashAll(rejectList),
        Object.hashAll(allowed),
        Object.hashAll(denied),
        paused,
      );
}

/// Parse the `paused` field of an `egress_rules` frame (#2332). Returns null
/// unless filtering is actually paused, so the rules view renders no pause
/// section until then. Mirrors the TUI `_parse_pause`.
EgressPause? _parsePause(Object? obj) {
  if (obj is! Map<String, dynamic> || obj['paused'] != true) return null;
  final until = obj['until'];
  return EgressPause(until: until is num ? until.toDouble() : null);
}

/// Stable in-place ordering of consent rules: decided rows newest-first,
/// rows with no `decided_at` last, ties broken by original index (Dart's
/// [List.sort] isn't stable). Mirrors the TUI's `sorted(...)` over the frame.
void _sortRulesStable(List<ConsentRule> rules) {
  final indexed = [for (var i = 0; i < rules.length; i++) (i, rules[i])];
  indexed.sort((a, b) {
    final aT = a.$2.decidedAt;
    final bT = b.$2.decidedAt;
    if (aT == null && bT == null) return a.$1.compareTo(b.$1);
    if (aT == null) return 1;
    if (bT == null) return -1;
    final c = bT.compareTo(aT); // descending
    return c != 0 ? c : a.$1.compareTo(b.$1);
  });
  rules.setAll(0, [for (final e in indexed) e.$2]);
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
  bool _connecting = false;
  bool _stopped = false;
  int _attempt = 0;

  /// Whether the last disconnect was an auth failure (close codes
  /// 4001/4002/4004 — the last is the must-change-password gate, #3172).
  /// While true the service stops reconnecting -- the app surfaces re-login.
  bool _authFailed = false;
  bool get authFailed => _authFailed;

  /// Transient status flash (server `error` frame / verdict send failure /
  /// verdict attempted while disconnected), shown by the banner until
  /// [_flashUntil]. Mirrors the TUI's status-line flash (cli/tui/consent.py
  /// `_flash`) so the user is never left wondering whether a verdict landed.
  String? _flashMessage;
  DateTime? _flashUntil;

  /// Duration of the user's last pause request, or null after Unpause
  /// (#2494). Set when a pause/unpause is sent and **reverted on a failed
  /// ack**, so the highlighted button never claims a window the server
  /// refused. The server's frame carries only `until` (not which window),
  /// so the active button follows the user's last acknowledged request --
  /// mirrors the TUI `_pause_duration`, minus its stale-on-nack flaw. Owned
  /// by the service (not the panel) so it survives a panel remount.
  String? _lastPauseRequest;

  /// The pause/unpause op awaiting its ack: a duration token ('15m'...) or
  /// 'unpause'. Lets a nack flash which op failed; cleared on any ack.
  String? _pendingPauseOp;

  /// The button-highlight source for the pause controls -- the user's last
  /// acknowledged pause request (null = Unpause is active). See
  /// [_lastPauseRequest].
  String? get lastPauseRequest => _lastPauseRequest;

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

  /// Latest in-effect rules snapshot (#2387), or null until the first
  /// `egress_rules` frame lands (on connect). Mirrors the TUI controller's
  /// `rules`; the rules tab renders an empty state until then.
  EgressRules? _rules;

  /// Pending requests, oldest-first (stable UI ordering by requested_at).
  List<PendingRequest> get pending => _pending.values.toList()
    ..sort((a, b) => a.requestedAt.compareTo(b.requestedAt));

  /// The latest in-effect rules snapshot, or null before the first frame.
  EgressRules? get rules => _rules;

  bool get connected => _connected;

  /// Override for testing to inject a fake channel factory.
  @visibleForTesting
  static WebSocketChannel Function(Uri uri)? testChannelFactory;

  /// The consent-decider WS URL for this workspace: token as a query
  /// param (mirroring [WsClient]) plus a one-shot DPoP proof parameter
  /// when the token is bound (#3218).
  Future<String> wsUrl() async {
    final loc = Uri.base;
    final wsScheme = loc.scheme == 'https' ? 'wss' : 'ws';
    final base =
        '$wsScheme://${loc.host}:${loc.port}$baseUrl/ws/consent-decider';
    final headers = await dpopHeadersFor('GET', base, token);
    final proof = headers['DPoP'];
    final suffix = proof == null ? '' : '&dpop=$proof';
    return '$base?workspace=$workspaceId&token=$token$suffix';
  }

  /// Open the connection (idempotent: a no-op if already
  /// connected/connecting). Async since #3218 (proof minting), so an
  /// in-flight connect is tracked — two overlapping calls must not open
  /// two channels — and failures are contained: fire-and-forget callers
  /// (the workspace page's construction cascade) must never see an
  /// unhandled async error; a failed connect just schedules the normal
  /// reconnect backoff.
  Future<void> connect() async {
    if (_channel != null || _stopped || _connecting) return;
    _connecting = true;
    try {
      await _openChannel();
    } catch (e) {
      debugPrint('[ConsentDecider] connect failed: $e');
      _channel = null;
      _scheduleReconnect();
    } finally {
      _connecting = false;
    }
  }

  Future<void> _openChannel() async {
    final url = await wsUrl();
    final factory = testChannelFactory;
    WebSocketChannel ch;
    if (factory != null) {
      ch = factory(Uri.parse(url));
    } else {
      // coverage:ignore-start
      ch = WebSocketChannel.connect(Uri.parse(url));
      // coverage:ignore-end
    }
    _channel = ch;
    // The server's snapshot (sent immediately on connect) is authoritative
    // for currently-held requests, so rows that resolved while we were
    // disconnected -- and thus never sent us an `egress_resolved` -- must not
    // linger. The snapshot `egress_request` frames that follow repopulate.
    _pending.clear();
    // The server re-sends the `egress_rules` snapshot on (re)connect, so a
    // stale snapshot from a prior session must not linger -- mirrors the
    // TUI controller's `reset()`.
    _rules = null;
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
      // coverage:ignore-start
      onError: (Object e) => debugPrint('[ConsentDecider] stream error: $e'),
      // coverage:ignore-end
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
      case ConsentFrameOutcome.rules:
        _rules = res.rules;
        notifyListeners();
        break;
      case ConsentFrameOutcome.revokeAck:
        _applyRevokeAck(res.revokeAckId, res.revokeOk);
        break;
      case ConsentFrameOutcome.pauseAck:
        _applyPauseAck(res.pauseOk, res.pauseUntil);
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

  /// Apply a `revoke_ack`: on success drop the rule from the cached snapshot
  /// (idempotent -- the server also pushes a refreshed `egress_rules`); on
  /// failure leave it enforced and flash (a still-enforced rule must never be
  /// hidden silently). Mirrors the TUI controller's `revoke_ack` handling.
  void _applyRevokeAck(String? id, bool ok) {
    if (!ok) {
      _flash('revoke failed — still in effect');
      return;
    }
    final r = _rules;
    if (id != null && r != null) {
      _rules = EgressRules(
        workspaceId: r.workspaceId,
        allowList: r.allowList,
        rejectList: r.rejectList,
        allowed: r.allowed.where((e) => e.id != id).toList(),
        denied: r.denied.where((e) => e.id != id).toList(),
        paused: r.paused,
      );
    }
    notifyListeners();
  }

  /// Apply a `pause_ack` (#2494 review): a nack reverts the highlight (the
  /// server refused -- unknown duration, for instance -- so no window was
  /// set) and flashes which op failed; a success applies the ack's own
  /// pause state as an authoritative fallback -- the refreshed
  /// `egress_rules` broadcast normally lands first (the server awaits it
  /// before acking), but the broadcast is best-effort server-side, so
  /// without this the display could sit stale after a successful pause.
  /// Null `until` on an ok ack means the pause was cleared (unpause).
  void _applyPauseAck(bool ok, double? until) {
    final op = _pendingPauseOp;
    _pendingPauseOp = null;
    if (!ok) {
      _lastPauseRequest = null;
      _flash(op == 'unpause' ? 'unpause failed' : 'pause failed');
      return;
    }
    final r = _rules;
    if (r != null) {
      _rules = EgressRules(
        workspaceId: r.workspaceId,
        allowList: r.allowList,
        rejectList: r.rejectList,
        allowed: r.allowed,
        denied: r.denied,
        paused: until != null ? EgressPause(until: until) : null,
      );
    }
    notifyListeners();
  }

  void _onClosed(WebSocketChannel ch) {
    if (!identical(ch, _channel))
      return; // a stale callback from a prior socket
    _stopPing();
    _connected = false;
    final code = ch.closeCode;
    if (code == 4001 || code == 4002 || code == 4004) {
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

  /// Revoke an active verdict (#2341). The row stays in the cached snapshot
  /// until the server's `revoke_ack` confirms success -- never removed
  /// optimistically, so a still-enforced rule is never hidden (mirrors the
  /// TUI). Flashes (not silent) when disconnected or the send fails.
  void sendRevoke(String requestId) {
    final ch = _channel;
    if (ch == null || !_connected) {
      _flash('disconnected — reconnecting');
      return;
    }
    try {
      ch.sink.add(buildRevoke(requestId));
    } catch (e) {
      debugPrint('[ConsentDecider] revoke send failed: $e');
      _flash('revoke send failed — reconnecting');
    }
  }

  /// Pause interactive consent prompting for the workspace for `duration`
  /// (#2332; one of [kConsentPauseDurations]). While paused, a destination
  /// with no allow-list rule and no recorded deny is auto-allowed instead of
  /// prompting. Never optimistic: the UI follows the server's `pause_ack` +
  /// the refreshed `egress_rules` frame. Flashes (not silent) when
  /// disconnected or the send fails. Mirrors the TUI.
  void sendPause(String duration) {
    _lastPauseRequest = duration;
    _pendingPauseOp = duration;
    notifyListeners();
    final ch = _channel;
    if (ch == null || !_connected) {
      _flash('disconnected — reconnecting');
      return;
    }
    try {
      ch.sink.add(buildPause(duration));
    } catch (e) {
      debugPrint('[ConsentDecider] pause send failed: $e');
      _flash('pause send failed — reconnecting');
    }
  }

  /// Resume prompting (clear an active pause) (#2332). Mirrors the TUI.
  void sendUnpause() {
    _lastPauseRequest = null;
    _pendingPauseOp = 'unpause';
    notifyListeners();
    final ch = _channel;
    if (ch == null || !_connected) {
      _flash('disconnected — reconnecting');
      return;
    }
    try {
      ch.sink.add(buildUnpause());
    } catch (e) {
      debugPrint('[ConsentDecider] unpause send failed: $e');
      _flash('unpause send failed — reconnecting');
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
    Map<String, PendingRequest> pending,
    String raw,
  ) {
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
      return ConsentFrameResult(
        ConsentFrameOutcome.error,
        message: msg['message']?.toString() ?? '',
      );
    }
    if (mtype == 'egress_rules') {
      final rules = EgressRules.fromJson(msg);
      if (rules == null) return ConsentFrameResult.ignored;
      return ConsentFrameResult(ConsentFrameOutcome.rules, rules: rules);
    }
    if (mtype == 'revoke_ack') {
      final rid = msg['request_id'];
      return ConsentFrameResult(
        ConsentFrameOutcome.revokeAck,
        revokeAckId: rid is String ? rid : null,
        revokeOk: msg['ok'] == true,
      );
    }
    if (mtype == 'pause_ack') {
      final until = msg['until'];
      return ConsentFrameResult(
        ConsentFrameOutcome.pauseAck,
        pauseOk: msg['ok'] == true,
        pauseUntil: until is num ? until.toDouble() : null,
      );
    }
    return ConsentFrameResult.ignored;
  }

  /// Build an outbound verdict frame (JSON string) for a held request.
  @visibleForTesting
  static String buildVerdict(
    String requestId,
    String decision,
    String duration,
  ) {
    return jsonEncode({
      'type': 'verdict',
      'request_id': requestId,
      'decision': decision,
      'scope': 'once',
      'duration': duration,
    });
  }

  /// Build an outbound revoke frame (JSON string) for an active verdict
  /// (#2341). Asks the server (#2339) to drop the verdict's sidecar rule and
  /// mark the row revoked.
  @visibleForTesting
  static String buildRevoke(String requestId) {
    return jsonEncode({'type': 'revoke', 'request_id': requestId});
  }

  /// Build an outbound pause frame (JSON string) silencing prompts for a
  /// window (#2332). `duration` is one of [kConsentPauseDurations]; the
  /// server replies `pause_ack` and re-broadcasts `egress_rules` with the
  /// live `paused` window. Mirrors the TUI `make_pause`.
  @visibleForTesting
  static String buildPause(String duration) {
    return jsonEncode({'type': 'pause', 'duration': duration});
  }

  /// Build an outbound unpause frame (JSON string) resuming prompting
  /// (#2332). Mirrors the TUI `make_unpause`.
  @visibleForTesting
  static String buildUnpause() {
    return jsonEncode({'type': 'unpause'});
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

  /// Seconds left on a timed verdict, or null if it has no fixed expiry
  /// (`forever`/`tilrestart`/`once`/unknown/missing `decided_at`). The rules
  /// view shows a label, not a countdown, for the open-ended ones. Mirrors
  /// the TUI `rule_remaining`.
  int? ruleRemainingSeconds(ConsentRule rule) {
    final decidedAt = rule.decidedAt;
    if (decidedAt == null) return null;
    final secs = kConsentDurationSeconds[rule.duration];
    if (secs == null) return null;
    final now = clock().millisecondsSinceEpoch / 1000.0;
    final left = decidedAt + secs - now;
    return left < 0 ? 0 : left.round();
  }

  /// Seconds left in the pause window, or null if not paused / indefinite.
  /// Mirrors the TUI `pause_remaining`; used by the rules view's pause label.
  int? pauseRemainingSeconds(EgressRules rules) {
    final until = rules.paused?.until;
    if (until == null) return null;
    final now = clock().millisecondsSinceEpoch / 1000.0;
    final left = until - now;
    return left < 0 ? 0 : left.round();
  }

  /// Whether a timed verdict's window has elapsed (its countdown reached zero)
  /// and the rule should be hidden from the rules view. The server drops it
  /// from `list_active` at the same instant, but only re-broadcasts
  /// `egress_rules` on the next discrete event (verdict/revoke/pause/
  /// reconnect) -- not on natural expiry -- so the client prunes it locally
  /// ([pruneExpiredRules]) to hide the row the moment it expires rather than
  /// freezing at "0s left". Open-ended verdicts (`forever`/`tilrestart`) and
  /// unknown/missing-`decided_at` rows never expire (they have no countdown).
  bool isRuleExpired(ConsentRule rule) {
    final remaining = ruleRemainingSeconds(rule);
    return remaining != null && remaining <= 0;
  }

  /// Whether the pause window has elapsed -- a finite `until` in the past
  /// (#2494 review). The server reverts to prompting at the real expiry but,
  /// like a rule, only re-broadcasts `egress_rules` on the next discrete
  /// event -- so without a local prune the panel would show "Filtering
  /// paused (resumes in 0s)" forever, claiming prompts are suppressed when
  /// holds have actually resumed. An indefinite pause (`until` null) and a
  /// not-paused workspace never expire. Mirrors [isRuleExpired] (#2467).
  bool isPauseExpired(EgressRules rules) {
    final until = rules.paused?.until;
    if (until == null) return false;
    final now = clock().millisecondsSinceEpoch / 1000.0;
    return until <= now;
  }

  /// Drop timed verdicts whose window has elapsed -- and a self-expired
  /// pause -- from the cached snapshot, notifying listeners if anything
  /// changed. Called by the rules view's 1s tick so an expired rule (or
  /// pause) disappears at the first tick past its expiry instead of
  /// lingering at "0s left". Returns whether anything was pruned. No-op
  /// (returns false) before the first `egress_rules` frame lands.
  bool pruneExpiredRules() {
    final r = _rules;
    if (r == null) return false;
    final allowed = r.allowed.where((e) => !isRuleExpired(e)).toList();
    final denied = r.denied.where((e) => !isRuleExpired(e)).toList();
    final paused = isPauseExpired(r) ? null : r.paused;
    if (allowed.length == r.allowed.length &&
        denied.length == r.denied.length &&
        paused == r.paused) {
      return false;
    }
    _rules = EgressRules(
      workspaceId: r.workspaceId,
      allowList: r.allowList,
      rejectList: r.rejectList,
      allowed: allowed,
      denied: denied,
      paused: paused,
    );
    notifyListeners();
    return true;
  }
}

DateTime _wallClock() => DateTime.now();
