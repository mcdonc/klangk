# Bark — Multi-User Web Coding Agent

## Overview

Bark is a multi-user web app that gives each user their own isolated Pi coding agent (pi.dev) running in a Docker container. Users authenticate with a simple login, can create multiple named workspaces, and interact with Pi through a split-pane UI with a chat interface, file viewer, and debug panel.

## Architecture

```
nginx reverse proxy (port 8995)
    ├── /bark/     → Bark backend (port 8997)
    └── /          → Soliplex backend (port 8555)

Browser (Flutter Web + Chat UI + AG-UI)
    ├── AG-UI events over WebSocket (authenticated)
    ├── Extension UI responses (client-side tool results)
    └── Direct Soliplex API calls (same domain, browser cookies)
Python/FastAPI backend (port 8997, serves API + frontend static files)
    ├── Auth (JWT sessions, SQLite user store)
    ├── Workspace registry (user → [workspace] → container)
    ├── Pi-to-AG-UI translator (Pi RPC events → AG-UI events)
    ├── Extension UI request/response forwarding
    ├── Message history (SQLite)
    ↕ docker attach subprocess
Pi container per workspace (stdin/stdout JSON-RPC)
    ├── Pi extensions (from plugins/*/extension.ts)
    ├── Server-side tools (from plugins/*/tools/)
    ├── AGENTS.md (dynamically generated on container start)
    ↕ bind mount
$DEVENV_STATE/.bark/workspaces/<user-id>/<workspace-name>/
```

### Components

- **Backend** (`backend/`): Python/FastAPI — single-port server for API, WebSocket, and frontend static files
- **Frontend** (`frontend/`): Flutter Web — chat with markdown rendering, syntax-highlighted code blocks, file viewer, debug panel
- **Docker** (`docker/`): Custom Dockerfile for Pi agent containers with Python3, Node.js, Dart, Flutter, Rust, build-essential, Pi extensions

### Key Technologies

- **AG-UI Protocol**: Standardized agent-user interaction protocol for event streaming
- **Pi Coding Agent**: Minimal terminal coding harness (pi.dev) running in RPC mode with native session persistence and extension tools
- **Ollama**: LLM provider — supports both Ollama Cloud and self-hosted instances, configurable via env vars (`OLLAMA_BASE_URL`, `OLLAMA_MODEL`, `OLLAMA_API_KEY`)
- **devenv**: Nix-based development environment with auto-setup, conditional build tasks (`execIfModified`), auto-reload disabled

## Project Structure

```
bark/
  devenv.nix                    # Dev environment: Python (uv), Flutter, Docker CLI, conditional build tasks
  devenv.yaml                   # devenv inputs, reload: false
  .envrc                        # direnv integration
  .env                          # Secrets: OLLAMA_API_KEY, OLLAMA_BASE_URL, OLLAMA_MODEL, BARK_JWT_SECRET, etc.
  .gitignore
  README.md
  PLAN.md
  synctoarctor.sh               # Deploy script for arctor.repoze.org
  bootstrap                     # Install Nix + devenv

  docker/
    Dockerfile                  # Pi agent image: node:22-slim + Pi + Python3 + Dart + Flutter + Rust + build-essential
    entrypoint.sh               # Generates models.json, settings.json, AGENTS.md; starts Pi in RPC mode
    models.json                 # Generated at startup from env vars
    settings.json               # Generated at startup from env vars
    extensions/                 # Pi extensions (TypeScript, registered as first-class LLM tools)
      word-count.ts             # Fast file stats: lines, words, characters, size
      pig-latin.ts              # Text to Pig Latin converter
      celebrate.ts              # Triggers confetti animation in the browser
      beep.ts                   # Plays an audible beep tone via Web Audio API
    tools/                      # Python helper scripts called by extensions
      word_count.py             # Backend for word-count extension

  backend/
    pyproject.toml              # Python deps: fastapi, aiodocker, aiosqlite, bcrypt, python-jose
    backend/
      main.py                   # FastAPI app, lifespan, routes, default user seeding, static file serving
      auth.py                   # Register/login/logout, JWT, bcrypt password hashing
      user_store.py             # SQLite: users, workspaces, token blocklist, message history
      workspace_manager.py      # Workspace CRUD + host directory management
      container_manager.py      # Docker lifecycle, port allocation, idle timeout, shutdown cleanup
      pi_rpc_client.py          # docker attach subprocess for Pi stdin/stdout JSON-RPC
      agui_translator.py        # Pi RPC events → AG-UI events mapping, file-change detection
      ws_handler.py             # WebSocket auth, workspace routing, AG-UI streaming, auto-restart
      file_service.py           # Host-side file read/write with path traversal protection

  frontend/
    pubspec.yaml                # Flutter deps: flutter_markdown, flutter_highlight, go_router, etc.
    web/index.html              # HTML shell with Google Fonts, service worker cleanup
    lib/
      main.dart                 # App entry with Provider setup
      app.dart                  # MaterialApp, GoRouter (auth-aware, URL-preserving via hash)
      utils/
        page_title.dart         # Browser tab title updates
        backend_url.dart        # Derives API base URL from <base href> for subpath hosting
      widgets/
        bark_logo.dart          # Bark logo widget (orange paw icon)
        confetti.dart           # Confetti animation overlay for celebrate tool
        beep.dart               # Web Audio API beep via dart:js_interop
      auth/
        auth_service.dart       # JWT storage, login/register/logout, async init
        login_page.dart         # Login/register form
      workspace/
        workspace_list_page.dart  # Workspace CRUD UI
        workspace_page.dart     # IDE view: WebSocket, container lifecycle, ui_ready handshake
      agui/
        agui_client.dart        # WebSocket client, AG-UI event stream, ui_ready command
        agui_events.dart        # AG-UI event type definitions
      terminal/
        chat_panel.dart         # Chat UI: markdown, syntax highlighting, tool cards, history loading
      file_viewer/
        file_viewer_panel.dart  # File tree + content viewer (16pt JetBrains Mono)
        file_upload.dart        # Drag-and-drop upload
      output/
        output_panel.dart       # Debug panel: container lifecycle, queries, tool calls, errors
      layout/
        ide_layout.dart         # Resizable 3-pane split layout with 3D dividers
```

## Features

### Authentication
- Username/password with bcrypt hashing
- JWT tokens (24hr expiry, secret configurable via BARK_JWT_SECRET) with token blocklist for logout
- Default user auto-seeded on startup (configurable via BARK_DEFAULT_USER/PASSWORD in .env)
- Session persists across page reloads (async token loading before routing)

### Workspaces
- Multiple workspaces per user
- Each workspace gets its own Docker container + bind-mounted directory
- URL-based workspace routing (survives page reload via hash URL reading)
- Workspace name shown in app bar and browser tab title
- Containers stop when navigating away (browser back, in-app back, logout)
- Containers auto-restart transparently when user sends next prompt
- Container lifecycle visible in debug panel

### Pi Agent Integration
- One Docker container per workspace running Pi in RPC mode
- Container communicates via stdin/stdout JSON-RPC (docker attach subprocess)
- Pi RPC events translated to AG-UI events in real-time
- Native Pi session persistence (JSONL files in workspace `.pi/sessions/`)
- Session resume on reconnect via `switch_session` RPC command
- 5 TCP ports allocated per workspace (9000-9004, 9005-9009, etc.) for user apps
- API keys and LLM config passed via environment variables
- 15-minute idle timeout with automatic container stop and debug notification
- All user containers stopped on logout and backend shutdown

### Pi Extensions (Server-Side Tools)
- Extensions are TypeScript files in `docker/extensions/` — registered as first-class LLM tools
- The LLM sees them in its tool list alongside built-in tools (read, write, edit, bash)
- Extensions can call Python scripts, run shell commands, or execute pure TypeScript logic
- AGENTS.md is generated dynamically on each container start, listing all registered extension tools
- Current extensions:
  - `word_count` — fast file stats (lines, words, characters, size) via Python script
  - `pig_latin` — text to Pig Latin converter (pure TypeScript)
  - `celebrate` — triggers confetti animation in the browser (frontend detects tool call)
  - `beep` — plays an audible beep tone via Web Audio API (frontend detects tool call)

### Chat Interface
- Markdown rendering for assistant responses (flutter_markdown)
- Syntax-highlighted code blocks (Monokai Sublime theme, highlight.dart, JetBrains Mono)
- Collapsible tool call cards showing arguments and results
- Streaming indicator while agent is thinking
- Enter to send, Shift+Enter for newline
- Abort button (red when agent running)
- Conversation history persisted to SQLite and restored on workspace reload
- Input history navigation (up/down arrow keys cycle through previous prompts)
- Queued messages shown dimmed with "queued" label, persisted in SQLite
- Persistent error snackbars with close button

### File Viewer
- Directory tree with file sizes
- Click to view file contents (16pt JetBrains Mono, left-aligned)
- Auto-refresh when Pi writes/edits files or runs file-creating/deleting bash commands
- Drag-and-drop file upload

### Debug Panel
- Container lifecycle events (starting, ready with port info and status, idle stop, restart)
- Session resume notifications
- Query text shown for each prompt sent
- Tool call entries from Pi (including extension tools)
- Error entries
- Timestamps and color-coded entries
- Clear button

### UI/Theme
- Harvest-inspired light theme (warm off-white, green accents, medium gray header)
- Orange Bark logo (paw icon + "Bark" text)
- 3D edges on all dividers, panel headers, and borders
- Three panes with subtly different background shades
- Dark blue back/logout buttons
- Browser tab title updates per page ("Bark - Login", "Bark - Workspaces", "Bark - workspace-name")
- Resizable split panes with drag handles (70/30 default for files/debug)

### Hosting
- Single-port architecture: FastAPI serves both API and Flutter frontend static files
- Subpath hosting behind nginx (e.g., `/bark/`) via `sub_filter` for `<base href>` rewriting
- Frontend derives API URLs from `<base href>` — works on both root and subpath
- WebSocket proxying via nginx `proxyWebsockets`
- Deployment via `synctoarctor.sh` rsync script

## Development

### Prerequisites
- Nix with devenv installed (run `./bootstrap` to install both)
- Docker daemon running
- Ollama — either a Cloud account with API key, or a self-hosted instance

### Setup & Run
```bash
# Create .env
cat > .env << 'EOF'
OLLAMA_API_KEY=your-api-key-here
OLLAMA_BASE_URL=https://ollama.com/v1       # or http://localhost:11434/v1 for self-hosted
OLLAMA_MODEL=gemma4:31b                     # any model available on your Ollama instance
BARK_JWT_SECRET=change-this-to-a-random-secret
BARK_DEFAULT_USER=admin
BARK_DEFAULT_PASSWORD=admin
EOF

# Install Nix and devenv (if not already installed)
./bootstrap

# Start the app
devenv processes up

# Open in browser
open http://localhost:8997
```

### Ports
- `8997`: Web UI + API (single FastAPI/uvicorn server)
- `9000+`: User app ports (5 per workspace)

### Rebuild

To force rebuild the Docker image and Flutter web app:

```bash
devenv shell -- rebuild
```

Then restart the processes. On normal startup, Flutter and Docker builds run automatically when their source files have changed (via devenv `execIfModified` content hashing). Watched paths:
- **Flutter**: `frontend/lib`, `frontend/web`, `frontend/pubspec.yaml`, `frontend/pubspec.lock`, `plugins/**/*.dart`
- **Docker**: `docker/Dockerfile`, `docker/entrypoint.sh`, `plugins/**/*.ts`, `plugins/**/tools/**`

### Plugin System

All tools live in `plugins/<name>/` directories. A plugin can contain:

- `extension.ts` — Pi extension with `pi.registerTool()`. Copied to `docker/extensions/` at build time.
- `plugin.dart` — Dart class extending `ToolPlugin` for client-side action handling. Must export a class with `extends ToolPlugin`.
- `*.dart` — Supporting Dart files (widgets, utilities). All `.dart` files are copied alongside `plugin.dart`.
- `tools/` — Server-side scripts. Everything in this subdirectory is copied to `/usr/local/bin/bark-tools/` in the Docker image.

A plugin needs at minimum an `extension.ts`. The `plugin.dart` is only needed for client-side tools that delegate execution to the browser via `ctx.ui.input("HOST_TOOL_REQUEST", ...)`.

**Build integration:**
- `scripts/gen_plugins.py` scans `plugins/*/plugin.dart`, copies `.dart` files into `frontend/lib/tools/plugins/`, and generates `plugins_generated.dart`
- `dockerbuild` collects `extension.ts` and `tools/` files from all plugins into the Docker build context
- `flutterbuildweb` runs the codegen before compiling
- Both are triggered automatically by `devenv up` via `execIfModified`

**Adding a plugin:**
1. Create `plugins/<name>/extension.ts` with `pi.registerTool()` 
2. For client-side tools, add `plugin.dart` extending `ToolPlugin` with action handlers
3. For server-side scripts, add files in `plugins/<name>/tools/`
4. `devenv up` rebuilds automatically when `plugins/` changes

**Built-in vs external plugins:**
- `celebrate` and `beep` live in `builtin-plugins/` (committed to the Bark repo)
- External plugins live in `plugins/` (gitignored, populated by `update-plugins`)
- The build system merges both directories — they follow the same structure and conventions
- All other plugins (soliplex, word-count, pig-latin, etc.) are external

**External plugin management (TODO):**

External plugins are declared in `plugins/plugins.yaml`:

```yaml
plugins:
  - git: https://github.com/mcdonc/bark-plugins
    path: soliplex
    ref: v1.2.0
  - git: https://github.com/mcdonc/bark-plugins
    path: word-count
    ref: main
  - git: https://github.com/someone/bark-plugin-custom
    ref: v2.0.0
```

The entire `plugins/` directory is gitignored.

- `update-plugins` — explicit devenv script:
  - If `plugins/` doesn't exist, creates it with a template `plugins.yaml`
  - If `plugins/plugins.yaml` exists, fetches listed plugins into `plugins/`, resolves git refs to commit SHAs, and writes `plugins/plugins.lock`
- `plugins/plugins.lock` — records resolved commit SHAs for reproducible builds
- On first `devenv up`, if `plugins/plugins.yaml` exists but no lockfile is found, `update-plugins` runs automatically. After that, updates are explicit only.
- Local plugin development: drop a directory into `plugins/` directly — the build system treats it the same as a fetched plugin.
- Built-in plugins in `builtin-plugins/` are always present and don't need entries in `plugins.yaml`.
- Deployments that want to share plugin config across a team can check in `plugins/plugins.yaml` and `plugins/plugins.lock` by adjusting `.gitignore`.
- `execIfModified` watches `plugins/plugins.lock` to trigger rebuilds when plugin versions change.

### Data
- All data stored in `$DEVENV_STATE/.bark/`
- SQLite database: `bark.db` (users, workspaces, messages, token blocklist)
- Workspace files: `workspaces/<user-id>/<workspace-name>/`
- Pi sessions: `workspaces/<user-id>/<workspace-name>/.pi/sessions/`
- Database persists across restarts and rebuilds

## Client-Side Tool Delegation via Extension UI Sub-Protocol

Pi's RPC mode does **not** support host tools (`set_host_tools` returns "Unknown command"). Instead, we use Pi's **Extension UI Sub-Protocol** to delegate tool execution to the browser.

### How it works

Pi extensions can call `ctx.ui.input(title, placeholder)` from within a tool's `execute` method. In RPC mode, this emits an `extension_ui_request` event on stdout and blocks until an `extension_ui_response` comes back on stdin. We use this as a general-purpose request/response channel between the container and the browser.

**Convention**: Extensions use `ctx.ui.input("HOST_TOOL_REQUEST", jsonPayload)` where the payload encodes the action to perform. The frontend parses the JSON, executes the action, and sends the result back.

### Flow

```
LLM calls tool → Pi extension execute()
  → ctx.ui.input("HOST_TOOL_REQUEST", '{"action":"...", ...}')
  → Pi emits: {"type":"extension_ui_request","id":"...","method":"input","title":"HOST_TOOL_REQUEST","placeholder":"..."}
  → Backend forwards to frontend via WebSocket
  → Frontend executes action (browser-side, with auth cookies)
  → Frontend sends: {"cmd":"extension_ui_response","id":"...","value":"result"}
  → Backend forwards to Pi stdin
  → Extension receives result, returns to LLM
```

### Current client-side tools

- **celebrate** (`docker/extensions/celebrate.ts`): Triggers confetti animation in the browser
- **beep** (`docker/extensions/beep.ts`): Plays a beep sound in the browser
- **soliplex_list_rooms** (`docker/extensions/soliplex.ts`): Lists available Soliplex knowledge base rooms
- **soliplex_query** (`docker/extensions/soliplex.ts`): Queries a Soliplex room via AG-UI (creates thread, posts question, collects SSE response). Default room: `search`

### Soliplex integration

The Soliplex tools run entirely in the browser, which has the user's Soliplex authentication cookies. When deployed behind nginx on the same domain, the browser can call Soliplex APIs directly with no CORS issues. The backend serves a `/api/config` endpoint that tells the frontend where Soliplex is located (via `SOLIPLEX_URL` env var).

The query flow: frontend creates a thread in the Soliplex room, posts the user's question as an AG-UI `RunAgentInput`, collects the streamed SSE response, extracts `TEXT_MESSAGE_CONTENT` deltas, and returns the assembled text to the Pi extension.

## TODO

- **Read-only root filesystem**: Use `--read-only` Docker flag to make the container's root filesystem unwritable. Only `/workspace` (bind mount) and necessary tmpfs mounts (`/tmp`, `/root/.pi`) should be writable. This prevents the agent from modifying system files or installing packages outside the workspace.
- **Container resource limits**: Add CPU/memory limits to containers to prevent runaway processes.
- **Container network isolation**: Restrict container network access to prevent use as an attack platform. Use a custom Docker network with limited egress — allow only the Ollama API endpoint (cloud or self-hosted) and block all other outbound traffic. Consider using `--network=none` with a proxy sidecar for allowlisted domains only.
- **Multiple LLM providers**: Support selecting different models per workspace.
- **Syntax highlighting language detection**: Improve code block language detection for unlabeled blocks.
- **Folder drag-and-drop upload**: Support dropping entire folders (with contents) into the file pane, preserving directory structure. Requires using the browser's File System Access API or `webkitGetAsEntry()` to traverse directory entries recursively.
- **Container terminal pane**: Add a terminal panel (xterm.dart) that gives the user direct shell access to the workspace container via `docker exec`. Would allow users to run commands, inspect processes, debug code, and interact with running servers without going through the AI agent.
- **Same-workspace multi-window**: Opening the same workspace in two browser windows simultaneously has undefined behavior — both WebSocket connections share one Pi container/session, and prompts from either window could collide or interleave unpredictably. Consider either locking a workspace to one connection at a time, or multiplexing both windows onto the same event stream.
- **Remove migration code**: The entrypoint.sh contains one-time migration logic (copying sessions from `/workspace/.pi/sessions` to the new bind mount, removing stale `AGENTS.md` and `.pi` from workspaces). Once all existing workspaces have been started at least once with the new container image, this migration code can be removed.
- **Workspace disk quotas**: Limit how much disk space each workspace can consume. Options: use filesystem quotas (XFS/ext4 project quotas on the host), overlay2 with size limits, or a loopback-mounted filesystem per workspace with a fixed size. Should also surface current disk usage in the UI (file viewer header or workspace list) so users can see how much space they've used.
- **Investigate running Pi under bubblewrap**: Explore using [bubblewrap](https://github.com/containers/bubblewrap) (bwrap) as an alternative to Docker for sandboxing Pi. Bubblewrap is lighter-weight than Docker — no daemon, no image builds, no container overhead — and provides namespace-based isolation (mount, PID, network, user). This could significantly reduce startup time and resource usage. Trade-offs: no pre-built image caching, need to manage tool installations on the host, less isolation than full container. Could be offered as an alternative backend alongside Docker.
