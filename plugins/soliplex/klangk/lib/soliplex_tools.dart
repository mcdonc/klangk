import 'dart:convert';
import 'package:flutter/services.dart' show rootBundle;
import 'package:http/http.dart' as http;
import 'package:http_parser/http_parser.dart' show MediaType;
import 'package:soliplex_client/soliplex_client.dart' as sox;

import 'soliplex_servers.dart';

/// Asset path for the bundled default-server config (declared in pubspec
/// `flutter: assets:`). Referenced through the package prefix so it resolves
/// from the consuming klangk frontend, not just this package.
const _defaultConfigAsset =
    'packages/klangk_plugin_soliplex/assets/soliplex_config.json';

/// Read the bundled `default_url` from the plugin asset. Returns null if the
/// asset is missing or malformed (registry then falls back to legacy config).
Future<String?> _loadBundledDefaultUrl() async {
  try {
    final raw = await rootBundle.loadString(_defaultConfigAsset);
    final data = jsonDecode(raw) as Map<String, dynamic>;
    return data['default_url'] as String?;
  } catch (_) {
    return null;
  }
}

/// Process-wide registry of Soliplex servers. The plugin defaults to this one;
/// tests construct their own with an injected loader/`http.Client`.
final SoliplexServerRegistry soliplexServers = SoliplexServerRegistry(
  defaultUrlLoader: _loadBundledDefaultUrl,
);

/// Accumulates RAG citations seen across an AG-UI run so they can be appended
/// to the answer text as a compact "Sources" list (CITATIONS PROTOTYPE).
///
/// WHY a separate object: the citation data lives in AG-UI *state*, not in the
/// text deltas. The backend emits a `StateSnapshotEvent` (full state) and/or
/// `StateDeltaEvent`s (RFC-6902 JSON Patch) whose `rag` namespace holds the
/// retrieved chunks (see soliplex_client `citation_extractor.dart` /
/// `rag_snapshot.dart`). To resolve citations we must replay those state events
/// to reconstruct the current state, then diff against the previous state — the
/// exact contract `CitationExtractor.extractNew(prev, curr)` expects. Keeping
/// that bookkeeping here (a) isolates the live-SSE-coupled `_streamRun` from it
/// and (b) makes the accumulation logic a plain, unit-testable unit (the
/// soliplex_client transport in `_streamRun` is coverage-ignored; this is not).
class CitationAccumulator {
  CitationAccumulator({sox.CitationExtractor? extractor})
    : _extractor = extractor ?? sox.CitationExtractor();

  final sox.CitationExtractor _extractor;

  /// The reconstructed agent state, mutated as state events arrive. Starts
  /// empty; `extractNew` treats an empty previous state as "no prior citations".
  Map<String, dynamic> _state = <String, dynamic>{};

  /// Ordered, de-duplicated sources collected so far. Order is first-seen
  /// (the order the backend surfaced them), de-duped by `chunkId`.
  final List<sox.SourceReference> _sources = <sox.SourceReference>[];
  final Set<String> _seenChunkIds = <String>{};

  /// Sources collected so far, in first-seen order (unmodifiable view).
  List<sox.SourceReference> get sources => List.unmodifiable(_sources);

  /// Feed one AG-UI event. Returns true if it was a state event we consumed
  /// (snapshot or delta); the caller still emits its keepalive either way.
  ///
  /// Non-state events are ignored here — text deltas are handled by the caller,
  /// and other events carry no citation state.
  bool consume(sox.BaseEvent event) {
    if (event is sox.StateSnapshotEvent) {
      // A snapshot replaces state wholesale. `snapshot` is typed `State`
      // (== dynamic upstream); guard the cast so a malformed frame can't throw.
      final snap = event.snapshot;
      _applyState(snap is Map<String, dynamic> ? snap : <String, dynamic>{});
      return true;
    }
    if (event is sox.StateDeltaEvent) {
      // A delta patches the running state (JSON Patch). Reuse the same patch
      // engine soliplex_client uses so our reconstructed state matches theirs.
      _applyState(sox.applyJsonPatch(_state, event.delta));
      return true;
    }
    return false;
  }

  /// Replace the running state with [next], harvesting any newly-introduced
  /// citations (diffed against the prior state) into [_sources].
  void _applyState(Map<String, dynamic> next) {
    final fresh = _extractor.extractNew(_state, next);
    for (final ref in fresh) {
      if (_seenChunkIds.add(ref.chunkId)) _sources.add(ref);
    }
    _state = next;
  }
}

/// Renders collected [sources] as a compact text block to append to an answer
/// (CITATIONS PROTOTYPE — inline text only, no pi-tui component).
///
/// Returns an empty string when there are no sources, so callers can append
/// unconditionally. Numbers from [1]: if the model already emitted inline
/// `[n]` markers in the answer, the backend assigns each citation an `index`,
/// and we honour it when present so the list lines up with those markers;
/// otherwise we fall back to first-seen ordering. Label prefers the document
/// title, then the URI's filename (`displayTitle`).
String formatSources(List<sox.SourceReference> sources) {
  if (sources.isEmpty) return '';
  final lines = <String>[];
  for (var i = 0; i < sources.length; i++) {
    final s = sources[i];
    final n = s.index ?? (i + 1);
    lines.add('[$n] ${s.displayTitle}');
  }
  return '\n\nSources:\n${lines.join('\n')}';
}

/// Lightweight Soliplex API client bound to one server [session]. All requests
/// go to `session.baseUrl` with `session.headers()` (bearer when available),
/// over the session's injectable `http.Client`.
class SoliplexClient {
  SoliplexClient(this.session);

  final SoliplexServerSession session;

  http.Client get _http => session.httpClient;
  String get _baseUrl => session.baseUrl;

  Future<Never> _unauthenticated() async {
    await session.clearStoredTokens();
    throw Exception(
      'Not authenticated. Click "Connect to Soliplex" to log in.',
    );
  }

  /// List all rooms the user has access to on this server.
  Future<List<Map<String, dynamic>>> listRooms() async {
    final response = await _http.get(
      Uri.parse('$_baseUrl/api/v1/rooms'),
      headers: await session.headers(),
    );
    if (response.statusCode == 401) await _unauthenticated();
    if (response.statusCode != 200) {
      throw Exception(
        'Failed to list rooms: ${response.statusCode} ${response.body}',
      );
    }
    final data = jsonDecode(response.body);
    if (data is Map) {
      return data.entries.map((e) {
        final room = e.value as Map<String, dynamic>;
        return {'room_id': e.key, ...room};
      }).toList();
    }
    if (data is List) {
      return data.cast<Map<String, dynamic>>();
    }
    return [];
  }

  /// Fetch one room's full info — name, description, welcome message, the
  /// suggested prompts, and capability flags. Endpoint:
  /// `GET /api/v1/rooms/{room_id}` (the single-room detail the server uses to
  /// populate [sox.Room], including `suggestions`).
  Future<Map<String, dynamic>> getRoomInfo(String roomId) async {
    final response = await _http.get(
      Uri.parse('$_baseUrl/api/v1/rooms/$roomId'),
      headers: await session.headers(),
    );
    if (response.statusCode == 401) await _unauthenticated();
    if (response.statusCode != 200) {
      throw Exception(
        'Failed to get room info: ${response.statusCode} ${response.body}',
      );
    }
    final data = jsonDecode(response.body);
    if (data is Map<String, dynamic>) return data;
    throw Exception('Unexpected room info response: ${response.body}');
  }

  /// List the conversation threads in [roomId] so the agent can resume one via
  /// [replyToThread]. Each entry has `thread_id`, `created`, and `metadata`
  /// ({name, description}). Server shape: `{ "threads": [AGUI_Thread...] }`.
  Future<List<Map<String, dynamic>>> listThreads(String roomId) async {
    final response = await _http.get(
      Uri.parse('$_baseUrl/api/v1/rooms/$roomId/agui'),
      headers: await session.headers(),
    );
    if (response.statusCode == 401) await _unauthenticated();
    if (response.statusCode != 200) {
      throw Exception(
        'Failed to list threads: ${response.statusCode} ${response.body}',
      );
    }
    final data = jsonDecode(response.body);
    final threads = data is Map ? data['threads'] : data;
    if (threads is! List) return [];
    return threads.cast<Map<String, dynamic>>();
  }

  /// Build the uploads base URL for a room, or a room's thread when [threadId]
  /// is given. The soliplex GET routes are:
  ///   room   = /api/v1/uploads/{room_id}
  ///   thread = /api/v1/uploads/{room_id}/thread/{thread_id}
  /// (verified against soliplex views/file_uploads.py). Kept as a pure helper so
  /// the URL shape is testable without a live server.
  String uploadsUrl(String roomId, {String? threadId}) =>
      (threadId == null || threadId.isEmpty)
      ? '$_baseUrl/api/v1/uploads/$roomId'
      : '$_baseUrl/api/v1/uploads/$roomId/thread/$threadId';

  /// Build the single-file GET URL. soliplex puts a literal `/file/` segment
  /// before the filename on BOTH the room and thread download routes:
  ///   room   = /api/v1/uploads/{room_id}/file/{filename}
  ///   thread = /api/v1/uploads/{room_id}/thread/{thread_id}/file/{filename}
  /// (this differs from the list routes, which have no `/file/`). The filename
  /// is percent-encoded so names with spaces/special chars resolve correctly.
  String fileUrl(String roomId, String filename, {String? threadId}) {
    final enc = Uri.encodeComponent(filename);
    return (threadId == null || threadId.isEmpty)
        ? '$_baseUrl/api/v1/uploads/$roomId/file/$enc'
        : '$_baseUrl/api/v1/uploads/$roomId/thread/$threadId/file/$enc';
  }

  /// Build the multipart upload (POST) URL. NOTE the thread POST route has NO
  /// `/thread/` segment — it is /api/v1/uploads/{room_id}/{thread_id} — whereas
  /// the room POST is /api/v1/uploads/{room_id} (verified in file_uploads.py:141
  /// and :313). This asymmetry with the GET routes is deliberate on the server.
  String uploadPostUrl(String roomId, {String? threadId}) =>
      (threadId == null || threadId.isEmpty)
      ? '$_baseUrl/api/v1/uploads/$roomId'
      : '$_baseUrl/api/v1/uploads/$roomId/$threadId';

  /// List files uploaded to a [roomId], or to a thread within it when
  /// [threadId] is given. Normalizes the soliplex `RoomUploads`/`ThreadUploads`
  /// response (`{room_id, uploads: [{filename, url}]}`) into a flat list of
  /// `{name, url, ...}` maps. `name` is the canonical key (mirrors the
  /// room_id-normalization that [listRooms] does) while the original `filename`
  /// is preserved too, so callers can use either.
  Future<List<Map<String, dynamic>>> listFiles(
    String roomId, {
    String? threadId,
  }) async {
    final response = await _http.get(
      Uri.parse(uploadsUrl(roomId, threadId: threadId)),
      headers: await session.headers(),
    );
    if (response.statusCode == 401) await _unauthenticated();
    if (response.statusCode != 200) {
      throw Exception(
        'Failed to list files: ${response.statusCode} ${response.body}',
      );
    }
    final data = jsonDecode(response.body);
    if (data is! Map) return [];
    final uploads = data['uploads'];
    if (uploads is! List) return [];
    return uploads.whereType<Map>().map((u) {
      final m = u.cast<String, dynamic>();
      return <String, dynamic>{'name': m['filename'], ...m};
    }).toList();
  }

  /// Max bytes of decoded text we return inline before truncating. A large file
  /// would otherwise blow up the tool result (and the model's context); the
  /// caller is told when truncation happened so it can fetch a narrower slice.
  static const int maxTextBytes = 64 * 1024;

  /// Download a single file from a [roomId] (or a thread within it). Returns the
  /// body as UTF-8 [content] when it decodes cleanly (text), otherwise as a
  /// base64 string with [base64] = true (binary). [contentType] is the server's
  /// Content-Type header when present. Text longer than [maxTextBytes] is
  /// truncated with a trailing note; binary is never truncated here (the caller
  /// decides) but is reported via the base64 flag.
  Future<({String content, bool base64, String? contentType})> getFile(
    String roomId,
    String filename, {
    String? threadId,
  }) async {
    final response = await _http.get(
      Uri.parse(fileUrl(roomId, filename, threadId: threadId)),
      headers: await session.headers(),
    );
    if (response.statusCode == 401) await _unauthenticated();
    if (response.statusCode != 200) {
      throw Exception(
        'Failed to get file "$filename": '
        '${response.statusCode} ${response.body}',
      );
    }
    final contentType = response.headers['content-type'];
    final bytes = response.bodyBytes;
    // Prefer text: try a strict UTF-8 decode. If it throws, the bytes aren't
    // valid UTF-8 (binary), so fall back to base64. We decode the bytes
    // ourselves rather than trust response.body, because http decodes with the
    // charset from Content-Type (often latin-1) and would mojibake real UTF-8.
    try {
      final text = utf8.decode(bytes);
      if (text.length > maxTextBytes) {
        return (
          content:
              '${text.substring(0, maxTextBytes)}\n'
              '...[truncated: ${text.length} chars total, '
              'showing first $maxTextBytes]',
          base64: false,
          contentType: contentType,
        );
      }
      return (content: text, base64: false, contentType: contentType);
    } on FormatException {
      return (
        content: base64Encode(bytes),
        base64: true,
        contentType: contentType,
      );
    }
  }

  /// Upload [bytes] as [filename] to a [roomId] (or a thread within it). Uses a
  /// multipart POST with the form field name `upload_file` — the EXACT field the
  /// soliplex endpoint expects (it binds a single `upload_file: fastapi.UploadFile`,
  /// see file_uploads.py:144 and :317), NOT a `files` list. The server responds
  /// 204 No Content on success. 401 clears tokens; any other non-2xx throws with
  /// the status + body.
  Future<void> uploadFile(
    String roomId,
    String filename,
    List<int> bytes, {
    String? threadId,
    String? contentType,
  }) async {
    final request = http.MultipartRequest(
      'POST',
      Uri.parse(uploadPostUrl(roomId, threadId: threadId)),
    );
    request.headers.addAll(await session.headers());
    request.files.add(
      http.MultipartFile.fromBytes(
        'upload_file', // soliplex's single UploadFile param name (verified)
        bytes,
        filename: filename,
        contentType: _mediaTypeOrNull(contentType),
      ),
    );
    final streamed = await _http.send(request);
    if (streamed.statusCode == 401) await _unauthenticated();
    if (streamed.statusCode < 200 || streamed.statusCode >= 300) {
      final body = await streamed.stream.bytesToString();
      throw Exception(
        'Failed to upload "$filename": ${streamed.statusCode} $body',
      );
    }
  }

  /// Parse a Content-Type string into a [MediaType], or null when absent/blank
  /// or unparseable — so a bad caller value degrades to "let the client default
  /// it" rather than throwing mid-upload.
  static MediaType? _mediaTypeOrNull(String? contentType) {
    if (contentType == null || contentType.trim().isEmpty) return null;
    try {
      return MediaType.parse(contentType);
    } catch (_) {
      return null;
    }
  }

  /// Query a room by creating a thread, posting a question, and collecting the
  /// streamed response. Returns the answer [text] plus the new [threadId] so a
  /// caller can continue the conversation via [replyToThread] (multi-turn).
  Future<({String text, String threadId})> queryRoom(
    String roomId,
    String question, {
    void Function(String delta)? onChunk,
  }) async {
    final threadResp = await _http.post(
      Uri.parse('$_baseUrl/api/v1/rooms/$roomId/agui'),
      headers: await session.headers(),
      body: jsonEncode({}),
    );
    if (threadResp.statusCode == 401) await _unauthenticated();
    if (threadResp.statusCode != 200) {
      throw Exception(
        'Failed to create thread: '
        '${threadResp.statusCode} ${threadResp.body}',
      );
    }
    final threadData = jsonDecode(threadResp.body);
    final threadId = threadData['thread_id'] as String;

    final runs = threadData['runs'] as Map<String, dynamic>? ?? {};
    if (runs.isEmpty) {
      throw Exception('No run created for thread');
    }
    final runId = runs.keys.first;

    final text = await _streamRun(roomId, threadId, runId, [
      sox.UserMessage(id: _messageId(0), content: question),
    ], onChunk);
    return (text: text, threadId: threadId);
  }

  /// Continue an existing thread: create a follow-up run on [threadId] and
  /// stream the answer. This is what makes multi-turn conversations with a
  /// soliplex room possible.
  ///
  /// [priorMessages] is the conversation so far (user/assistant turns). The
  /// AG-UI run input carries the full message list — the backend does NOT
  /// replay a thread's history into a new run on its own, so the caller must
  /// supply it for the model to see earlier turns. The new [message] is
  /// appended as the latest user turn. Returns the assistant's answer.
  Future<String> replyToThread(
    String roomId,
    String threadId,
    List<sox.Message> priorMessages,
    String message, {
    void Function(String delta)? onChunk,
  }) async {
    // Create a new run on the existing thread: POST .../agui/{threadId}.
    final runResp = await _http.post(
      Uri.parse('$_baseUrl/api/v1/rooms/$roomId/agui/$threadId'),
      headers: await session.headers(),
      body: jsonEncode({}),
    );
    if (runResp.statusCode == 401) await _unauthenticated();
    if (runResp.statusCode != 200) {
      throw Exception(
        'Failed to create run on thread $threadId: '
        '${runResp.statusCode} ${runResp.body}',
      );
    }
    final runData = jsonDecode(runResp.body) as Map<String, dynamic>;
    final runId = runData['run_id'] as String?;
    if (runId == null) {
      throw Exception('No run_id returned for thread $threadId');
    }

    final messages = <sox.Message>[
      ...priorMessages,
      sox.UserMessage(id: _messageId(priorMessages.length), content: message),
    ];
    return _streamRun(roomId, threadId, runId, messages, onChunk);
  }

  /// Stable-ish unique message id for a run input.
  String _messageId(int index) =>
      'msg-${DateTime.now().millisecondsSinceEpoch}-$index';

  /// Stream a single AG-UI run, accumulating assistant text deltas and
  /// forwarding each to [onChunk] as it arrives. Shared by [queryRoom] and
  /// [replyToThread]. [messages] is the full conversation sent to the run.
  ///
  /// Streams via soliplex_client's AgUiStreamClient rather than hand-rolling
  /// the SSE. The transport's AuthenticatedHttpClient injects the bearer;
  /// getToken is synchronous, so pre-fetch (and refresh) once.
  // coverage:ignore-start
  Future<String> _streamRun(
    String roomId,
    String threadId,
    String runId,
    List<sox.Message> messages,
    void Function(String delta)? onChunk,
  ) async {
    // Auth is optional: a no-auth Soliplex deployment needs no bearer. Only
    // wrap the client with the authenticator when we actually have a token.
    String token = '';
    try {
      token = await session.getAccessToken();
    } catch (_) {
      // No token — proceed unauthenticated (no-auth server).
    }
    final inner = sox.DartHttpClient();
    final agui = sox.AgUiStreamClient(
      httpTransport: sox.HttpTransport(
        client: token.isNotEmpty
            ? sox.AuthenticatedHttpClient(inner, () => token)
            : inner,
      ),
      urlBuilder: sox.UrlBuilder('$_baseUrl/api/v1'),
    );
    try {
      final input = sox.SimpleRunAgentInput(
        threadId: threadId,
        runId: runId,
        messages: messages,
      );
      final buffer = StringBuffer();
      // CITATIONS PROTOTYPE: collect RAG sources from state events as they
      // stream, so we can append a "Sources" block to the answer.
      final citations = CitationAccumulator();
      await for (final outcome in agui.runAgent(
        'rooms/$roomId/agui/$threadId/$runId',
        input,
      )) {
        // runAgent yields DecodeOutcomes; unwrap decoded events and collect
        // text deltas. DecodeFailed outcomes are skipped.
        if (outcome is sox.DecodedEvent) {
          final event = outcome.event;
          if (event is sox.TextMessageContentEvent) {
            buffer.write(event.delta);
            onChunk?.call(event.delta);
          } else {
            // Harvest citations from state events. This does NOT change the
            // keepalive contract below: state events still fall through to the
            // empty-chunk relay, so the idle timer is reset exactly as before.
            citations.consume(event);
            // Keepalive: forward an empty chunk for every other AG-UI event
            // (run/activity/tool/thinking). The klangk bridge bounds the gap
            // BETWEEN chunks (KLANGK_BRIDGE_TIMEOUT_SECONDS, default 30s); a
            // long-but-active run (RAG + LLM, multi-step tools) emits these
            // frequently, so relaying them resets the idle timer and the
            // request never times out — even past 30s/2min. Empty deltas add
            // nothing to the answer text. (See mcdonc/klangk#82.)
            onChunk?.call('');
          }
        }
      }
      final answer = buffer.isNotEmpty
          ? buffer.toString()
          : '(No response from Soliplex)';
      // CITATIONS PROTOTYPE: append a compact Sources list when any were seen.
      // formatSources returns '' when empty, so this is a no-op otherwise.
      return answer + formatSources(citations.sources);
    } finally {
      agui.close();
    }
  }

  // coverage:ignore-end
}
