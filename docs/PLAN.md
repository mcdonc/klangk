# Bark — Multi-User Web Coding Agent

## Overview

Bark is a multi-user web app that gives each user their own isolated Pi coding agent (pi.dev) running in a Docker container. Users authenticate with a simple login, can create multiple named workspaces, and interact with Pi through a split-pane UI with a chat interface, file viewer, and debug panel.

## Architecture

```
Browser (Flutter Web + Chat UI + AG-UI)
    ├── AG-UI events over WebSocket (authenticated)
    ├── Extension UI responses (client-side tool results)
Python/FastAPI backend (port 8997, serves API + frontend static files)
    ├── Auth (JWT sessions, SQLite user store)
    ├── Workspace registry (user → [workspace] → container)
    ├── Pi-to-AG-UI translator (Pi RPC events → AG-UI events)
    ├── Extension UI request/response forwarding
    ├── Message history (SQLite)
    ↕ docker attach subprocess
Pi container per workspace (stdin/stdout JSON-RPC)
    ├── Pi extensions (from $BARK_PLUGINS_DIR/*/extension.ts)
    ├── Server-side tools (from $BARK_PLUGINS_DIR/*/tools/)
    ├── AGENTS.md (dynamically generated on container start)
    ↕ bind mount
$BARK_DATA_DIR/workspaces/<user-id>/data/<workspace-id>/
```

### Components

- **Backend** (`src/backend/`): Python/FastAPI — single-port server for API, WebSocket, and frontend static files
- **Frontend** (`src/frontend/`): Flutter Web — chat with markdown rendering, syntax-highlighted code blocks, file viewer, debug panel
- **Docker** (`src/dockerimage/`): Custom Dockerfile for Pi agent containers with Python3, Node.js, Dart, Flutter, Rust, build-essential, PostgreSQL, SQLite, vim, emacs, network tools, Pi extensions

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

  plugins/              # Starter plugins (source of truth, fetched into $BARK_PLUGINS_DIR by update-plugins)
    celebrate/                  # Confetti animation (client-side)
    beep/                       # Audible beep tone (client-side)
    pig-latin/                  # Text to Pig Latin converter (server-side)
    word-count/                 # Fast file stats (server-side)
  scripts/
    import_plugins.py              # Codegen: scans plugins, generates plugins_generated.dart
    update_plugins.py           # Fetches plugins from git repos, writes plugins.lock
    test_port_allocation.py    # Tests port allocation lifecycle: create, increase, decrease, delete
    flutterbuildweb.sh         # Flutter build: plugin auto-fetch, codegen, flutter build web
    dockerbuild.sh             # Docker build: plugin collection, container cleanup, image build
    nginx.sh                   # nginx reverse proxy: config generation and exec
  tests/
    (tests live in src/backend/tests/)
    (E2E tests live in src/e2e_tests/)

  src/dockerimage/
    Dockerfile                  # Pi agent image: node:22-slim + Pi + Python3 + Dart + Flutter + Rust + build-essential + PostgreSQL + SQLite + vim + emacs + net tools
    entrypoint.sh               # Sets up Pi config (FIFO for models.json, system prompt), starts Pi in RPC mode
    system-prompt.md            # Static system prompt for Pi (copied into image)
    builtin-extensions/         # Built-in Pi extensions (port-map.ts, etc.) — not from plugins
    extensions/                 # Generated: collected from $BARK_PLUGINS_DIR/*/extension.ts at build time
    tools/                      # Generated: collected from $BARK_PLUGINS_DIR/*/tools/ at build time

  src/backend/
    pyproject.toml              # Python deps: fastapi, aiodocker, aiosqlite, bcrypt, python-jose
    bark_backend/
      main.py                   # FastAPI app, lifespan, hosted app proxy, default user seeding, static file serving
      api.py                    # API route handlers (auth, workspaces, files, messages) via APIRouter
      auth.py                   # Register/login/logout, JWT, bcrypt password hashing
      user_store.py             # SQLite: users, workspaces, token blocklist, message history
      workspace_manager.py      # Workspace CRUD + host directory management
      container_manager.py      # Docker lifecycle, port allocation, idle timeout, session resume env, shutdown cleanup
      pi_rpc_client.py          # docker attach subprocess for Pi stdin/stdout JSON-RPC (chunked reads for large events)
      agui_translator.py        # Pi RPC events → AG-UI events mapping, file-change detection
      ws_handler.py             # WebSocket auth, workspace routing, AG-UI streaming, session resume, auto-restart
      file_service.py           # Host-side file read/write/delete/rename with path traversal protection
      terminal_manager.py      # Docker exec PTY subprocess for interactive shell access

  src/frontend/
    pubspec.yaml                # Flutter deps: flutter_markdown_plus, flutter_highlight, go_router, etc.
    web/index.html              # HTML shell with Google Fonts, service worker cleanup
    lib/
      main.dart                 # App entry with Provider setup
      app.dart                  # MaterialApp, GoRouter (auth-aware, URL-preserving via hash)
      utils/
        page_title.dart         # Browser tab title updates
        backend_url.dart        # Derives API base URL from <base href> for subpath hosting
        suppress_browser_menu.dart  # Widget to suppress browser context menu per-panel
      widgets/
        bark_logo.dart          # Bark logo widget (orange paw icon)
      tools/
        tool_plugin.dart        # ToolPlugin base class and ToolPluginRegistry
        plugins_generated.dart  # Generated: imports and registers all plugins with plugin.dart
        plugins/                # Generated: .dart files copied from $BARK_PLUGINS_DIR by import_plugins.py
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
        container_terminal.dart # xterm.dart terminal widget with dark theme, PTY via WebSocket
      file_viewer/
        file_viewer_panel.dart  # File tree + content viewer (16pt JetBrains Mono)
        file_upload.dart        # Drag-and-drop upload
      output/
        output_panel.dart       # Debug panel: container lifecycle, queries, tool calls, errors
      layout/
        ide_layout.dart         # Split layout: chat left, Terminal+Files tabs + slidable Debug right
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
- Session resume on reconnect via `--session` CLI flag (passed as `BARK_RESUME_SESSION` env var to the container; avoids `switch_session` RPC which would re-read the FIFO)
- Per-workspace port allocation: well-known container ports (8000+) mapped to host ports (9000+), persisted in SQLite (`port_allocations` table with per-port PRIMARY KEY preventing overlap). Ports allocated at workspace creation, stable across restarts, freed by CASCADE on workspace delete. `num_ports` column on workspaces table (default 5) controls how many; on container start, ports are added/removed to match. `BARK_PORT_MAPPINGS` env var passes container:host pairs to the container.
- Built-in `get_hosted_url` tool converts container port to full user-facing URL using `BARK_PORT_MAPPINGS`, `BARK_HOSTING_HOSTNAME`, `BARK_HOSTING_PROTO`, and `BARK_HOSTING_BASE_PATH`
- Hosted app proxy: user apps are accessible at `{base_path}/hosted/{workspace_id}/{port}/` — the backend streams requests to `localhost:{port}` on the host. No authentication required for hosted app URLs. nginx `X-Forwarded-Prefix` and `$http_host` headers provide the base path and hostname with port.
- LLM provider/model configured via `settings.json` FIFO (sets `defaultProvider` and `defaultModel`)
- API key delivered via `models.json` FIFO (named pipe, written once at startup, deleted after Pi reads it — key never persists on disk)
- Both config FIFOs written by a `nohup` background process that survives the `exec` to Pi — settings.json is written first (Pi's SettingsManager reads it), then models.json (Pi's ModelRegistry reads it)
- All provider env vars (`OLLAMA_*`, `ANTHROPIC_*`, etc.) stripped from Pi's process environment before exec
- System prompt stored as `src/dockerimage/system-prompt.md`, copied into image at build time
- 30-minute idle timeout (configurable via `BARK_IDLE_TIMEOUT_SECONDS`) with automatic container stop, debug notification, and terminal overlay with restart button
- All user containers stopped on logout and backend shutdown
- Read-only root filesystem (`ReadonlyRootfs: True`) — the agent cannot modify system files or install packages outside the workspace. Writable paths:
  - `/workspace` — bind mount to host (user files)
  - `/home/bark/.pi/sessions` — bind mount to host (Pi session history)
  - `/home/bark` — tmpfs (Pi agent config, regenerated each start)
  - `/tmp` — tmpfs (scratch space)
  - `/run`, `/var/log` — tmpfs (runtime)
- `bark` user baked into the image at build time with the host UID/GID (passed as Docker build args)
- Root escalation prevented: root password locked, suid removed from `su`/`chsh`/`chfn`/`newgrp`
- Containers labeled with `bark.managed=true`, `bark.instance=<BARK_INSTANCE_ID>`, and `bark.workspace-id=<id>` for identification, cleanup, and orphan detection (not reliant on image name). Multiple Bark instances on the same host use different `BARK_INSTANCE_ID` values to isolate their containers.
- `Init: True` (Docker `--init`) runs `tini` as PID 1 to reap zombie processes from terminal sessions and tool executions

### Container Terminal

- Direct shell access to the workspace container via the Terminal tab in the right panel
- Uses xterm.dart (pure Flutter terminal emulator) with a dark theme (Tomorrow Night palette)
- Backend spawns `docker exec` subprocess with PTY (`os.openpty`) piped over the existing WebSocket
- Runs as `bark` user in `/workspace` with bash, tab completion, readline, and colored prompt/ls
- Terminal interaction bumps the container idle timeout via `record_activity()`
- On-demand: subprocess starts when user clicks the Terminal tab
- State preserved across tab switches (IndexedStack keeps all panels alive)
- Right-click context menu with Copy (when text selected) and Paste
- Scrollbar for terminal history
- Overlay with restart button when container stops (idle timeout or unexpected), auto-reconnects terminal session after restart
- Cleaned up on workspace disconnect or WebSocket close

### Right Panel Layout

- Two-part split: tabbed panel on top (Terminal, Files tabs) and slidable Debug panel on bottom
- Debug panel collapsed by default, expandable via draggable horizontal divider
- All panels stay alive across switches (IndexedStack for tabs, always-mounted Debug)
- Debug pane receives events from the start, even before first viewed

### Pi Extensions (Tools)

- Extensions are TypeScript files collected from `$BARK_PLUGINS_DIR/*/extension.ts` into `src/dockerimage/extensions/` at build time
- The LLM sees them in its tool list alongside built-in tools (read, write, edit, bash)
- Extensions can be server-side (run code inside the container) or client-side (delegate to the browser via the Extension UI Sub-Protocol)
- AGENTS.md is generated dynamically on each container start, listing all registered extension tools
- Sample plugins exist in `plugins/`:
  - `word_count` — fast file stats (lines, words, characters, size) via Python script (server-side)
  - `pig_latin` — text to Pig Latin converter, pure TypeScript (server-side)
  - `celebrate` — triggers confetti animation in the browser (client-side, via Extension UI Sub-Protocol)
  - `beep` — plays an audible beep tone via Web Audio API (client-side, via Extension UI Sub-Protocol)

### Chat Interface

- Markdown rendering for assistant responses (flutter_markdown_plus)
- Syntax-highlighted code blocks (Monokai Sublime theme, highlight.dart, JetBrains Mono)
- Collapsible tool call cards showing arguments and results
- Streaming indicator while agent is thinking
- Enter to send, Shift+Enter for newline
- Abort button (red when agent running)
- Conversation history persisted to SQLite and restored on workspace reload
- Input history navigation (up/down arrow keys cycle through previous prompts)
- Queued messages shown dimmed with "queued" label, persisted in SQLite
- Persistent error snackbars with close button
- Text is selectable and copyable via native right-click
- Clickable URLs open in a new browser tab

### File Viewer

- Directory tree with file sizes
- Click to view file contents (16pt JetBrains Mono, left-aligned)
- Auto-refresh when Pi writes/edits files or runs file-creating/deleting bash commands
- Auto-refresh when switching to the Files tab (refreshes in-place, preserving current directory)
- Drag-and-drop upload for files and folders (preserves directory structure, progress indicator)
- Uploads go into the currently viewed directory (not always root)
- Duplicate detection: blocks upload if a file or folder with the same name already exists
- Right-click context menu on files and folders: Download, Rename (with dialog), and Delete (with confirmation)
- Download files directly; download folders as .zip (zipped on the fly by the backend)
- Path bar with ellipsis overflow, clickable `/` root link, and up-arrow navigation button
- nginx `client_max_body_size 500m` for large file uploads
- nginx `sub_filter` rewrites `<base href>` for subpath hosting (`/bark/`)

### Debug Panel

- Container lifecycle events (starting, ready with port info and status, idle stop, restart)
- Session resume notifications
- Query text shown for each prompt sent
- Tool call entries from Pi (including extension tools)
- Error entries
- Timestamps and color-coded entries
- Selectable text for titles and content
- Clear button

### UI/Theme

- Harvest-inspired light theme (warm off-white, green accents, medium gray header)
- Orange Bark logo (paw icon + "Bark" text)
- 3D edges on all dividers, panel headers, and borders
- Two-column layout: chat (left, 38%) and right panel (62%) with resizable vertical divider
- Right panel: Terminal+Files tabs on top, slidable Debug panel on bottom (collapsed by default)
- All panels kept alive via IndexedStack (tabs) and always-mounted Debug
- Dark blue back/logout buttons
- Browser tab title updates per page ("Bark - Login", "Bark - Workspaces", "Bark - workspace-name")

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

# Start the app (foreground with TUI)
devenv processes up

# Or start in background (no TUI)
devenv processes up -d

# Disable TUI globally (useful for scripting/CI)
export DEVENV_TUI=0

# Open in browser
open http://localhost:8997
```

### Environment Variables

All settings can be overridden in `.env`. Defaults (where appropriate) are provided in `devenv.nix` at low priority so `.env` values take precedence.

| Variable                    | Default                              | Description                                                                                                               |
| --------------------------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| `BARK_PORT`                 | `8997`                               | Backend (FastAPI/uvicorn) port                                                                                            |
| `BARK_NGINX_PORT`           | `8995`                               | nginx reverse proxy port                                                                                                  |
| `BARK_SOLIPLEX_PORT`        | `8555`                               | Soliplex backend port (for nginx proxy)                                                                                   |
| `BARK_DATA_DIR`             | `~/.bark/data`                       | Database, workspaces, Pi sessions                                                                                         |
| `BARK_PLUGINS_DIR`          | `~/.bark/plugins`                    | Fetched plugins (outside repo for `execIfModified`)                                                                       |
| `BARK_IMAGE_NAME`           | `bark-pi`                            | Docker image name for workspace containers                                                                                |
| `BARK_INSTANCE_ID`          | `default`                            | Instance identifier for multi-instance deployments on the same host — isolates containers, names, and cleanup             |
| `BARK_HOSTING_HOSTNAME`     | (from `Host` header)                 | Hostname for user-facing app URLs. Auto-derived from `X-Forwarded-Host` or `Host` WebSocket header if not set             |
| `BARK_HOSTING_PROTO`        | (from `X-Forwarded-Proto` or `http`) | Protocol for user-facing app URLs. Auto-derived from request headers if not set                                           |
| `BARK_HOSTING_BASE_PATH`    | (from `X-Forwarded-Prefix` or empty) | Base path prefix for user-facing app URLs (e.g., `/bark`). Auto-derived from nginx `X-Forwarded-Prefix` header if not set |
| `BARK_IDLE_TIMEOUT_SECONDS` | `1800`                               | Container idle timeout in seconds (check interval auto-computed as timeout/3, clamped 10–60s)                             |
| `SOLIPLEX_URL`              | (empty)                              | Soliplex base URL as seen by browser (empty = same origin)                                                                |
| `OLLAMA_API_KEY`            |                                      | Ollama Cloud API key                                                                                                      |
| `OLLAMA_BASE_URL`           |                                      | Ollama API URL (cloud or self-hosted)                                                                                     |
| `OLLAMA_MODEL`              |                                      | LLM model name                                                                                                            |
| `BARK_JWT_SECRET`           |                                      | JWT signing secret                                                                                                        |
| `BARK_DEFAULT_USER`         |                                      | Auto-seeded user on startup                                                                                               |
| `BARK_DEFAULT_PASSWORD`     |                                      | Auto-seeded password on startup                                                                                           |

### Ports

- `BARK_PORT` (default `8997`): Web UI + API (single FastAPI/uvicorn server)
- `BARK_NGINX_PORT` (default `8995`): nginx reverse proxy
- `9000+`: User app ports (5 per workspace)

### Rebuild

To force rebuild the Docker image and Flutter web app:

```bash
devenv shell -- rebuild
```

Then restart the processes. On normal startup, Flutter and Docker builds run automatically when their source files have changed (via devenv `execIfModified` content hashing). Watched paths:

- **Flutter**: `src/frontend/lib`, `src/frontend/web`, `src/frontend/pubspec.yaml`, `src/frontend/pubspec.lock`, `$BARK_PLUGINS_DIR/**/*.dart`, `$BARK_PLUGINS_DIR/plugins.lock`
- **Docker**: `src/dockerimage/Dockerfile`, `src/dockerimage/entrypoint.sh`, `src/dockerimage/*.md`, `src/dockerimage/builtin-extensions/*.ts`, `$BARK_PLUGINS_DIR/**/*.ts`, `$BARK_PLUGINS_DIR/**/tools/**`, `$BARK_PLUGINS_DIR/plugins.lock`

### Testing

**Unit tests** (backend, no Docker required):

```bash
# Run all backend tests (devenv script)
devenv shell -- test-backend

# Or directly with pytest
devenv shell -- python -m pytest src/backend/tests -v

# Run a single test file
devenv shell -- test-backend src/backend/tests/test_auth.py

# Run a single test by name
devenv shell -- test-backend -k 'test_login_success'
```

100% line coverage across all backend modules. Tests use real SQLite databases in pytest temp directories (no mocking of the database layer). Docker and subprocess interactions are mocked. Each test gets its own isolated temp directory — multiple test processes can run in parallel without conflicts. Coverage report is printed automatically.

**E2E tests** (Playwright, requires running devenv processes):

```bash
# Install browsers (first time only)
devenv shell -- bash -c "cd src/e2e_tests && npm run install-browsers"

# Run all tests (devenv script)
devenv shell -- test-e2e

# Run a single test by name
devenv shell -- test-e2e -g 'login with default credentials'

# Run with headed browser (visible) — useful for debugging coordinate-based clicks
devenv shell -- test-e2e --headed

# Run with verbose output
devenv shell -- test-e2e --reporter=list
```

E2E tests start their own isolated Bark server via `globalSetup` — no need to start one manually. The test server uses non-default ports (18997/18995) and a temp `BARK_DATA_DIR` so it doesn't conflict with a running dev server. Config is written to `.env.e2e` which overrides `.env` via `dotenv.filename` in `devenv.nix`. The temp directory and `.env.e2e` are cleaned up in `globalTeardown`. Each test creates its own workspace and cleans it up, so tests are independent and can run in any order. Flutter Web renders to canvas, so UI interaction uses coordinate-based clicks on `<flutter-view>`. LLM-dependent tests require `OLLAMA_API_KEY`, `OLLAMA_BASE_URL`, and `OLLAMA_MODEL` in `.env` or the process environment.

**Frontend unit tests** (Dart/Flutter, no browser required):

```bash
# Run all frontend tests (devenv script)
devenv shell -- test-frontend

# Run a single test file
devenv shell -- test-frontend test/agui_events_test.dart
```

Tests cover agui events, tool plugin registry, auth service, output panel, IDE layout, agui client, login page, file upload, file viewer panel, chat panel, container terminal, workspace list page, and bark logo. Browser-only APIs (`dart:html`, `dart:js_interop`) are abstracted via conditional imports (`web_helpers_stub.dart`/`web_helpers_web.dart`) so tests run in VM mode without a browser.

### Pre-commit Hooks

Pre-commit hooks run automatically on `git commit` via [git-hooks.nix](https://github.com/cachix/git-hooks.nix):

- **ruff check --fix** — Python linting with auto-fix
- **ruff format** — Python formatting
- **dart format** — Dart formatting
- **prettier** — TypeScript, JavaScript, and YAML formatting
- **yamllint** — YAML linting

Hooks are installed automatically when entering the devenv shell.

### CI

GitHub Actions run automatically on PRs and pushes to main (all also support `workflow_dispatch` for manual triggering):

- **Backend tests** (`.github/workflows/backend-tests.yml`) — triggered by changes to `src/backend/` or `pytest.ini`
- **Frontend tests** (`.github/workflows/frontend-tests.yml`) — triggered by changes to `src/frontend/lib/`, `src/frontend/test/`, or `src/frontend/pubspec.yaml`
- **E2E tests** (`.github/workflows/e2e-tests.yml`) — runs hourly via schedule (skips if no commits in the last hour) and via manual `workflow_dispatch`. Requires `OLLAMA_API_KEY`, `OLLAMA_BASE_URL`, and `OLLAMA_MODEL` secrets. Uses Nix/devenv to build and run the full stack, then runs Playwright against the running server. Uploads test results as artifacts on failure.

### Plugin System

All plugins live in `$BARK_PLUGINS_DIR/<name>/` directories. A plugin can contain:

- `extension.ts` — Pi extension with `pi.registerTool()`. Copied to `src/dockerimage/extensions/` at build time.
- `plugin.dart` — Dart class extending `ToolPlugin` for client-side action handling. Must export a class with `extends ToolPlugin`.
- `*.dart` — Supporting Dart files (widgets, utilities). All `.dart` files are copied alongside `plugin.dart`.
- `tools/` — Server-side scripts. Everything in this subdirectory is copied to `/usr/local/bin/bark-tools/` in the Docker image.

A plugin needs at minimum an `extension.ts`. The `plugin.dart` is only needed for client-side tools that delegate execution to the browser via `ctx.ui.input("HOST_TOOL_REQUEST", ...)`.

**Build integration:**

- `scripts/import_plugins.py` scans `$BARK_PLUGINS_DIR/*/plugin.dart`, copies `.dart` files into `src/frontend/lib/tools/plugins/`, and generates `plugins_generated.dart`
- `dockerbuild` collects `extension.ts` and `tools/` files from all plugins into the Docker build context
- `flutterbuildweb` runs the codegen before compiling
- Both are triggered automatically by `devenv up` via `execIfModified`

**Adding a plugin:**

For local development, create files directly in `$BARK_PLUGINS_DIR`:

1. Create `$BARK_PLUGINS_DIR/<name>/extension.ts` with `pi.registerTool()`
2. For client-side tools, add `plugin.dart` extending `ToolPlugin` with action handlers
3. For server-side scripts, add files in `$BARK_PLUGINS_DIR/<name>/tools/`
4. `devenv up` rebuilds automatically when `$BARK_PLUGINS_DIR` changes

For remote plugins, add an entry to `$BARK_PLUGINS_DIR/plugins.yaml` and run `update-plugins` to fetch it. See **Plugin management** below.

**Plugin management:**

Plugins are declared in `$BARK_PLUGINS_DIR/plugins.yaml`. Each entry requires `name` and `git`; `path` and `ref` are optional:

```yaml
plugins:
  - name: celebrate
    git: git@github.com:mcdonc/bark.git
    path: plugins/celebrate
    ref: main
  - name: beep
    git: git@github.com:mcdonc/bark.git
    path: plugins/beep
    ref: main
  - name: pig-latin
    git: git@github.com:mcdonc/bark.git
    path: plugins/pig-latin
    ref: main
  - name: word-count
    git: git@github.com:mcdonc/bark.git
    path: plugins/word-count
    ref: main
```

- `scripts/update_plugins.py` — Python script that manages plugin fetching:
  - If `$BARK_PLUGINS_DIR` doesn't exist, creates it with a template `plugins.yaml` that includes sample plugins (celebrate, beep, pig-latin, word-count)
  - If `plugins.yaml` exists, fetches listed plugins, resolves git refs to commit SHAs, and writes `plugins.lock`
  - On full update, removes fetched plugin directories that are no longer listed in `plugins.yaml` (local-only plugins not in the lockfile are left alone)
- `update-plugins` — devenv script alias that runs `python3 scripts/update_plugins.py "$@"`
- `update-plugins <name>` — fetch/update a single plugin by name, preserving other lock entries
- `plugins/` — directory in the Bark repo containing starter plugin source. These aren't special — they're just plugins that happen to live in the same repo and are referenced in the generated template.
- `plugins.lock` — records resolved commit SHAs for reproducible builds
- On first `devenv up`, if `plugins.yaml` exists but no lockfile is found, `update-plugins` runs automatically. After that, updates are explicit only.
- Local plugin development: drop a directory into `$BARK_PLUGINS_DIR` directly — the build system treats it the same as a fetched plugin.
- `execIfModified` watches `$BARK_PLUGINS_DIR` to trigger rebuilds when plugin content or the lockfile changes.
- Since `$BARK_PLUGINS_DIR` is outside the repo, there are no `.gitignore` conflicts with devenv's `execIfModified`.

### Data

- All data stored in `$BARK_DATA_DIR` (defaults to `~/.bark/data`)
- SQLite database: `bark.db` (users, workspaces, messages, token blocklist)
- Workspace files: `workspaces/<user-id>/data/<workspace-id>/`
- Pi sessions: `workspaces/<user-id>/sessions/<workspace-id>/`
- Database persists across restarts and rebuilds

## Client-Side Tool Delegation via Extension UI Sub-Protocol

We use Pi's **Extension UI Sub-Protocol** to delegate tool execution to the browser.

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

- **celebrate** (`plugins/celebrate/`): Triggers confetti animation in the browser
- **beep** (`plugins/beep/`): Plays a beep sound in the browser

### Soliplex integration

Soliplex has its own Bark plugins currently hosted on the `bark-integration` branch of the Soliplex repository within `bark-plugin`. The `bark` repository has some sops to Soliplex integration, namely that it starts an nginx service that is unnecessary for non-integraion scenarios.

The Soliplex tools run entirely in the browser, which has the user's Soliplex authentication cookies. When deployed behind nginx on the same domain, the browser can call Soliplex APIs directly with no CORS issues. Set `SOLIPLEX_URL` in `.env` to tell the frontend where Soliplex is (served via the `/api/config` endpoint). Leave it empty when Bark and Soliplex share the same origin (the typical nginx setup). Cross-origin setups require CORS configuration on the Soliplex side.

The query flow: frontend creates a thread in the Soliplex room, posts the user's question as an AG-UI `RunAgentInput`, collects the streamed SSE response, extracts `TEXT_MESSAGE_CONTENT` deltas, and returns the assembled text to the Pi extension.

- **soliplex_list_rooms** (external, via `plugins.yaml`): Lists available Soliplex knowledge base rooms
- **soliplex_query** (external, via `plugins.yaml`): Queries a Soliplex room via AG-UI (creates thread, posts question, collects SSE response). Default room: `search`

The devenv.nix currently runs nginx for local Soliplex development:

```
nginx reverse proxy (port 8995)
    ├── /bark/     → Bark backend (port 8997)
    └── /          → Soliplex backend (port 8555)
```

## TODO

- **Local files pane**: Add a browser-side file pane where users can upload files into an in-browser-memory filesystem (e.g., using the File System Access API or an in-memory store). These files would be accessible to client-side plugins and could be passed to the REPL as context without uploading to the server. Useful for working with sensitive files that shouldn't leave the browser, or for quick one-off analysis without persisting to the workspace.

- **Don't stop container on tab navigation**: `workspace_page.dart`'s `deactivate()` calls `disconnectWorkspace()`, which stops the container. This means switching browser tabs or navigating within the app kills the container. The container should only stop on explicit logout, workspace switch, or idle timeout — not on widget deactivation from tab changes. Alternatively (or additionally), the hosted app proxy could auto-start the container before proxying if it's not running.

- **Parallelize Playwright E2E tests**: Tests are now independent (no serial mode, each creates its own workspace), but still run with 1 worker. Setting `workers > 1` would require verifying that concurrent Docker container starts, port allocations, and workspace operations don't conflict on a single Bark server.
- **Clean up stale plugin build artifacts**: When a plugin is removed from `plugins.yaml`/`plugins.lock`, `update-plugins` removes the plugin directory from `$BARK_PLUGINS_DIR`, but the build artifacts remain: `.dart` files in `src/frontend/lib/tools/plugins/`, `.ts` files in `src/dockerimage/extensions/`, and tool scripts in `src/dockerimage/tools/`. Stale files accumulate and can cause build errors. The build scripts (`import_plugins.py`, `dockerbuild.sh`) should delete any plugin-originated files that don't correspond to a current plugin in `$BARK_PLUGINS_DIR` before copying fresh ones.
- **Plugin directory structure**: Consider whether each plugin should have explicit subdirectories for different file types (e.g., `dart/` for Flutter code, `extension/` for TypeScript, `tools/` for server-side scripts) instead of the current flat layout where `import_plugins.py` copies all `*.dart` files and `dockerbuild` picks up `extension.ts` by name. Subdirectories would simplify the copying logic and make it clearer what goes where.
- **Plugin version numbers**: Plugins may want their own version numbers (in `plugin.yaml` or similar metadata) for compatibility checking, display in the UI, and meaningful pinning beyond git refs.
- **Entrypoint nohup zombie**: The `nohup sh -c "cat ... > FIFO ..."` writer in `entrypoint.sh` (line 78) leaves one `[sh] <defunct>` zombie per container start. Its parent is the `su` process which doesn't call `wait()`. Harmless but cosmetic. Fix by restructuring the entrypoint so the writer finishes before the final `exec`, or by using a different mechanism to feed the FIFOs.
- **Container resource limits**: Add CPU/memory limits to containers to prevent runaway processes.
- **Container network isolation**: Restrict container network access to prevent use as an attack platform. Use a custom Docker network with limited egress — allow only the Ollama API endpoint (cloud or self-hosted) and block all other outbound traffic. Consider using `--network=none` with a proxy sidecar for allowlisted domains only.
- **Syntax highlighting language detection**: Improve code block language detection for unlabeled blocks.
- **Strip env vars from terminal session**: Currently `docker exec -e VAR=` blanks sensitive env vars (API keys, BARK_RESUME_SESSION) but they still appear in `env` output as empty strings. Investigate using `env -u` inside the exec command or wrapping the shell invocation to fully unset them rather than just blanking.
- **Same-workspace multi-window**: Opening the same workspace in two browser windows simultaneously has undefined behavior — both WebSocket connections share one Pi container/session, and prompts from either window could collide or interleave unpredictably. Consider either locking a workspace to one connection at a time, or multiplexing both windows onto the same event stream.
- **Workspace disk quotas**: Limit how much disk space each workspace can consume. Options: use filesystem quotas (XFS/ext4 project quotas on the host), overlay2 with size limits, or a loopback-mounted filesystem per workspace with a fixed size. Should also surface current disk usage in the UI (file viewer header or workspace list) so users can see how much space they've used.
- **User dotfile customization**: Allow users to customize their container shell environment (`.bashrc`, `.vimrc`, `.emacs`, `.gitconfig`, etc.). Options: bind-mount a per-user dotfiles directory from the host into `/home/bark`, or provide a UI for editing dotfiles that persist across container restarts. Currently `/home/bark` is a tmpfs regenerated each start, so any customization is lost.
- **Investigate running Pi under bubblewrap**: Explore using [bubblewrap](https://github.com/containers/bubblewrap) (bwrap) as an alternative to Docker for sandboxing Pi. Bubblewrap is lighter-weight than Docker — no daemon, no image builds, no container overhead — and provides namespace-based isolation (mount, PID, network, user). This could significantly reduce startup time and resource usage. Trade-offs: no pre-built image caching, need to manage tool installations on the host, less isolation than full container. Could be offered as an alternative backend alongside Docker.
- **Remove leading underscores from internal functions**: Functions like `_handle_prompt`, `_forward_events`, `_cleanup_connection`, `_derive_hosting_info`, etc. in `ws_handler.py` and helper functions in other modules use leading underscores to signal "module-private". Since these are now tested directly via imports, the underscores are unnecessary and make the test imports look odd. Rename to drop the underscores.
- **Dart/Flutter unit tests**: Add widget and unit tests for the Flutter frontend. Key areas to cover: `AguiClient` (WebSocket connection, event parsing), `AuthService` (token storage, login/logout), `ToolPluginRegistry` (plugin dispatch), `FileViewerPanelState` (navigation, refresh, breadcrumbs), and `ChatPanel` (message rendering, input handling). Use `flutter test` with `mockito` or manual mocks for WebSocket and HTTP dependencies.
- **Test workspace_page.dart and app.dart**: `workspace_page.dart` imports the gitignored `plugins_generated.dart`, making it untestable on a clean checkout. Options: commit a stub `plugins_generated.dart` that returns `[]` (codegen overwrites it), or make the import conditional. `app.dart` needs GoRouter/navigation mocking.
- **Clipboard image paste in chat**: Investigate whether Pi supports image inputs and, if so, allow pasting images from the clipboard into the chat input field. Would need to intercept paste events, detect image MIME types, convert to a format Pi can accept (base64 or URL), and pass via the `images` parameter of `prompt()`.
- **Simplify deploy script**: Consider replacing `synctoarctor.sh` with a plain `rsync` of the entire project directory instead of the current approach of syncing specific files and directories individually. Would be simpler to maintain and less likely to miss new files.
- **Hosted app URLs should respect external headers**: The `get_hosted_url` tool generates URLs using `BARK_HOSTING_*` env vars passed to the container. These are derived from `X-Forwarded-Host`/`X-Forwarded-Proto` headers at WebSocket connect time, but if the hosting environment changes (e.g., different reverse proxy), the container's cached values become stale. Consider re-deriving hosting info on each `get_hosted_url` call or providing a mechanism to update the container's env vars without restart.
