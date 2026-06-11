# Browser Bridge

Pi extensions inside the container can delegate actions to the browser via the backend bridge endpoint.

## How It Works

Extensions POST to `http://host.containers.internal:<nginx_port>/api/browser-delegate` with a bridge token. Each terminal exec session gets its own per-connection bridge token injected via the podman exec environment (`KLANGK_BRIDGE_TOKEN`), overriding any container-level env. The backend resolves the bridge token to the specific browser connection that owns the terminal and relays the request over the existing WebSocket. Only a response from that specific connection is accepted (preventing spoofing from other tabs or users sharing the same workspace).

## Flow

```text
LLM calls tool → Pi extension execute()
  → HTTP POST to /api/browser-delegate {action, token, ...}
  → Backend resolves token → (workspace_id, target_connection)
  → WebSocket message to target only: {"type":"browser_request","id":"...","action":"..."}
  → Flutter BrowserDelegate handles action (fetch, celebrate, etc.)
  → WebSocket message: {"cmd":"browser_response","id":"...","data":"..."}
  → Backend verifies sender matches target, returns HTTP response to extension
  → Extension returns result to LLM
```

Built-in actions: `fetch` (HTTP request with browser cookies). All other actions are dispatched to the `ToolPluginRegistry` which routes to Dart plugin handlers registered by `klangk/` subdirectories.

## Bridge Token

The bridge token (`KLANGK_BRIDGE_TOKEN`) is a per-connection UUID, created when a terminal session starts, revoked on connection cleanup or terminal restart. It routes requests to the specific browser tab that owns the terminal.

The `@klangk/bridge` npm package provides `browserFetch()`, `browserAction()`, and `isBridgeAvailable()` helpers for extension authors.

## Current Client-Side Tools

- **celebrate** (`plugins/celebrate/`): Triggers confetti animation in the browser
- **beep** (`plugins/beep/`): Plays a beep sound in the browser
- **bobdobbs** (`plugins/bobdobbs/`): Bob "J.R." Dobbs quote generator
