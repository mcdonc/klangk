import 'dart:async';
import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'package:soliplex_agent/soliplex_agent.dart' show ThreadKey;
import 'package:soliplex_client/soliplex_client.dart' as sox;

import 'soliplex_servers.dart';
import 'soliplex_status_page.dart';
import 'soliplex_tools.dart';

const soliplexFeatureVersion = '2026-09-30-keepalive';

/// Default period between keepalive chunks emitted during a bridged
/// streaming call (#3485). Well under the 30s default bridge idle timeout, so
/// a silent-but-active phase (RAG retrieval, LLM first-token latency) keeps
/// resetting the idle timer.
const defaultKeepaliveInterval = Duration(seconds: 20);

/// Default per-room deadline for one RAG + LLM query (#3485). A room that
/// misses it returns an error (per-room in a fan-out) instead of hanging the
/// call — this is the primary bound that keeps every call finite. The
/// deadline bounds only the caller's wait: the server-side RAG + LLM run
/// keeps running to completion on its own, because the client has no way to
/// cancel it.
const defaultQueryDeadline = Duration(minutes: 3);

/// Default total-duration cap on keepalive emission (#3485). After this much
/// call time the ticker goes silent on purpose, so the bridge idle timeout
/// closes the stream — the backstop for a hang the per-room deadline somehow
/// misses (room parsing, `"*"` expansion, session setup, a deadline that
/// fails to fire). The ticker spans the whole call, so every one of those
/// phases is inside the cap.
const defaultKeepaliveCeiling = Duration(minutes: 10);

/// Outcome of querying one room in a multiroom fan-out: either the answer
/// (with its Sources block + thread id, for continuation) or a captured
/// per-room error message. A failed room carries [error] != null and never
/// aborts the batch.
class FanOutResult {
  const FanOutResult({
    required this.server,
    required this.room,
    this.answer,
    this.threadId,
    this.error,
  });
  final String server;
  final String room;
  final String? answer;
  final String? threadId;
  final String? error;
}

/// Periodic empty-chunk keepalive for ONE bridged streaming tool call
/// (#3485).
///
/// The klangk browser-delegate bridge bounds the gap BETWEEN chunks (the
/// per-chunk idle timeout), not total duration — so a call is safe exactly as
/// long as something keeps crossing the bridge. [start] emits an immediate
/// empty chunk (the initial keepalive covering the silent warm-up before the
/// first AG-UI event: session fetch, thread creation, model warm-up), then
/// re-emits every [interval] so a silent-but-active phase (RAG retrieval,
/// first-token latency) cannot outlive the idle timeout — including a
/// multiroom fan-out where every room outlives it. After [ceiling] of total
/// call time the ticker goes silent ON PURPOSE: the bridge idle timeout then
/// closes the stream, so a hung call terminates instead of running forever.
/// Both bounds are what keep "never times out" from becoming literal.
class KeepaliveTicker {
  KeepaliveTicker(this._sink, {required this.interval, required this.ceiling});

  final ToolChunkSink? _sink;
  final Duration interval;
  final Duration ceiling;
  final Stopwatch _elapsed = Stopwatch();
  Timer? _timer;

  /// Whether the periodic timer is armed.
  bool get running => _timer != null;

  /// Emit the initial keepalive and arm the periodic timer. No-op without a
  /// sink (non-streaming call) or when already running.
  void start() {
    if (_sink == null || _timer != null) return;
    _elapsed.start();
    _sink('');
    _timer = Timer.periodic(interval, (_) => _tick());
  }

  void _tick() {
    if (_elapsed.elapsed >= ceiling) {
      // Total-duration cap: fall silent so the bridge idle timeout closes
      // the stream (see the class doc). stop() also disarms the timer.
      stop();
      return;
    }
    _sink?.call('');
  }

  /// Cancel the timer. Safe to call more than once; every start() owner must
  /// call this in a finally so a completed call never leaks a running timer.
  void stop() {
    _timer?.cancel();
    _timer = null;
    _elapsed.stop();
  }
}

/// Render ONE fan-out result as its labeled `## server/room` block. PURE (no
/// I/O). Used twice: by [formatFanOut] for the final aggregate, and by the
/// fan-out itself to stream each room's block as a chunk the moment that room
/// finishes (#3485) — so the same text the agent sees mid-call is what the
/// final aggregated result contains.
String formatFanOutBlock(FanOutResult r) {
  final header = '## ${r.server}/${r.room}';
  if (r.error != null) return '$header\nError: ${r.error}';
  final tid = r.threadId == null
      ? ''
      : '\n[soliplex server: ${r.server}, room_id: ${r.room}, '
          'thread_id: ${r.threadId} — continue with '
          'soliplex_reply(server, room_id, thread_id, message)]';
  return '$header\n${r.answer ?? ''}$tid';
}

/// Render multiroom fan-out [results] as one aggregated, per-room-labeled
/// block. PURE (no I/O) so the aggregation shape is unit-testable
/// independently of the live SSE in `_streamRun`. Each successful room keeps
/// its own answer (which already carries its "Sources" list) and prints its
/// `thread_id` so the agent can continue THAT conversation via
/// soliplex_reply(server, room_id, thread_id, message). Failed rooms render
/// `Error: <message>` instead, so a partial failure is visible inline rather
/// than collapsing the whole result.
String formatFanOut(String question, List<FanOutResult> results) {
  final blocks = results.map(formatFanOutBlock).join('\n\n');
  return 'Asked ${results.length} room(s): "$question"\n\n$blocks';
}

/// Render one room's info block (pi `soliplex_get_room_info`). PURE (no I/O) so
/// the shape is unit-testable. Leads with name/description/welcome, then the
/// **suggested prompts** — the "what can I ask here?" hints the agent surfaces.
String formatRoomInfo(String server, String roomId, Map<String, dynamic> room) {
  final name = (room['name'] as String?)?.trim();
  final desc = (room['description'] as String?)?.trim();
  final welcome =
      ((room['welcome_message'] ?? room['welcomeMessage']) as String?)?.trim();
  final rawSuggestions = room['suggestions'];
  final suggestions = rawSuggestions is List
      ? rawSuggestions.whereType<String>().toList()
      : const <String>[];

  final lines = <String>['Room "$roomId" on "$server":'];
  lines.add('- name: ${(name == null || name.isEmpty) ? roomId : name}');
  if (desc != null && desc.isNotEmpty) lines.add('- description: $desc');
  if (welcome != null && welcome.isNotEmpty) lines.add('- welcome: $welcome');

  final flags = <String>[];
  if (room['enable_attachments'] == true || room['enableAttachments'] == true) {
    flags.add('attachments');
  }
  if (room['allow_mcp'] == true || room['allowMcp'] == true) flags.add('mcp');
  if (flags.isNotEmpty) lines.add('- enabled: ${flags.join(', ')}');

  if (suggestions.isEmpty) {
    lines.add('- suggestions: (none)');
  } else {
    lines.add('- suggestions:');
    lines.addAll(suggestions.map((s) => '  - $s'));
  }
  return lines.join('\n');
}

/// Knowledge-base feature: bridges the agent's `soliplex_list_rooms` /
/// `soliplex_query` tools to the user's Soliplex server, with an auth overlay.
///
/// Platform-agnostic: all browser-only concerns (token storage, interactive
/// login) live behind soliplex_platform.dart, so this compiles for native and
/// web. No `dart:js_interop` / `package:web` imports here (Phase 4 guardrail).
class SoliplexFeature extends ToolPlugin with ChangeNotifier {
  /// Registry of reachable Soliplex servers. Defaults to the process-wide
  /// [soliplexServers]; tests inject one backed by a mock `http.Client`.
  final SoliplexServerRegistry registry;

  /// Keepalive timing for bridged streaming calls (#3485). See the
  /// `defaultKeepalive*` / `defaultQueryDeadline` constants for the semantics;
  /// tests inject small values so timer behavior runs in milliseconds.
  final Duration keepaliveInterval;
  final Duration queryDeadline;
  final Duration keepaliveCeiling;

  bool _authenticated = false;
  bool _loggingIn = false;
  String? _loginError;
  bool _overlayExpanded = false;

  /// In-memory conversation history per [ThreadKey] (serverId, roomId,
  /// threadId), so multi-turn [soliplex_reply] turns carry context (the AG-UI
  /// run input must include prior turns; the backend does not replay them).
  /// Keyed by the full tuple because thread ids are only unique within one
  /// room on one server. Session-scoped — cleared on reload; the thread itself
  /// persists server-side.
  final Map<ThreadKey, List<sox.Message>> _threadHistory = {};

  int _msgSeq = 0;
  String _mid(String p) =>
      '$p-${DateTime.now().millisecondsSinceEpoch}-${_msgSeq++}';

  SoliplexFeature({
    SoliplexServerRegistry? registry,
    this.keepaliveInterval = defaultKeepaliveInterval,
    this.queryDeadline = defaultQueryDeadline,
    this.keepaliveCeiling = defaultKeepaliveCeiling,
  }) : registry = registry ?? soliplexServers {
    _refreshAuthState();
  }

  /// Resolve the `server` tool argument to a server name, defaulting to the
  /// config-derived `default` server when absent/blank.
  String _serverArg(Map<String, dynamic> request) {
    final raw = (request['server'] as String?)?.trim();
    return (raw == null || raw.isEmpty)
        ? SoliplexServerRegistry.defaultName
        : raw;
  }

  String? get loginError => _loginError;

  Future<void> _refreshAuthState() async {
    // The collapsed icon reflects whether ANY configured server is connected.
    // Network/config failures must not throw out of the constructor's
    // fire-and-forget call.
    bool any = false;
    try {
      await registry.ensureDefault();
      for (final s in registry.servers) {
        if (await (await registry.session(s.name)).isConnected()) {
          any = true;
          break;
        }
      }
    } catch (_) {
      any = false;
    }
    if (any != _authenticated) {
      _authenticated = any;
      notifyListeners();
    }
  }

  /// Disconnect every server OTHER than [keepServer]. Enforces the
  /// single-server invariant: only one server may be connected at a time.
  Future<void> _disconnectOtherServers(String keepServer) async {
    try {
      await registry.ensureDefault();
      for (final s in registry.servers) {
        if (s.name == keepServer) continue;
        final session = await registry.session(s.name);
        if (await session.isConnected()) {
          await session.clearStoredTokens();
        }
      }
    } catch (_) {
      // Best-effort: if we can't check/disconnect others, proceed with the
      // login attempt anyway.
    }
  }

  /// Configured servers (ensures `default` is loaded first). For the overlay.
  Future<List<SoliplexServer>> listServers() async {
    await registry.ensureDefault();
    return registry.servers;
  }

  /// Whether [server] currently holds a valid token. For the overlay's
  /// per-server status.
  Future<bool> isServerConnected(String server) async {
    try {
      return (await registry.session(server)).isConnected();
    } catch (_) {
      return false;
    }
  }

  /// Mark an open (no-auth) [server] as connected. It needs no login but is
  /// usable immediately, so the overlay treats it like a logged-in server
  /// (green icon/dot). Best-effort: the server still works without the flag.
  /// Enforces single-server: disconnects any other connected server first.
  Future<void> markServerOpenConnected(String server) async {
    await _disconnectOtherServers(server);
    try {
      await (await registry.session(server)).markOpenConnected();
    } catch (_) {
      // Persisting the marker failed; the server is still usable.
    }
    await _refreshAuthState();
  }

  /// Add a server from the overlay UI (mirrors the pi `soliplex_add_server`
  /// path). Returns null on success or an error message.
  Future<String?> addServerFromUi(String name, String url) async {
    final n = name.trim();
    final u = url.trim();
    if (n.isEmpty) return 'Name is required';
    if (u.isEmpty) return 'URL is required';
    if (n == SoliplexServerRegistry.defaultName) {
      return '"${SoliplexServerRegistry.defaultName}" is reserved';
    }
    try {
      await registry.addServer(n, u);
      notifyListeners();
      return null;
    } catch (e) {
      return '$e';
    }
  }

  /// Remove a user-added server from the overlay UI. Returns null on success
  /// or an error message. The bundled `default` server cannot be removed.
  Future<String?> removeServerFromUi(String name) async {
    final n = name.trim();
    if (n.isEmpty) return 'Name is required';
    if (n == SoliplexServerRegistry.defaultName) {
      return '"${SoliplexServerRegistry.defaultName}" cannot be removed';
    }
    try {
      await registry.ensureDefault();
      if (!registry.names.contains(n)) return 'Server "$n" not found';
      await logout(server: n);
      await registry.removeServer(n);
      notifyListeners();
      return null;
    } catch (e) {
      return '$e';
    }
  }

  bool get authenticated => _authenticated;
  bool get loggingIn => _loggingIn;
  bool get overlayExpanded => _overlayExpanded;

  void toggleOverlay() {
    _overlayExpanded = !_overlayExpanded;
    notifyListeners();
  }

  /// OIDC auth systems available on [server] (for the connect overlay).
  Future<Map<String, dynamic>> getAuthSystems({
    String server = SoliplexServerRegistry.defaultName,
  }) async =>
      (await registry.session(server)).getAuthSystems();

  @override
  Map<String, ToolHandler> get handlers => {
        'soliplex_list_rooms': _listRooms,
        'soliplex_get_room_info': _getRoomInfo,
        'soliplex_list_threads': _listThreads,
        'soliplex_query': _query,
        'soliplex_reply': _reply,
        'soliplex_list_servers': _listServers,
        'soliplex_add_server': _addServer,
        'soliplex_remove_server': _removeServer,
        'soliplex_list_files': _listFiles,
        'soliplex_get_file': _getFile,
        'soliplex_upload_file': _uploadFile,
      };

  @override
  Map<String, StreamingToolHandler> get streamingHandlers => {
        'soliplex_query': _queryStream,
        'soliplex_reply': _replyStream,
      };

  late final _appBarAction = _SoliplexAppBarIcon(
    key: const ValueKey('soliplex_app_bar_icon'),
    feature: this,
  );

  late final _overlay = _SoliplexAuthOverlay(
    key: const ValueKey('soliplex_auth_overlay'),
    feature: this,
  );

  @override
  Widget? buildAppBarAction(BuildContext context) => _appBarAction;

  @override
  Widget? buildOverlay(BuildContext context) => _overlay;

  @override
  List<PluginRoute> get routes => [
        PluginRoute(
          path: '/soliplex-status',
          builder: (context, pathParams, queryParams) =>
              SoliplexStatusPage(registry: registry),
        ),
      ];

  Future<void> login(
    String systemId, {
    String server = SoliplexServerRegistry.defaultName,
  }) async {
    _loggingIn = true;
    _loginError = null;
    notifyListeners();
    try {
      // Enforce single-server: disconnect any other connected server first.
      await _disconnectOtherServers(server);
      await (await registry.session(server)).login(systemId);
    } catch (e) {
      _loginError = e.toString();
    } finally {
      _loggingIn = false;
      notifyListeners();
      await _refreshAuthState(); // recompute "any server connected"
    }
  }

  /// Clear stored tokens for [server]. The collapsed icon dims only when no
  /// server remains connected.
  Future<void> logout({
    String server = SoliplexServerRegistry.defaultName,
  }) async {
    await (await registry.session(server)).clearStoredTokens();
    _loginError = null;
    notifyListeners();
    await _refreshAuthState();
  }

  /// List the configured servers (names usable as the `server` arg). Reachable
  /// from the pi `soliplex_list_servers` tool.
  Future<String> _listServers(Map<String, dynamic> request) async {
    try {
      await registry.ensureDefault();
      final lines = registry.servers
          .map(
            (s) => '- ${s.name}: '
                '${s.baseUrl.isEmpty ? '(not configured)' : s.baseUrl}',
          )
          .join('\n');
      return 'Configured soliplex servers:\n$lines';
    } catch (e) {
      return 'Error listing soliplex servers: $e';
    }
  }

  /// Register an additional server (pi `soliplex_add_server` tool). Auth is
  /// per-server and interactive, so the user still connects via the overlay;
  /// no-auth servers work immediately.
  Future<String> _addServer(Map<String, dynamic> request) async {
    final name = (request['name'] as String?)?.trim() ?? '';
    final url = (request['url'] as String?)?.trim() ?? '';
    if (name.isEmpty) return 'Error: name is required';
    if (url.isEmpty) return 'Error: url is required';
    if (name == SoliplexServerRegistry.defaultName) {
      return 'Error: "${SoliplexServerRegistry.defaultName}" is reserved; '
          'choose another name';
    }
    try {
      await registry.addServer(name, url);
      notifyListeners(); // overlay may show the new server in its selector
      return 'Added soliplex server "$name" ($url). If it requires auth, '
          'connect via the "Connect to Soliplex" overlay; then pass '
          'server: "$name" to soliplex_query / soliplex_list_rooms / soliplex_reply.';
    } catch (e) {
      return 'Error adding soliplex server "$name": $e';
    }
  }

  /// Remove a user/agent-added server (pi `soliplex_remove_server` tool). The
  /// bundled `default` server is protected and cannot be removed.
  Future<String> _removeServer(Map<String, dynamic> request) async {
    final name = (request['name'] as String?)?.trim() ?? '';
    if (name.isEmpty) return 'Error: name is required';
    if (name == SoliplexServerRegistry.defaultName) {
      return 'Error: "${SoliplexServerRegistry.defaultName}" is reserved and '
          'cannot be removed';
    }
    try {
      await registry.ensureDefault();
      if (!registry.names.contains(name)) {
        return 'Error: no soliplex server named "$name"; '
            'use soliplex_list_servers to see configured names';
      }
      await registry.removeServer(name);
      notifyListeners(); // overlay drops the server from its selector
      return 'Removed soliplex server "$name".';
    } catch (e) {
      return 'Error removing soliplex server "$name": $e';
    }
  }

  Future<String> _listRooms(Map<String, dynamic> request) async {
    final server = _serverArg(request);
    try {
      final session = await registry.session(server);
      final rooms = await SoliplexClient(session).listRooms();
      await _refreshAuthState();
      // Surface other configured servers so the agent learns the names it can
      // pass as the `server` arg to query/reply elsewhere.
      final others = registry.names.where((n) => n != server).toList();
      final header = 'Rooms on "$server"'
          '${others.isEmpty ? '' : ' (other servers: ${others.join(', ')})'}:';
      if (rooms.isEmpty) return '$header\nNo rooms available.';
      return '$header\n${rooms.map((r) => '- ${r['room_id'] ?? r['id']}: '
          '${r['name'] ?? 'unnamed'} — ${r['description'] ?? 'no description'}').join('\n')}';
    } catch (e) {
      await _refreshAuthState();
      return 'Error listing rooms on "$server": $e';
    }
  }

  /// List conversation threads in a room so the agent can resume one via
  /// soliplex_reply. Pairs with reply: list → pick thread_id → reply.
  Future<String> _listThreads(Map<String, dynamic> request) async {
    final server = _serverArg(request);
    final roomId = request['room_id'] as String? ?? '';
    if (roomId.isEmpty) return 'Error: room_id is required';
    try {
      final session = await registry.session(server);
      final threads = await SoliplexClient(session).listThreads(roomId);
      await _refreshAuthState();
      if (threads.isEmpty) {
        return 'No threads in room "$roomId" on "$server".';
      }
      final lines = threads.map((t) {
        final id = t['thread_id'] ?? '?';
        final meta = t['metadata'] as Map<String, dynamic>?;
        final name = (meta?['name'] as String?)?.trim();
        final created = t['created'] as String?;
        final label = (name == null || name.isEmpty) ? '(untitled)' : name;
        return '- $id: $label${created == null ? '' : ' [$created]'}';
      }).join('\n');
      return 'Threads in room "$roomId" on "$server" — resume one with '
          'soliplex_reply(server, room_id, thread_id, message):\n$lines';
    } catch (e) {
      await _refreshAuthState();
      return 'Error listing threads in "$roomId" on "$server": $e';
    }
  }

  /// Fetch one room's info — name, description, welcome message, and the
  /// suggested prompts (pi `soliplex_get_room_info` tool). Pairs with
  /// soliplex_list_rooms (list → pick room_id → get_room_info).
  Future<String> _getRoomInfo(Map<String, dynamic> request) async {
    final server = _serverArg(request);
    final roomId = (request['room_id'] as String?)?.trim() ?? '';
    if (roomId.isEmpty) return 'Error: room_id is required';
    try {
      final session = await registry.session(server);
      final room = await SoliplexClient(session).getRoomInfo(roomId);
      await _refreshAuthState();
      return formatRoomInfo(server, roomId, room);
    } catch (e) {
      await _refreshAuthState();
      return 'Error getting room info for "$roomId" on "$server": $e';
    }
  }

  Future<String> _query(Map<String, dynamic> request) =>
      _runQuery(request, null);

  Future<String> _queryStream(
    Map<String, dynamic> request,
    ToolChunkSink emit,
  ) =>
      _runQuery(request, emit);

  /// Ask [question] in one room or many on a single server (`soliplex_query`).
  /// The `room_id` argument may be a single room id, several ids
  /// comma-separated, or `"*"` (every room on [server]); several resolved
  /// rooms fan out in parallel on that ONE server and aggregate into
  /// per-room-labeled sections ([formatFanOut]). With a single room the answer
  /// streams token-by-token through [onChunk]; with several, per-room deltas
  /// are dropped (a concurrent interleave is unreadable) and each room's
  /// completed block streams through [onChunk] the moment it finishes, while
  /// the call's [KeepaliveTicker] bridges every silent phase — including room
  /// parsing and `"*"` expansion, before any room is asked (#3485).
  Future<String> _runQuery(
    Map<String, dynamic> request,
    ToolChunkSink? onChunk,
  ) async {
    final server = _serverArg(request);
    final question = (request['question'] as String?)?.trim() ?? '';
    if (question.isEmpty) return 'Error: question is required';
    // ONE ticker spans the whole call: room parsing, "*" expansion (a
    // listRooms round-trip), and the dispatch below. The room handlers rely
    // on it — starting it only inside them would leave the expansion window
    // silent, the same bug this fixes (#3485).
    final ticker = KeepaliveTicker(
      onChunk,
      interval: keepaliveInterval,
      ceiling: keepaliveCeiling,
    )..start();
    try {
      final List<String> rooms;
      try {
        final parsed = _parseRoomsArg(request['room_id']) ?? ['search'];
        rooms = (await _expandRooms(server, parsed)).toSet().toList();
      } on ArgumentError catch (e) {
        return 'Error: ${e.message}';
      } catch (e) {
        // A `*` expansion can fail (unknown server / listRooms error). Surface
        // it as a validation-style error rather than a half-built fan-out.
        return 'Error expanding rooms: $e';
      }
      if (rooms.isEmpty) return 'Error: no rooms resolved from room_id';
      if (rooms.length == 1) {
        return await _queryOneRoom(server, rooms.single, question, onChunk);
      }
      return await _queryManyRooms(server, rooms, question, onChunk);
    } finally {
      ticker.stop();
    }
  }

  /// Parse the `room_id` argument: one room id, several ids comma-separated,
  /// or `"*"` (every room on the server). An explicit list is also accepted
  /// for callers that pass arrays through the bridge. Returns null when the
  /// argument is absent so the caller keeps the legacy `search` default.
  /// Throws [ArgumentError] on a malformed value so validation happens before
  /// any network fan-out.
  static List<String>? _parseRoomsArg(Object? raw) {
    if (raw == null) return null;
    if (raw is String) {
      final s = raw.trim();
      if (s.isEmpty) throw ArgumentError('room_id is required');
      final rooms = s.split(',').map((e) => e.trim()).toList();
      if (rooms.any((e) => e.isEmpty)) {
        throw ArgumentError('room_id entries must be non-empty');
      }
      return rooms;
    }
    if (raw is! List) {
      throw ArgumentError(
        'room_id must be a room id, comma-separated room ids, or "*"',
      );
    }
    if (raw.isEmpty) throw ArgumentError('room_id list must not be empty');
    final rooms = <String>[];
    for (final e in raw) {
      if (e is! String || e.trim().isEmpty) {
        throw ArgumentError('room_id entries must be non-empty strings');
      }
      rooms.add(e.trim());
    }
    return rooms;
  }

  /// Expand any `"*"` entry in [rooms] to every room on [server] (concrete ids
  /// pass through). Kept separate from dispatch/format so the expansion
  /// (including the `*` + listRooms path) is unit-testable through the
  /// MockClient — it hits `/api/v1/rooms` and returns BEFORE any live-SSE
  /// `_streamRun`. A bad server name or listRooms failure throws and is
  /// caught per-call in [_runQuery], becoming a validation-style error.
  Future<List<String>> _expandRooms(String server, List<String> rooms) async {
    if (!rooms.contains('*')) return rooms;
    final session = await registry.session(server);
    final list = await SoliplexClient(session).listRooms();
    final expanded = <String>[];
    for (final r in rooms) {
      if (r != '*') {
        expanded.add(r);
        continue;
      }
      for (final room in list) {
        final id = (room['room_id'] ?? room['id'])?.toString();
        if (id != null && id.isNotEmpty) expanded.add(id);
      }
    }
    return expanded;
  }

  /// Ask [question] in a single [roomId] on [server], streaming deltas through
  /// [onChunk] when present. Seeds the thread history so a later
  /// soliplex_reply has context. The caller's ([_runQuery]) KeepaliveTicker
  /// bridges the silent session + thread-creation phase and silent-but-active
  /// stretches; [queryDeadline] bounds the room so a hung server returns an
  /// error instead of stalling the call (#3485).
  Future<String> _queryOneRoom(
    String server,
    String roomId,
    String question,
    ToolChunkSink? onChunk,
  ) async {
    try {
      final session = await registry.session(server);
      final result = await SoliplexClient(
        session,
      ).queryRoom(roomId, question, onChunk: onChunk).timeout(queryDeadline);
      await _refreshAuthState();
      // Seed this thread's history so a later soliplex_reply has context.
      final key = (serverId: server, roomId: roomId, threadId: result.threadId);
      _threadHistory[key] = [
        sox.UserMessage(id: _mid('u'), content: question),
        sox.AssistantMessage(id: _mid('a'), content: result.text),
      ];
      // Surface server + thread id so the agent can continue this conversation
      // on the SAME server/thread via soliplex_reply (multi-turn).
      return '${result.text}\n\n[soliplex server: $server, '
          'thread_id: ${result.threadId} — continue with '
          'soliplex_reply(server, room_id, thread_id, message)]';
    } on TimeoutException {
      await _refreshAuthState();
      return 'Error: ${_deadlineMessage('room "$roomId" on "$server"')}';
    } catch (e) {
      await _refreshAuthState();
      return 'Error querying Soliplex: $e';
    }
  }

  /// Deadline message body shared by every query path (#3485). Callers that
  /// return it directly prepend `Error: `; fan-out callers store it in a
  /// [FanOutResult.error], whose block formatter adds the prefix — so it is
  /// never doubled.
  String _deadlineMessage(String what) =>
      'Soliplex $what did not answer within '
      '${queryDeadline.inSeconds}s (per-call deadline)';

  /// Run ONE question against MANY rooms on ONE server in parallel and
  /// aggregate into a single labeled block ([formatFanOut]). Each room is
  /// wrapped so a thrown error (server down, 401, no run, deadline) becomes a
  /// [FanOutResult] error entry rather than failing the batch (PARTIAL-FAILURE
  /// TOLERANT), and per-room thread history is seeded so a later
  /// soliplex_reply has context. Each room's completed block streams through
  /// [onChunk] the moment it finishes (#3485) — incremental display in
  /// completion order — while the caller's ([_runQuery]) KeepaliveTicker
  /// keeps the bridge alive through silent phases and [queryDeadline] bounds
  /// every room.
  Future<String> _queryManyRooms(
    String server,
    List<String> rooms,
    String question,
    ToolChunkSink? onChunk,
  ) async {
    Future<FanOutResult> askRoom(String room) async {
      try {
        final session = await registry.session(server);
        // Drop per-room token deltas (a concurrent interleave is unreadable);
        // the answer is collected whole below.
        final r = await SoliplexClient(
          session,
        ).queryRoom(room, question).timeout(queryDeadline);
        // Seed history so soliplex_reply on this exact (server, room, thread)
        // has prior context — same contract as _queryOneRoom.
        final key = (serverId: server, roomId: room, threadId: r.threadId);
        _threadHistory[key] = [
          sox.UserMessage(id: _mid('u'), content: question),
          sox.AssistantMessage(id: _mid('a'), content: r.text),
        ];
        final result = FanOutResult(
          server: server,
          room: room,
          answer: r.text,
          threadId: r.threadId,
        );
        onChunk?.call(formatFanOutBlock(result));
        return result;
      } on TimeoutException {
        final result = FanOutResult(
          server: server,
          room: room,
          error: _deadlineMessage('room "$room" on "$server"'),
        );
        onChunk?.call(formatFanOutBlock(result));
        return result;
      } catch (e) {
        final result = FanOutResult(server: server, room: room, error: '$e');
        onChunk?.call(formatFanOutBlock(result));
        return result;
      }
    }

    final results = await Future.wait(rooms.map(askRoom));
    await _refreshAuthState();
    return formatFanOut(question, results);
  }

  Future<String> _reply(Map<String, dynamic> request) =>
      _runReply(request, null);

  Future<String> _replyStream(
    Map<String, dynamic> request,
    ToolChunkSink emit,
  ) =>
      _runReply(request, emit);

  /// Continue an existing soliplex thread (multi-turn). Requires `thread_id`
  /// (from a prior soliplex_query) and a `message`; the soliplex backend keeps
  /// the thread history so the model sees the earlier turns. Same keepalive +
  /// deadline contract as the query paths (#3485).
  Future<String> _runReply(
    Map<String, dynamic> request,
    ToolChunkSink? onChunk,
  ) async {
    final server = _serverArg(request);
    final roomId = request['room_id'] as String? ?? 'search';
    final threadId = request['thread_id'] as String? ?? '';
    final message = request['message'] as String? ?? '';
    if (threadId.isEmpty) return 'Error: thread_id is required';
    if (message.isEmpty) return 'Error: message is required';
    final ticker = KeepaliveTicker(
      onChunk,
      interval: keepaliveInterval,
      ceiling: keepaliveCeiling,
    )..start();
    try {
      final session = await registry.session(server);
      final key = (serverId: server, roomId: roomId, threadId: threadId);
      final prior = _threadHistory[key] ?? <sox.Message>[];
      final result = await SoliplexClient(
        session,
      )
          .replyToThread(roomId, threadId, prior, message, onChunk: onChunk)
          .timeout(queryDeadline);
      await _refreshAuthState();
      _threadHistory[key] = [
        ...prior,
        sox.UserMessage(id: _mid('u'), content: message),
        sox.AssistantMessage(id: _mid('a'), content: result),
      ];
      return '$result\n\n[soliplex server: $server, thread_id: $threadId]';
    } on TimeoutException {
      await _refreshAuthState();
      return 'Error: ${_deadlineMessage('thread "$threadId" on "$server"')}';
    } catch (e) {
      await _refreshAuthState();
      return 'Error replying to Soliplex thread: $e';
    } finally {
      ticker.stop();
    }
  }

  /// Normalize a `thread_id` argument: blank/absent -> null (room-scoped). Used
  /// by every file tool so a passed-through empty string behaves like "no
  /// thread" rather than building a malformed thread URL.
  static String? _threadArg(Map<String, dynamic> request) {
    final raw = (request['thread_id'] as String?)?.trim();
    return (raw == null || raw.isEmpty) ? null : raw;
  }

  /// List files uploaded to a room (or a thread within it). The soliplex
  /// response only carries `filename` + `url` per file (no size/mtime), so we
  /// list names; the URL is omitted from the text since it is an internal,
  /// auth-gated download link the agent should reach via soliplex_get_file.
  /// Files live behind a room's `enable_attachments` flag. Returns an error
  /// message to hand back to the caller when attachments are explicitly
  /// disabled for [roomId], or null when files are available. Only blocks on an
  /// explicit `false` — if the flag is absent or the room lookup fails, we let
  /// the actual file operation surface its own error rather than over-blocking.
  Future<String?> _attachmentsBlocked(
    SoliplexServerSession session,
    String server,
    String roomId,
  ) async {
    try {
      final room = await SoliplexClient(session).getRoomInfo(roomId);
      final raw = room['enable_attachments'] ?? room['enableAttachments'];
      if (raw == false) {
        return 'Error: files are not available in room "$roomId" on "$server" '
            '— attachments are disabled for this room '
            '(see soliplex_get_room_info).';
      }
      return null;
    } catch (_) {
      return null;
    }
  }

  Future<String> _listFiles(Map<String, dynamic> request) async {
    final server = _serverArg(request);
    final roomId = (request['room_id'] as String?)?.trim() ?? '';
    if (roomId.isEmpty) return 'Error: room_id is required';
    final threadId = _threadArg(request);
    final scope = threadId == null ? 'room "$roomId"' : 'thread "$threadId"';
    try {
      final session = await registry.session(server);
      final blocked = await _attachmentsBlocked(session, server, roomId);
      if (blocked != null) {
        await _refreshAuthState();
        return blocked;
      }
      final files = await SoliplexClient(
        session,
      ).listFiles(roomId, threadId: threadId);
      await _refreshAuthState();
      if (files.isEmpty) return 'No files in $scope on "$server".';
      final lines = files
          .map((f) => '- ${f['name'] ?? f['filename'] ?? '(unnamed)'}')
          .join('\n');
      return 'Files in $scope on "$server":\n$lines';
    } catch (e) {
      await _refreshAuthState();
      return 'Error listing files in $scope on "$server": $e';
    }
  }

  /// Download a file's contents. Text is returned inline; binary is returned
  /// base64-encoded with a clear note + content type so the agent knows it is
  /// not literal text (and can decode it if it needs the raw bytes).
  Future<String> _getFile(Map<String, dynamic> request) async {
    final server = _serverArg(request);
    final roomId = (request['room_id'] as String?)?.trim() ?? '';
    final filename = (request['filename'] as String?)?.trim() ?? '';
    if (roomId.isEmpty) return 'Error: room_id is required';
    if (filename.isEmpty) return 'Error: filename is required';
    final threadId = _threadArg(request);
    try {
      final session = await registry.session(server);
      final blocked = await _attachmentsBlocked(session, server, roomId);
      if (blocked != null) {
        await _refreshAuthState();
        return blocked;
      }
      final file = await SoliplexClient(
        session,
      ).getFile(roomId, filename, threadId: threadId);
      await _refreshAuthState();
      if (file.base64) {
        return '[binary file "$filename"'
            '${file.contentType == null ? '' : ', ${file.contentType}'}'
            ' — base64-encoded below]\n${file.content}';
      }
      return file.content;
    } catch (e) {
      await _refreshAuthState();
      return 'Error getting file "$filename" on "$server": $e';
    }
  }

  /// Upload a file to a room (or thread). Exactly one of `content` (UTF-8 text)
  /// or `content_base64` (binary) must be supplied; we decode to bytes and POST.
  /// The xor + required-field checks happen BEFORE any network call so a bad
  /// invocation fails fast and cheaply.
  Future<String> _uploadFile(Map<String, dynamic> request) async {
    final server = _serverArg(request);
    final roomId = (request['room_id'] as String?)?.trim() ?? '';
    final filename = (request['filename'] as String?)?.trim() ?? '';
    final content = request['content'] as String?;
    final contentB64 = request['content_base64'] as String?;
    final contentType = (request['content_type'] as String?)?.trim();
    if (roomId.isEmpty) return 'Error: room_id is required';
    if (filename.isEmpty) return 'Error: filename is required';
    // Exactly one body source. Treat null as "absent"; an empty string still
    // counts as supplied (an empty file is legitimate) so we check for null.
    final hasText = content != null;
    final hasB64 = contentB64 != null;
    if (hasText == hasB64) {
      return 'Error: provide exactly one of content or content_base64';
    }
    final List<int> bytes;
    try {
      // The xor check above guarantees the chosen branch's value is non-null.
      // `content` promotes via `hasText`; `contentB64` needs the assertion since
      // the analyzer doesn't track the `hasB64` boolean back to nullability.
      bytes = hasText ? utf8.encode(content) : base64Decode(contentB64!);
    } on FormatException catch (e) {
      return 'Error: content_base64 is not valid base64: $e';
    }
    final threadId = _threadArg(request);
    final scope = threadId == null ? 'room "$roomId"' : 'thread "$threadId"';
    try {
      final session = await registry.session(server);
      final blocked = await _attachmentsBlocked(session, server, roomId);
      if (blocked != null) {
        await _refreshAuthState();
        return blocked;
      }
      await SoliplexClient(session).uploadFile(
        roomId,
        filename,
        bytes,
        threadId: threadId,
        contentType:
            (contentType == null || contentType.isEmpty) ? null : contentType,
      );
      await _refreshAuthState();
      return 'Uploaded "$filename" (${bytes.length} bytes) to $scope on "$server".';
    } catch (e) {
      await _refreshAuthState();
      return 'Error uploading "$filename" to $scope on "$server": $e';
    }
  }
}

/// App bar icon: a compact hub icon that toggles the overlay panel. Tinted
/// when any server is connected.
class _SoliplexAppBarIcon extends StatefulWidget {
  final SoliplexFeature feature;
  const _SoliplexAppBarIcon({super.key, required this.feature});

  @override
  State<_SoliplexAppBarIcon> createState() => _SoliplexAppBarIconState();
}

class _SoliplexAppBarIconState extends State<_SoliplexAppBarIcon> {
  @override
  void initState() {
    super.initState();
    widget.feature.addListener(_onUpdate);
  }

  @override
  void dispose() {
    widget.feature.removeListener(_onUpdate);
    super.dispose();
  }

  void _onUpdate() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    final connected = widget.feature.authenticated;
    final scheme = Theme.of(context).colorScheme;
    return IconButton(
      key: const ValueKey('soliplex_overlay_icon'),
      icon: Icon(
        Icons.hub,
        size: 20,
        color: connected ? scheme.primary : scheme.onSurfaceVariant,
      ),
      tooltip: 'Soliplex servers',
      onPressed: widget.feature.toggleOverlay,
    );
  }
}

/// Expanded overlay panel: per-server status list with connect/logout actions
/// and an "add server" form. Shown when the app bar icon is tapped. Single
/// active server: at most one server is connected at a time — connecting to
/// another disconnects the currently active one (connecting IS the switch).
class _SoliplexAuthOverlay extends StatefulWidget {
  final SoliplexFeature feature;
  const _SoliplexAuthOverlay({super.key, required this.feature});

  @override
  State<_SoliplexAuthOverlay> createState() => _SoliplexAuthOverlayState();
}

class _SoliplexAuthOverlayState extends State<_SoliplexAuthOverlay> {
  bool get _expanded => widget.feature.overlayExpanded;

  List<SoliplexServer> _servers = const [];
  final Map<String, bool> _connected = {};

  // Per-server connect flow (auth-system picker for one server at a time).
  String? _connectingServer;
  Map<String, dynamic>? _authSystems;
  String? _selectedSystem;
  bool _loadingSystems = false;
  bool _authError =
      false; // true only on a real fetch failure (non-200/network)

  // Remove confirmation: tracks which server name is pending removal.
  String? _confirmingRemove;

  // Add-server form.
  bool _showAdd = false;
  final TextEditingController _nameCtrl = TextEditingController();
  final TextEditingController _urlCtrl = TextEditingController();
  String? _addError;

  @override
  void initState() {
    super.initState();
    widget.feature.addListener(_onUpdate);
    _refreshServers();
  }

  @override
  void dispose() {
    widget.feature.removeListener(_onUpdate);
    _nameCtrl.dispose();
    _urlCtrl.dispose();
    super.dispose();
  }

  void _onUpdate() {
    if (mounted) setState(() {});
  }

  Future<void> _refreshServers() async {
    final servers = await widget.feature.listServers();
    final status = <String, bool>{};
    for (final s in servers) {
      status[s.name] = await widget.feature.isServerConnected(s.name);
    }
    if (!mounted) return;
    setState(() {
      _servers = servers;
      _connected
        ..clear()
        ..addAll(status);
    });
  }

  Future<void> _toggleExpand() async {
    widget.feature.toggleOverlay();
    if (_expanded) await _refreshServers();
  }

  Future<void> _startConnect(String server) async {
    _connectingServer = server;
    _authSystems = null;
    _selectedSystem = null;
    _authError = false;
    _loadingSystems = true;
    if (mounted) setState(() {});
    try {
      // Empty map = open / no-auth server (valid). Only a thrown error (non-200
      // or network) is a real failure.
      final systems = await widget.feature.getAuthSystems(server: server);
      _authSystems = systems;
      if (systems.isNotEmpty) {
        _selectedSystem = systems.keys.first;
      } else {
        // Open / no-auth server: usable immediately, so mark it connected and
        // reflect that in the collapsed icon and this row's status dot.
        await widget.feature.markServerOpenConnected(server);
        _connected[server] = true;
      }
    } catch (e, st) {
      debugPrint('[Soliplex] Failed to load auth systems: $e\n$st');
      _authError = true;
    } finally {
      _loadingSystems = false;
      if (mounted) setState(() {});
    }
  }

  void _cancelConnect() {
    _connectingServer = null;
    _authSystems = null;
    _selectedSystem = null;
    _authError = false;
    if (mounted) setState(() {});
  }

  Future<void> _doConnect() async {
    final server = _connectingServer;
    final system = _selectedSystem;
    if (server == null || system == null) return;
    await widget.feature.login(system, server: server);
    _cancelConnect();
    await _refreshServers();
  }

  Future<void> _doLogout(String server) async {
    await widget.feature.logout(server: server);
    await _refreshServers();
  }

  Future<void> _doRemoveServer(String server) async {
    await widget.feature.removeServerFromUi(server);
    await _refreshServers();
  }

  Future<void> _submitAdd() async {
    final err = await widget.feature.addServerFromUi(
      _nameCtrl.text,
      _urlCtrl.text,
    );
    if (!mounted) return;
    if (err != null) {
      setState(() => _addError = err);
      return;
    }
    _nameCtrl.clear();
    _urlCtrl.clear();
    setState(() {
      _addError = null;
      _showAdd = false;
    });
    await _refreshServers();
  }

  @override
  Widget build(BuildContext context) {
    final scheme = Theme.of(context).colorScheme;
    if (!_expanded) return const SizedBox.shrink();

    return Positioned(
      top: 8,
      right: 8,
      child: SizedBox(
        width: 280,
        child: Material(
          elevation: 4,
          borderRadius: BorderRadius.circular(8),
          color: scheme.surface,
          child: SelectionArea(
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Icon(Icons.hub, size: 16, color: scheme.onSurface),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          'Soliplex servers',
                          style: TextStyle(
                            fontSize: 12,
                            fontWeight: FontWeight.bold,
                            color: scheme.onSurface,
                          ),
                        ),
                      ),
                      InkWell(
                        key: const ValueKey('soliplex_overlay_close'),
                        onTap: _toggleExpand,
                        child: Icon(
                          Icons.close,
                          size: 16,
                          color: scheme.onSurface,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Text(
                    'One server connected at a time — connecting to another '
                    'disconnects it.',
                    style: TextStyle(
                      fontSize: 10,
                      color: scheme.onSurfaceVariant,
                    ),
                  ),
                  const SizedBox(height: 8),
                  ..._servers.map(_serverRow),
                  const Divider(height: 16),
                  if (_showAdd) _addForm(scheme) else _addToggle(scheme),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }

  Widget _serverRow(SoliplexServer s) {
    final scheme = Theme.of(context).colorScheme;
    final connected = _connected[s.name] ?? false;
    final isConnecting = _connectingServer == s.name;
    return MouseRegion(
      cursor: SystemMouseCursors.basic,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              // Remove button (X) or equal-width spacer so names align.
              if (s.name != SoliplexServerRegistry.defaultName && !isConnecting)
                if (_confirmingRemove == s.name)
                  TextButton(
                    key: ValueKey('soliplex_remove_confirm_${s.name}'),
                    onPressed: () {
                      _confirmingRemove = null;
                      _doRemoveServer(s.name);
                    },
                    style: TextButton.styleFrom(
                      foregroundColor: scheme.error,
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                      minimumSize: const Size(0, 28),
                    ),
                    child: const Text(
                      'Remove?',
                      style: TextStyle(fontSize: 12),
                    ),
                  )
                else
                  InkWell(
                    key: ValueKey('soliplex_remove_${s.name}'),
                    onTap: () => setState(() => _confirmingRemove = s.name),
                    child: Padding(
                      padding: const EdgeInsets.only(right: 4),
                      child: Icon(
                        Icons.close,
                        size: 14,
                        color: scheme.onSurfaceVariant,
                      ),
                    ),
                  )
              else
                const SizedBox(width: 18),
              Icon(
                Icons.circle,
                size: 10,
                color: connected ? Colors.green : scheme.outline,
              ),
              const SizedBox(width: 6),
              Expanded(
                child: Text(
                  s.name,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 12, color: scheme.onSurface),
                ),
              ),
              if (connected)
                TextButton(
                  key: ValueKey('soliplex_logout_${s.name}'),
                  onPressed: () => _doLogout(s.name),
                  style: TextButton.styleFrom(
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                    minimumSize: const Size(0, 28),
                  ),
                  child: const Text('Logout', style: TextStyle(fontSize: 12)),
                )
              else if (!isConnecting)
                TextButton(
                  key: ValueKey('soliplex_connect_${s.name}'),
                  onPressed: widget.feature.loggingIn
                      ? null
                      : () => _startConnect(s.name),
                  style: TextButton.styleFrom(
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                    minimumSize: const Size(0, 28),
                  ),
                  child: const Text('Connect', style: TextStyle(fontSize: 12)),
                ),
            ],
          ),
          if (isConnecting) _connectPicker(scheme),
        ],
      ),
    );
  }

  Widget _connectPicker(ColorScheme scheme) {
    if (_loadingSystems) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 6),
        child: SizedBox(
          width: 14,
          height: 14,
          child: CircularProgressIndicator(strokeWidth: 2),
        ),
      );
    }
    if (_authError) {
      return Padding(
        padding: const EdgeInsets.only(left: 16, bottom: 6),
        child: Text(
          'Failed to load providers',
          style: TextStyle(fontSize: 11, color: scheme.error),
        ),
      );
    }
    if (_authSystems == null || _authSystems!.isEmpty) {
      // Open / no-auth server: nothing to log into; queries work directly.
      return Padding(
        padding: const EdgeInsets.only(left: 16, bottom: 6),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              'No login required — this server is open.',
              style: TextStyle(fontSize: 11, color: scheme.onSurfaceVariant),
            ),
            TextButton(
              key: const ValueKey('soliplex_connect_done'),
              onPressed: _cancelConnect,
              style: TextButton.styleFrom(
                padding: const EdgeInsets.symmetric(horizontal: 8),
                minimumSize: const Size(0, 28),
              ),
              child: const Text('OK', style: TextStyle(fontSize: 12)),
            ),
          ],
        ),
      );
    }
    return Padding(
      padding: const EdgeInsets.only(left: 16, bottom: 4),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          ..._authSystems!.entries.map((e) {
            final title =
                (e.value as Map<String, dynamic>)['title'] as String? ?? e.key;
            return InkWell(
              onTap: () => setState(() => _selectedSystem = e.key),
              mouseCursor: SystemMouseCursors.click,
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Radio<String>(
                    value: e.key,
                    groupValue: _selectedSystem,
                    onChanged: (v) => setState(() => _selectedSystem = v),
                    materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    visualDensity: VisualDensity.compact,
                  ),
                  Text(title, style: const TextStyle(fontSize: 12)),
                ],
              ),
            );
          }),
          if (widget.feature.loginError != null)
            Text(
              widget.feature.loginError!,
              style: TextStyle(fontSize: 10, color: scheme.error),
            ),
          Row(
            children: [
              TextButton(
                key: const ValueKey('soliplex_connect_submit'),
                onPressed: (widget.feature.loggingIn || _selectedSystem == null)
                    ? null
                    : _doConnect,
                child: widget.feature.loggingIn
                    ? const SizedBox(
                        width: 14,
                        height: 14,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : const Text('Connect', style: TextStyle(fontSize: 12)),
              ),
              TextButton(
                onPressed: _cancelConnect,
                child: const Text('Cancel', style: TextStyle(fontSize: 12)),
              ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _addToggle(ColorScheme scheme) {
    return TextButton.icon(
      key: const ValueKey('soliplex_add_toggle'),
      onPressed: () => setState(() => _showAdd = true),
      icon: const Icon(Icons.add, size: 16),
      label: const Text('Add server', style: TextStyle(fontSize: 12)),
      style: TextButton.styleFrom(
        padding: const EdgeInsets.symmetric(horizontal: 8),
        minimumSize: const Size(0, 28),
      ),
    );
  }

  Widget _addForm(ColorScheme scheme) {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        TextField(
          key: const ValueKey('soliplex_add_name'),
          controller: _nameCtrl,
          style: const TextStyle(fontSize: 12),
          decoration: const InputDecoration(
            isDense: true,
            labelText: 'Name',
            labelStyle: TextStyle(fontSize: 12),
          ),
        ),
        const SizedBox(height: 4),
        TextField(
          key: const ValueKey('soliplex_add_url'),
          controller: _urlCtrl,
          style: const TextStyle(fontSize: 12),
          decoration: const InputDecoration(
            isDense: true,
            labelText: 'URL (https://…)',
            labelStyle: TextStyle(fontSize: 12),
          ),
        ),
        if (_addError != null)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(
              _addError!,
              style: TextStyle(fontSize: 10, color: scheme.error),
            ),
          ),
        Row(
          children: [
            TextButton(
              key: const ValueKey('soliplex_add_submit'),
              onPressed: _submitAdd,
              child: const Text('Add', style: TextStyle(fontSize: 12)),
            ),
            TextButton(
              onPressed: () => setState(() {
                _showAdd = false;
                _addError = null;
              }),
              child: const Text('Cancel', style: TextStyle(fontSize: 12)),
            ),
          ],
        ),
      ],
    );
  }
}
