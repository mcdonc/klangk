# Soliplex Network Inspector — Implementation Plan

A developer-facing "Network" tab (DevTools-style, scoped to Soliplex) that
surfaces the HTTP/SSE traffic the Soliplex integration generates: method, URL,
status, headers (incl. `Authorization: Bearer`), request/response bodies, and
the streamed SSE events from `queryRoom`. Goal is debuggability; tokens are
redacted by default with an opt-in reveal.

> Scope note: this is a design plan. Nothing here is implemented yet.

---

## 1. Where the traffic actually originates (grounding)

All Soliplex HTTP/SSE calls live in **one file**, the plugin's
`SoliplexClient` and its free functions in
`native-plugins/soliplex/lib/soliplex_tools.dart`:

- `_getSoliplexUrl` — `http.get(.../api/config)` (`soliplex_tools.dart:21`)
- `_getTokenEndpoint` — `http.get(.../.well-known/openid-configuration)` (`soliplex_tools.dart:36`)
- `_tryRefreshToken` — `http.post(tokenEndpoint, ...)` (token refresh, sensitive) (`soliplex_tools.dart:57`)
- `getAuthSystems` — `http.get(.../api/login)` (`soliplex_tools.dart:119`)
- `SoliplexClient.listRooms` — `http.get(.../api/v1/rooms, headers)` (`soliplex_tools.dart:165`)
- `SoliplexClient.queryRoom`:
  - thread create `http.post(.../agui)` (`soliplex_tools.dart:197`)
  - SSE run: `http.Client()` + `http.Request('POST', sseUrl)` + `client.send(request)` then `streamedResp.stream.bytesToString()` (`soliplex_tools.dart:239`–`263`)
- The `Authorization: Bearer <token>` header is attached in `_getHeaders` (`soliplex_tools.dart:147`–`159`) and again directly on the SSE request (`soliplex_tools.dart:245`–`246`).

Key structural facts that drive the design:

- These calls use the **top-level `http` functions** (`http.get`, `http.post`)
  and a locally-constructed `http.Client()` — there is **no injectable
  client** today (`soliplex_tools.dart:2`, `:239`).
- `SoliplexClient` is constructed fresh per tool call inside the plugin:
  `SoliplexClient().listRooms()` (`plugin.dart:68`) and
  `SoliplexClient().queryRoom(...)` (`plugin.dart:86`).
- The plugin lives in an **external-ish package**: `pubspec.yaml` pulls
  `klangk_plugin_api` from a git URL
  (`native-plugins/soliplex/pubspec.yaml:26`–`28`), and the frontend likewise
  consumes both `klangk_plugin_api` (git, `src/frontend/pubspec.yaml:38`–`40`)
  and `klangk_plugins` (the aggregator at
  `native-plugins/aggregator`, via `pubspec_overrides.yaml`).
- The plugin contract is tiny: `ToolPlugin` exposes only `handlers`,
  `buildOverlay`, `dispose`
  (`klangk_plugin_api` `lib/src/tool_plugin.dart:8`–`18`). The
  `ToolPluginRegistry` is a **singleton** (`factory ToolPluginRegistry()
  => _instance`, `tool_plugin.dart:21`–`24`) shared by `BrowserDelegate`
  and `WorkspacePage`.
- The app's existing observability is the WS debug log: `WsClient` emits
  `WsDebugEntry` on a broadcast `debugLog` stream
  (`ws_client.dart:9`–`20`, `:56`, `:71`), and `DebugPanel` subscribes,
  buffers (max 500), renders a SEND/RECV list, and shows a JSON dialog on row
  tap (`debug_panel.dart:17`–`205`). `DebugPanel` is mounted as the `debug:`
  slot of `IdeLayout` in `WorkspacePage`
  (`workspace_page.dart:251`; pane wiring at `ide_layout.dart:128`–`161`).

**The central problem:** Soliplex traffic is generated *inside the plugin
package*, but the inspector UI (`DebugPanel`) lives *in the app*
(`src/frontend`). The plugin's HTTP calls never pass through
`BrowserDelegate._handleFetch` (that path is only for the built-in `fetch`
action, `browser_delegate.dart:60`–`102`) — the plugin calls `http` directly.
So the app cannot observe plugin traffic unless the plugin **publishes** it
across the package boundary.

---

## 2. Capture layer: options and recommendation

### Option A — Wrap `http.Client` inside `SoliplexClient` (recording interceptor)

Replace the top-level `http.get/post` and bare `http.Client()` with a single
injectable, recording client. A `BaseClient` subclass intercepts every
`send()`, captures request (method/url/headers/body) and response
(status/headers/body/duration), and pushes a record to a sink.

- Pros: captures **exactly** the Soliplex traffic and nothing else; sees the
  real headers including the bearer token actually sent; sees the SSE response
  stream where it is consumed; works identically on native and web (the `http`
  package's default client is platform-correct on both).
- Cons: requires editing the plugin package and threading a sink across the
  package boundary; SSE is currently read as one `bytesToString()` blob
  (`soliplex_tools.dart:263`), so per-event streaming requires a small parse
  hook, not just a client wrapper.

### Option B — Capture at the `BrowserDelegate` / bridge layer

Record traffic in `BrowserDelegate`.

- Reality check: **this does not work for Soliplex.** The bridge's only HTTP
  path is the built-in `fetch` action (`browser_delegate.dart:41`,
  `:60`–`102`). Soliplex tools arrive as `soliplex_list_rooms` /
  `soliplex_query` actions and are dispatched to the plugin
  (`browser_delegate.dart:43`–`49`), which then makes its **own** `http`
  calls. The delegate sees only the final string result, never the underlying
  HTTP request/response/SSE. So B can record "a soliplex tool ran and returned
  X" but cannot surface method/url/status/headers/SSE — i.e., it cannot meet
  the requirement.

### Recommendation — **Option A**, via an event sink exposed by the plugin

Capture inside `SoliplexClient` with a recording `http.BaseClient`, and expose
the captured events through a **sink defined in `klangk_plugin_api`** so the
app can observe them without importing plugin internals. This keeps the plugin
package self-contained while giving the app a typed, stable channel.

Concretely:

1. Add a tiny, dependency-light **observability contract** to
   `klangk_plugin_api` (the package both sides already depend on):
   - `SoliplexNetEvent` data model (see §3).
   - A broadcast event bus, e.g. `class PluginNetworkObserver { Stream<SoliplexNetEvent> get events; void emit(SoliplexNetEvent e); }`
     with a shared default instance. Putting it in the shared API package is
     what lets plugin (emitter) and app (consumer) meet without a direct
     dependency from app→plugin internals.
2. In the soliplex package, add a `RecordingClient extends http.BaseClient`
   that wraps an inner `http.Client`, times each `send`, buffers the response
   stream so it can record the body *and* re-emit it downstream (so callers
   still get their bytes), and calls `observer.emit(...)`.
3. Make `SoliplexClient` use that client everywhere:
   - Add an optional `http.Client? client` constructor param
     (`SoliplexClient({http.Client? client})`), defaulting to the recording
     client. Today it is `SoliplexClient()` (`soliplex_tools.dart:144`–`145`).
   - Replace top-level `http.get/post` calls in the free functions
     (`_getSoliplexUrl`, `_getTokenEndpoint`, `_tryRefreshToken`,
     `getAuthSystems`) with calls through a module-level recording client so
     auth/config traffic is also captured. (These are free functions, so give
     the module a shared recording client instance.)
   - Replace the bare `http.Client()` in `queryRoom`
     (`soliplex_tools.dart:239`) with the recording client.
4. SSE: keep the request/response captured by the wrapper, and additionally
   feed `_extractTextFromSseResponse` (or a streamed variant) so each parsed
   SSE event (`TEXT_MESSAGE_CONTENT` and others) is appended to the entry's
   `sseEvents` list (`soliplex_tools.dart:273`–`290`). Minimal change: emit one
   `SoliplexNetEvent` for the SSE request and attach the parsed event list when
   the stream completes.

Boundary summary: **plugin emits → shared API bus → app consumes**. The app
never imports `soliplex_tools.dart`; it depends only on
`klangk_plugin_api`.

---

## 3. Data model

Defined in `klangk_plugin_api` so both sides share one type.

```dart
class SoliplexNetEvent {
  final String id;              // correlation id (uuid or counter)
  final DateTime startedAt;
  final String method;          // GET / POST
  final String url;             // full request URL
  final Map<String, String> requestHeaders;
  final String? requestBody;
  final int? statusCode;        // null until response / on transport error
  final Map<String, String>? responseHeaders;
  final String? responseBody;   // omitted/elided for large SSE blobs
  final Duration? duration;
  final List<SseEvent> sseEvents; // parsed SSE events (empty for non-SSE)
  final String? error;          // transport/exception text, if any
  final bool isSse;             // text/event-stream
}

class SseEvent {
  final DateTime at;
  final String type;            // e.g. TEXT_MESSAGE_CONTENT
  final String raw;             // the `data:` payload line
  final Map<String, dynamic>? json; // parsed if JSON
}
```

Notes:
- `requestHeaders`/`responseHeaders` are captured **raw** at the boundary;
  redaction happens only at render time (§5), so a developer can opt to reveal.
- Cap `responseBody` and `requestBody` lengths (e.g. 64 KB) with an
  "elided N bytes" marker, mirroring `DebugPanel`'s 200-char preview behavior
  (`debug_panel.dart:123`–`124`) but larger.

---

## 4. UI

Mirror `DebugPanel` exactly so it feels native. Two viable shapes:

### Recommended: tabbed `DebugPanel` (WebSocket | Network)

Add a `TabBar`-free, lightweight two-tab toolbar to the existing
`DebugPanel` (it already has a 22px toolbar row at `debug_panel.dart:59`–`92`).
Keep tab 1 as the current WS log; add tab 2 "Network (n)" that lists
`SoliplexNetEvent`s.

- Reuse the same dark palette and `monospace` styling
  (`debug_panel.dart:55`, `:138`–`168`), the auto-scroll toggle and clear
  button (`debug_panel.dart:73`–`90`), and the 500-entry cap
  (`debug_panel.dart:22`, `:30`–`33`).
- Row layout for a network entry: `time | METHOD | status | url(elided)`,
  colored by status (2xx green `0xFFB5BD68`, 4xx/5xx red, pending grey),
  reusing the existing color constants.
- Tap a row → detail dialog (same `showDialog` pattern as
  `_showFullMessage`, `debug_panel.dart:177`–`204`) with collapsible sections:
  General (method/url/status/duration), Request Headers, Request Body,
  Response Headers, Response Body, and — when `isSse` — an **SSE stream**
  section listing each `SseEvent` (timestamp, type, payload).
- Data source: subscribe to the shared `PluginNetworkObserver.events` stream in
  `initState`, buffering into a `List<SoliplexNetEvent>`, identical to how the
  WS tab subscribes to `widget.wsClient.debugLog`
  (`debug_panel.dart:27`–`43`). Pass the observer into `DebugPanel` as a
  constructor param (defaulting to the shared instance) so it stays testable,
  matching how `wsClient` is injected (`debug_panel.dart:9`–`11`).

### Alternative: separate `SoliplexNetworkPanel`

A new widget mounted alongside `DebugPanel`. Cleaner separation but needs new
layout space in `IdeLayout` (only one `debug` slot exists today,
`ide_layout.dart:13`, `:128`–`161`) or a toggle. The tabbed approach reuses the
existing pane and is less invasive — **prefer tabs**.

---

## 5. Security / redaction (required)

Bearer tokens flow through `Authorization` headers
(`soliplex_tools.dart:154`, `:246`) and the token-refresh POST body carries
`refresh_token` (`soliplex_tools.dart:60`–`64`). The backing IdP is org
infrastructure, so:

- **Redact by default at render time.** The capture stores raw values, but the
  UI masks:
  - `Authorization` header → `Bearer ****` (show scheme + last 4 chars max,
    e.g. `Bearer …a1b2`), default fully masked.
  - Any header whose name matches `authorization`, `cookie`, `set-cookie`,
    `proxy-authorization`, `x-api-key` (case-insensitive) → masked.
  - Request/response bodies: redact JSON fields named `access_token`,
    `refresh_token`, `id_token`, `client_secret`, `code` (the token endpoint
    request/response, `soliplex_tools.dart:57`–`81`).
- **Opt-in reveal.** A per-entry "Reveal secrets" toggle (eye icon) in the
  detail dialog, plus a panel-level "Mask tokens" default-on switch in the
  toolbar. Revealing is a deliberate, momentary action; never persisted.
- **No clipboard of secrets by default.** "Copy as cURL"/"Copy entry" emits the
  redacted form unless reveal is active.
- Do not log raw headers to console or persist entries to disk.
- Do not hardcode server names; the only literal that may appear is
  `rag.enfoldsystems.net` where it already exists in code/config. The inspector
  reads URLs from captured events, not from constants.

Implementation detail: keep redaction in a single pure helper
(`redactHeaders`, `redactBody`) in the app's debug layer so it is unit-testable
and the raw model stays clean.

---

## 6. Cross-platform (native macOS/Linux + web)

- The `http` package's default client is correct on both native and web; the
  recording `BaseClient` only wraps `send()`, so it is platform-agnostic.
- The plugin is already written to be platform-agnostic (no
  `dart:js_interop`/`package:web` in `plugin.dart`; web concerns live behind
  `soliplex_platform.dart`, per the Phase 4 guardrail comment,
  `plugin.dart:10`–`13`). Keep the observer free of platform imports too —
  it is plain Dart streams.
- SSE on web: the response is still consumed via `streamedResp.stream`
  (`soliplex_tools.dart:250`–`263`); the wrapper buffers/re-emits identically.
  No platform branch needed.
- UI is pure Flutter widgets (same as `DebugPanel`), so no platform work.

---

## 7. Ordered step list (files to add / edit)

In `klangk_plugin_api` (shared package — the boundary):
1. **Add** `lib/src/network_observer.dart`: `SoliplexNetEvent`, `SseEvent`,
   `PluginNetworkObserver` (broadcast stream + shared instance + `emit`).
2. **Edit** `lib/klangk_plugin_api.dart`: `export 'src/network_observer.dart';`
   (currently exports `tool_plugin.dart` + `backend_url.dart`).

In `native-plugins/soliplex`:
3. **Add** `lib/soliplex_recording_client.dart`: `RecordingClient extends
   http.BaseClient` that times, buffers, re-emits, and `emit`s a
   `SoliplexNetEvent`.
4. **Edit** `lib/soliplex_tools.dart`:
   - module-level recording client for the free functions
     (`_getSoliplexUrl` `:21`, `_getTokenEndpoint` `:36`, `_tryRefreshToken`
     `:57`, `getAuthSystems` `:119`).
   - `SoliplexClient({http.Client? client})` ctor (`:144`); use it in
     `listRooms` (`:165`) and `queryRoom`'s thread POST (`:197`) and SSE
     `client` (`:239`).
   - feed parsed SSE events into the entry's `sseEvents`
     (around `_extractTextFromSseResponse`, `:273`–`290`).

In `src/frontend`:
5. **Add** `lib/debug/network_redaction.dart`: `redactHeaders`, `redactBody`
   pure helpers (unit-testable).
6. **Edit** `lib/debug/debug_panel.dart`: add the "Network" tab, the
   `SoliplexNetEvent` buffer + subscription to `PluginNetworkObserver.events`,
   row widget, detail dialog with header/body/SSE sections and reveal toggle.
   Optionally add a `PluginNetworkObserver? observer` ctor param defaulting to
   the shared instance.
7. **Edit** `lib/workspace/workspace_page.dart`: only if `DebugPanel` ctor
   gains the observer param — pass the shared instance (`workspace_page.dart:251`).
   No `IdeLayout` change needed if using tabs.

No backend (`src/backend`) changes are required.

---

## 8. Testing approach

- **Recording client (soliplex package):** unit test with
  `package:http/testing.dart` `MockClient` as the inner client (the existing
  `browser_delegate_test.dart:4`–`5` already uses `http/testing`). Assert that
  a GET/POST produces a `SoliplexNetEvent` with correct method/url/status/
  duration and that the caller still receives the original body (re-emit
  works).
- **SSE parsing:** feed a canned `text/event-stream` body with several
  `data: {...}` lines (mirroring `_extractTextFromSseResponse`,
  `soliplex_tools.dart:273`–`290`) and assert `sseEvents` contains the parsed
  `TEXT_MESSAGE_CONTENT` events and the aggregate text is still returned.
- **Redaction (frontend):** pure-function tests for `redactHeaders` /
  `redactBody` — `Authorization: Bearer xyz` → masked; token-endpoint body
  fields elided; non-sensitive headers untouched; reveal path returns raw.
- **DebugPanel widget test:** add `debug_panel_test.dart` (none exists today —
  `test/` lists no debug test). Pump `DebugPanel` with a fake observer, emit a
  `SoliplexNetEvent`, switch to the Network tab, assert the row renders the
  masked URL/status, tap it, assert the dialog shows redacted headers and the
  SSE section, toggle reveal, assert raw appears. Follow the injection style of
  `browser_delegate_test.dart` (fakes) and existing widget tests
  (`tool_plugin_test.dart`, `workspace_page_test.dart`).
- **Static analysis:** run `dart analyze` (and `dart fix --apply`) in both the
  soliplex package and the frontend before committing.
- **Manual:** run the app, authenticate via the existing "Connect to Soliplex"
  overlay (`plugin.dart:42`–`48`), trigger `soliplex_list_rooms` /
  `soliplex_query`, open the Network tab, confirm entries appear with tokens
  masked and SSE events listed; verify on both macOS native and web builds.
