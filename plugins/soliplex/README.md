# klangk_plugin_soliplex

Soliplex knowledge-base plugin for [Klangk](https://github.com/mcdonc/klangk),
native + web. Bridges the agent tools `soliplex_list_rooms` and `soliplex_query`
to a Soliplex RAG server, with an in-app auth overlay (PKCE login on native via
`flutter_appauth`, popup on web).

This package is **vendored into the Klangk monorepo** at `plugins/soliplex/`
(snapshot from [`soliplex/klangk-plugin-soliplex`](https://github.com/soliplex/klangk-plugin-soliplex) `v0.4`, [#1686](https://github.com/mcdonc/klangk/issues/1686));
upstream remains maintained by the Soliplex org. It is written against
**Klangk `main`** — no fork overrides.

## Streaming queries (no more 30 s timeout)

Long RAG + LLM answers used to exceed the browser-delegate bridge's fixed 30 s
round-trip, producing an HTTP 502 that the Pi extension mislabeled as an
expired-token retry loop. The fix streams deltas end-to-end
([mcdonc/klangk#82](https://github.com/mcdonc/klangk/issues/82)):

| Layer                                                                     | Where                                | Status |
| ------------------------------------------------------------------------- | ------------------------------------ | ------ |
| Backend `/api/browser-delegate/stream` + per-chunk idle timeout           | klangk `main` (#79)                  | merged |
| Plugin-agnostic `BrowserDelegate` / `ws_client.sendBrowserChunk` relay    | klangk `main` (#91)                  | merged |
| `StreamingToolHandler` / `ToolChunkSink` / `ToolPlugin.streamingHandlers` | `klangk_plugin_api` (#1)             | merged |
| **soliplex `soliplex_query` delta emission**                              | **this package** (was klangk PR #80) | here   |

This package supplies the last row. `SoliplexPlugin` exposes:

```dart
@override
Map<String, StreamingToolHandler> get streamingHandlers => {
      'soliplex_query': _queryStream,
    };
```

`queryRoom` takes an optional `onChunk` sink, fed from the `soliplex_client`
`AgUiStreamClient` text deltas. When the bridge dispatches with `stream: true`,
each delta is relayed as a `browser_chunk` immediately, keeping the socket
alive; a terminal `browser_response` carries the final answer. The non-streaming
`_query` handler is retained for callers that don't pass an `onChunk`.

## Dependencies

- `klangk_plugin_api` — git, `mcdonc/klangk-plugin-api` (HEAD includes the
  streaming API from #1).
- `soliplex_client` — git, `soliplex/frontend`, path `packages/soliplex_client`.

Both are git deps so the package resolves on any machine/CI.

## Consuming from the Klangk app

This follows Klangk `main`'s plugin convention: a plugin repo holds its Flutter
package under `klangk/`. Add an entry to your `plugins.yaml`
(`$KLANGK_PLUGINS_DIR/plugins.yaml`):

```yaml
plugins:
  - name: soliplex
    git: https://github.com/soliplex/klangk-plugin-soliplex.git
    ref: main
```

then regenerate the aggregator and rebuild:

```bash
update-plugins                       # scripts/update_plugins.py — vendors the repo
python3 scripts/import_dart_plugins.py   # scans <name>/klangk/, writes createAllPlugins()
cd src/frontend && flutter pub get && flutter build
```

`import_dart_plugins.py` finds `soliplex/klangk/lib/plugin.dart`, sees
`class SoliplexPlugin extends ToolPlugin`, and emits it into `createAllPlugins()`.
No `pubspec_overrides` for `klangk_plugin_api` is needed — the streaming API is
upstream as of klangk `main`.

## Layout

```text
klangk/                           # Flutter package (main's plugin convention)
  pubspec.yaml
  lib/
    klangk_plugin_soliplex.dart   # barrel: exports SoliplexPlugin
    plugin.dart                   # ToolPlugin: handlers + streamingHandlers + auth overlay
    soliplex_tools.dart           # SoliplexClient: listRooms / queryRoom(onChunk)
    soliplex_auth_result.dart
    soliplex_platform.dart        # conditional export (native/web)
    soliplex_platform_native.dart # shared_preferences + flutter_appauth
    soliplex_platform_web.dart    # localStorage + popup
```
