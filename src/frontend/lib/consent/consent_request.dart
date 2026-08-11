/// One held egress request awaiting an allow/deny verdict.
///
/// Mirrors the TUI decider client's `ConsentRequest`
/// (`src/klangk/klangk/cli/tui/consent.py`) so both deciders speak the same
/// server frame protocol (`/ws/consent-decider`, #2244). Flutter-side pure
/// model; parsing lives in [parseConsentRequest] so the client stays focused
/// on transport.
class ConsentRequest {
  ConsentRequest({
    required this.id,
    required this.workspaceId,
    required this.destHost,
    required this.destPort,
    required this.processName,
    required this.pid,
    required this.requestedAt,
  });

  /// Server-assigned request id (used as the `verdict.request_id`).
  final String id;

  /// Workspace the held connection originated in.
  final String workspaceId;

  /// Destination host the workspace process tried to reach (server-observed
  /// DNS). Rendered literally — never styled from it.
  final String destHost;

  /// Destination port, or null when the server didn't record one.
  final int? destPort;

  /// Originating process name (e.g. "curl"), or null.
  final String? processName;

  /// Originating PID inside the workspace, or null.
  final int? pid;

  /// Epoch seconds (wall-clock) the server stamped the hold. Shares the
  /// `time.time()` domain the server uses, so the auto-deny countdown math
  /// (`requestedAt + holdTimeout - now`) is meaningful.
  final double requestedAt;
}

/// Build a [ConsentRequest] from an inbound frame's `request` object.
///
/// Returns `null` on a shape that can't be acted on (missing id/workspace),
/// mirroring the TUI's `_parse_request`. Defensive about types: the frame
/// comes off the wire untyped.
ConsentRequest? parseConsentRequest(Object? obj) {
  if (obj is! Map<String, dynamic>) return null;
  final id = obj['id'];
  final wid = obj['workspace_id'];
  if (id is! String || wid is! String) return null;
  final requestedAtRaw = obj['requested_at'];
  final requestedAt = requestedAtRaw is num ? requestedAtRaw.toDouble() : 0.0;
  final port = obj['dest_port'];
  final pid = obj['pid'];
  return ConsentRequest(
    id: id,
    workspaceId: wid,
    destHost: (obj['dest_host'] ?? '').toString(),
    destPort: port is num ? port.toInt() : null,
    processName: obj['process_name']?.toString(),
    // int and bool are disjoint types in Dart, so `pid is int` already
    // excludes a JSON true/false (no Python-style bool-is-int gotcha here).
    pid: pid is int ? pid : null,
    requestedAt: requestedAt,
  );
}
