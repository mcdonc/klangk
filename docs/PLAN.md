# Bark — Multi-User Web Coding Agent

## Overview

Bark is a multi-user web app that gives each user their own isolated Pi coding agent (pi.dev) running in a Docker container. Users authenticate with a simple login, can create multiple named workspaces, and interact with Pi through a split-pane UI with a chat interface, file viewer, and debug panel.

## Architecture

```
Browser (Flutter Web + Chat UI + AG-UI)
    ├── AG-UI events over WebSocket (authenticated)
    ├── Extension UI responses (client-side tool results)
nginx reverse proxy (port 8995, serves UI + API + hosted app proxy)
    ↕
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
$BARK_DATA_DIR/workspaces/<user-id>/work/<workspace-id>/
```

### Components

- **Backend** (`src/backend/`): Python/FastAPI — single-port server for API, WebSocket, and frontend static files
- **Frontend** (`src/frontend/`): Flutter Web — chat with markdown rendering, syntax-highlighted code blocks, file viewer, debug panel
- **Docker** (`src/dockerimage/`): Custom Dockerfile for Pi agent containers with Python3, Node.js, build-essential, SQLite, vim, emacs, network tools, Pi extensions

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
  bootstrap                     # Install Nix + devenv

  plugins/              # Starter plugins (source of truth, fetched into $BARK_PLUGINS_DIR by update-plugins)
    celebrate/                  # Confetti animation (client-side)
    beep/                       # Audible beep tone (client-side)
    pig-latin/                  # Text to Pig Latin converter (server-side)
    word-count/                 # Fast file stats (server-side)
  scripts/
    import_dart_plugins.py     # Codegen: generates $BARK_PLUGINS_DIR/.dart/ package with plugin deps
    update_plugins.py          # Fetches plugins from git repos, writes plugins.lock
    stub_dart_plugins.sh       # Creates minimal bark_plugins stub for first-time checkout / CI
    flutterbuildweb.sh         # Flutter build: plugin auto-fetch, codegen, flutter build web, cache-bust
    dockerbuild.sh             # Docker build: plugin staging, container cleanup, workspace image build (named build contexts)
    dockerbuild-base.sh        # Build base Docker image
    pull-base-image.sh         # Pull latest base image from GHCR (if changed)
    nginx.sh                   # nginx reverse proxy: config generation and exec

  src/dockerimage/
    Dockerfile                  # Workspace image: FROM bark-pi-base + plugin extensions + tools + entrypoint + /etc/bash.bashrc
    Dockerfile.base             # Base image: node:22-slim + Pi + Python3 + build-essential + SQLite + vim + emacs + net tools + /bin/sh→bash (pushed to GHCR)
    entrypoint.sh               # Sets up Pi config (FIFO for models.json, system prompt), starts Pi in RPC mode
    system-prompt.md            # Static system prompt for Pi (copied into image)
    builtin-extensions/         # Built-in Pi extensions (port-map.ts, etc.) — not from plugins
    # Plugin extensions and tools are staged at $BARK_PLUGINS_DIR/.docker/ at build time via named Docker build contexts

  src/backend/
    pyproject.toml              # Python deps: fastapi, aiodocker, aiosqlite, bcrypt, python-jose
    bark_backend/
      main.py                   # FastAPI app, lifespan, default user seeding, static file serving
      api.py                    # API route handlers (health, auth, workspaces, files, messages, admin) via APIRouter
      auth.py                   # Register/login/logout, JWT with roles, bcrypt, require_role(), email validation, verification tokens
      email_service.py          # Email sending via SMTP or sendmail (verification emails)
      user_store.py             # SQLite: users (with verified flag), workspaces, roles, user_roles, token blocklist, message history
      workspace_manager.py      # Workspace CRUD + host directory management + user data archival
      container_manager.py      # Docker lifecycle, port allocation, idle timeout, session resume env, shutdown cleanup
      pi_rpc_client.py          # docker attach subprocess for Pi stdin/stdout JSON-RPC (chunked reads for large events)
      agui_translator.py        # Pi RPC events → AG-UI events mapping, file-change detection
      ws_handler.py             # WebSocket auth, workspace routing, AG-UI streaming, session resume, auto-restart
      file_service.py           # Host-side file read/write/delete/rename with path traversal protection
      terminal_manager.py      # Docker exec PTY subprocess for interactive shell access
    tests/                      # pytest unit tests (100% coverage, parallel via xdist)

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
      (ToolPlugin and ToolPluginRegistry are in the bark_plugin_api package)
      (Plugin registration is in the bark_plugins package at $BARK_PLUGINS_DIR/.dart/)
      auth/
        auth_service.dart       # JWT storage, login/register/logout, async init, email/roles/isAdmin from JWT payload
        login_page.dart         # Login/register form with email validation
        verify_page.dart        # Email verification page (auto-login on success)
      admin/
        admin_users_page.dart   # Admin user management: list, add, edit, delete users, toggle roles
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
    test/                       # Dart unit tests (VM-mode, no browser required)

  src/e2e_tests/                # Playwright E2E tests with isolated test server
    e2e/bark.spec.ts            # E2E test specs
    global-setup.ts             # Starts isolated Bark server for E2E
    global-teardown.ts          # Stops test server, cleans up temp data
    playwright.config.ts        # Playwright configuration
```

## Features

### Authentication

- Email/password with bcrypt hashing, email validated at registration
- Email verification: registration sends a verification email with a signed token link; user must click to activate account and is auto-logged-in on verification
- Email sent via SMTP (`BARK_SMTP_HOST/PORT/USER/PASSWORD/FROM`) or local sendmail (default, configurable via `BARK_SENDMAIL_PATH`)
- JWT tokens (24hr expiry, secret configurable via BARK_JWT_SECRET) with token blocklist for logout, roles claim in JWT payload
- Role-based access control: `roles` and `user_roles` tables, `require_role()` FastAPI dependency for endpoint protection
- Default user auto-seeded on startup with admin role (configurable via BARK_DEFAULT_USER/PASSWORD in .env)
- Admin user management: list/add/edit/delete users, toggle roles, user data archived to tar.xz on deletion, self-deletion prevented
- Open registration with email verification (test mode auto-verifies for E2E tests)
- Login rejects unverified accounts
- Session persists across page reloads (async token loading before routing)
- Deep link preservation: unauthenticated visits to protected URLs redirect to login, then return to the original URL after successful login

### Workspaces

- Multiple workspaces per user
- Each workspace gets its own Docker container + bind-mounted directory
- URL-based workspace routing (survives page reload via hash URL reading)
- Deep link preservation: unauthenticated visits to protected URLs redirect to login with a `?redirect=` param, then return to the original URL after successful login
- Workspace name and logged-in user email shown in app bar, browser tab title
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
- Hosted app proxy: user apps are accessible at `{base_path}/hosted/{workspace_id}/{port}/` — nginx proxies requests directly to `localhost:{port}` on the host (bypassing the Python backend). No authentication required for hosted app URLs.
- LLM provider/model configured via `settings.json` FIFO (sets `defaultProvider` and `defaultModel`)
- API key delivered via `models.json` FIFO (named pipe, written once at startup, deleted after Pi reads it — key never persists on disk)
- Both config FIFOs written by a `nohup` background process that survives the `exec` to Pi — settings.json is written first (Pi's SettingsManager reads it), then models.json (Pi's ModelRegistry reads it)
- All provider env vars (`OLLAMA_*`, `ANTHROPIC_*`, etc.) stripped from Pi's process environment before exec
- `/bin/sh` symlinked to `/bin/bash` in the base image so Pi's bash tool supports bashisms (`source`, etc.)
- System prompt (`src/dockerimage/system-prompt.md`) copied into image at build time. Instructs the agent to: create virtualenvs for Python projects, run `npm init` for Node projects, background long-running servers, always use `get_hosted_url` for fresh URLs, show full URLs as link text, and warn users that container restarts kill running processes
- 30-minute idle timeout (configurable via `BARK_IDLE_TIMEOUT_SECONDS`) with automatic container stop, debug notification, and terminal overlay with restart button. Activity is recorded on user actions (prompt, steer, terminal input) and on every Pi event (tool calls, text streaming), so containers stay alive during long-running LLM requests as long as events are flowing. Stuck tool executions (e.g., foreground server) produce no events and will eventually time out.
- All user containers stopped on logout and backend shutdown
- Read-only root filesystem (`ReadonlyRootfs: True`) — the agent cannot modify system files or install packages outside the workspace. Writable paths:
  - `/work` — bind mount to host (user files, `$BARK_DATA_DIR/workspaces/<user>/work/<workspace>/`)
  - `/home/bark` — bind mount to host (persistent home, `$BARK_DATA_DIR/workspaces/<user>/home/<workspace>/`). Dotfiles (`.bashrc`, `.vimrc`, `.gitconfig`), bash history, and Pi sessions persist across container restarts. Pi agent config (`.pi/agent/`) is cleaned and regenerated each start.
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
- Runs as `bark` user in `/work` with bash, tab completion, readline, colored prompt/ls, and persistent history (defaults from `/etc/bash.bashrc` in the image, overridable via `~/.bashrc` on the persistent home mount). History is flushed to `~/.bash_history` after each command via `PROMPT_COMMAND` so it survives terminal kills.
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

- Extensions are TypeScript files collected from `$BARK_PLUGINS_DIR/*/extension.ts` and staged at `$BARK_PLUGINS_DIR/.docker/extensions/` at build time (injected via named Docker build contexts)
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
- Clickable URLs open in a new browser tab, with a copy button next to each link
- Bare URLs in assistant messages are auto-linked (converted to clickable markdown links)

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

- **nginx is the primary access point** (port 8995 locally). It proxies API/WebSocket to uvicorn and proxies hosted app URLs directly to container ports (no Python in the hosted app path).
- FastAPI serves API endpoints and Flutter frontend static files on port 8997 (not accessed directly by users).
- Hosted app URLs (`/hosted/{workspace_id}/{port}/`) are handled by an nginx regex location that extracts the port and proxies to `127.0.0.1:{port}`.
- Subpath hosting (e.g., `/bark/` on arctor) handled by an outer nginx that sends `X-Forwarded-Prefix`, `X-Forwarded-Host`, and `X-Forwarded-Proto` headers. Bark's `_derive_hosting_info` uses these to generate correct hosted app URLs. The outer nginx also rewrites `<base href>` via `sub_filter`.
- Frontend derives API URLs from `<base href>` — works on both root and subpath.
- WebSocket proxying via nginx `proxyWebsockets`.

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
BARK_DEFAULT_USER=admin@example.com
# BARK_DEFAULT_PASSWORD=admin  # omit to generate a random password on first run
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
open http://localhost:8995
```

### Environment Variables

All settings can be overridden in `.env`. Defaults (where appropriate) are provided in `devenv.nix` at low priority so `.env` values take precedence.

| Variable                    | Default                              | Description                                                                                                                                               |
| --------------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `BARK_NGINX_PORT`           | `8995`                               | **Primary access point** — nginx reverse proxy port (UI, API, WebSocket, hosted apps)                                                                     |
| `BARK_PORT`                 | `8997`                               | Backend (FastAPI/uvicorn) port — proxied through nginx, not accessed directly                                                                             |
| `BARK_SOLIPLEX_PORT`        | `8555`                               | Soliplex backend port (unused unless Soliplex integration is configured)                                                                                  |
| `BARK_DATA_DIR`             | `~/.bark/data`                       | Database, workspaces, Pi sessions                                                                                                                         |
| `BARK_PLUGINS_DIR`          | `~/.bark/plugins`                    | Fetched plugins (outside repo for `execIfModified`)                                                                                                       |
| `BARK_IMAGE_NAME`           | `bark-pi`                            | Docker image name for workspace containers                                                                                                                |
| `BARK_INSTANCE_ID`          | `default`                            | Instance identifier for multi-instance deployments on the same host — isolates containers, names, and cleanup                                             |
| `BARK_HOSTING_HOSTNAME`     | (auto-derived)                       | Hostname for hosted app URLs. Behind a reverse proxy: uses `X-Forwarded-Host` as-is. Direct access: uses `Host` header with `BARK_NGINX_PORT` substituted |
| `BARK_HOSTING_PROTO`        | (from `X-Forwarded-Proto` or `http`) | Protocol for user-facing app URLs. Auto-derived from request headers if not set                                                                           |
| `BARK_HOSTING_BASE_PATH`    | (from `X-Forwarded-Prefix` or empty) | Base path prefix for user-facing app URLs (e.g., `/bark`). Auto-derived from nginx `X-Forwarded-Prefix` header if not set                                 |
| `BARK_IDLE_TIMEOUT_SECONDS` | `1800`                               | Container idle timeout in seconds (check interval auto-computed as timeout/3, clamped 10–60s)                                                             |
| `SOLIPLEX_URL`              | (empty)                              | Soliplex base URL as seen by browser (empty = same origin)                                                                                                |
| `OLLAMA_API_KEY`            |                                      | Ollama Cloud API key                                                                                                                                      |
| `OLLAMA_BASE_URL`           |                                      | Ollama API URL (cloud or self-hosted)                                                                                                                     |
| `OLLAMA_MODEL`              |                                      | LLM model name                                                                                                                                            |
| `BARK_JWT_SECRET`           |                                      | JWT signing secret                                                                                                                                        |
| `BARK_DEFAULT_USER`         |                                      | Auto-seeded admin email on startup                                                                                                                        |
| `BARK_DEFAULT_PASSWORD`     |                                      | Auto-seeded password on startup (omit to generate random)                                                                                                 |
| `BARK_SMTP_HOST`            |                                      | SMTP server hostname (if set, uses SMTP; otherwise uses sendmail)                                                                                         |
| `BARK_SMTP_PORT`            | `587`                                | SMTP server port                                                                                                                                          |
| `BARK_SMTP_USER`            |                                      | SMTP auth username                                                                                                                                        |
| `BARK_SMTP_PASSWORD`        |                                      | SMTP auth password                                                                                                                                        |
| `BARK_SMTP_FROM`            |                                      | Email sender address (falls back to SMTP_USER, then noreply@localhost)                                                                                    |
| `BARK_SMTP_USE_TLS`         | `true`                               | Use STARTTLS for SMTP                                                                                                                                     |
| `BARK_SENDMAIL_PATH`        | `sendmail`                           | Path to sendmail binary (used when BARK_SMTP_HOST is not set)                                                                                             |

### Ports

- `BARK_NGINX_PORT` (default `8995`): **Primary access point** — nginx serves UI, API, WebSocket, and proxies hosted app URLs directly to container ports
- `BARK_PORT` (default `8997`): Backend (FastAPI/uvicorn)
- `9000+`: User app ports (5 per workspace)

### Rebuild

To force rebuild the Docker image and Flutter web app:

```bash
devenv shell -- rebuild
```

The `dockerbuild` and `flutterbuildweb` commands run the corresponding devenv tasks (`bark:docker-build`, `bark:flutter-build`) with `--refresh-task-cache` to force a rebuild regardless of `execIfModified` state. The `rebuild` command runs both. The `pull-base-image` command pulls the latest base image from GHCR (run this after CI rebuilds the base image).

On normal startup, Flutter and Docker builds run automatically when their source files have changed (via devenv `execIfModified` content hashing). Watched paths:

- **Flutter**: `src/frontend/lib/**`, `src/frontend/web/**`, `src/frontend/pubspec.yaml`, `src/frontend/pubspec.lock`, `$BARK_PLUGINS_DIR/**/*.dart`, `$BARK_PLUGINS_DIR/plugins.lock`
- **Docker**: `src/dockerimage/**`, `$BARK_PLUGINS_DIR/**/*.ts`, `$BARK_PLUGINS_DIR/**/tools/**`, `$BARK_PLUGINS_DIR/plugins.lock`

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

**E2E tests** (Playwright, Chromium + Firefox + WebKit):

```bash
# Run all tests (devenv script — installs npm deps, runs each browser sequentially)
devenv shell -- test-e2e

# Run a single test by name
devenv shell -- test-e2e -g 'navigate to workspace'

# Run with headed browser (visible) — useful for debugging coordinate-based clicks
devenv shell -- test-e2e --headed

# Run with verbose output
devenv shell -- test-e2e --reporter=list
```

E2E tests run against Chromium, Firefox, and WebKit using browsers from `pkgs.playwright-driver.browsers` (NixOS-patched, no manual browser install needed). The `@playwright/test` npm version must match `pkgs.playwright-driver.version` exactly (currently 1.59.1). Browsers run sequentially (one at a time) to avoid memory pressure from multiple browser engines — within each browser, tests run in parallel with one worker per CPU core (default `100%`; override with `BARK_E2E_WORKERS=N`). Global test timeout is 300s. Each test registers its own unique user and creates its own workspace, so tests are fully isolated.

The test server spawns the bark server on a non-default port (18997) with a temp `BARK_DATA_DIR` so it doesn't conflict with a running dev server. Flutter Web renders to canvas, so UI interaction uses coordinate-based clicks on `<flutter-view>`. LLM-dependent tests require `OLLAMA_API_KEY`, `OLLAMA_BASE_URL`, and `OLLAMA_MODEL` in `.env` or the process environment.

**Frontend unit tests** (Dart/Flutter, no browser required):

```bash
# Run all frontend tests (devenv script)
devenv shell -- test-frontend

# Run a single test file
devenv shell -- test-frontend test/agui_events_test.dart
```

Tests cover agui events, agui client, auth service, chat panel, container terminal, file upload (conflict detection, upload paths, auth headers, error handling, directory flattening), file viewer panel (navigation, breadcrumbs, refresh), IDE layout (tabs, dividers, IndexedStack), login page, output panel, tool plugin registry, workspace list page (CRUD dialogs, loading/error states), and bark logo. Every test has at least one assertion. Browser-only APIs (`dart:html`, `dart:js_interop`) are abstracted via conditional imports (`web_helpers_stub.dart`/`web_helpers_web.dart`) so tests run in VM mode without a browser.

### Pre-commit Hooks

Pre-commit hooks run automatically on `git commit` via [git-hooks.nix](https://github.com/cachix/git-hooks.nix):

- **ruff check --fix** — Python linting with auto-fix
- **ruff format** — Python formatting
- **dart format** — Dart formatting
- **nixfmt** — Nix formatting
- **prettier** — TypeScript, JavaScript, and YAML formatting
- **yamllint** — YAML linting

Hooks are installed automatically when entering the devenv shell.

### CI

GitHub Actions run automatically on PRs and pushes to main (all also support `workflow_dispatch` for manual triggering):

- **Backend tests** (`.github/workflows/backend-tests.yml`) — triggered by changes to `src/backend/` or `pytest.ini`
- **Frontend tests** (`.github/workflows/frontend-tests.yml`) — triggered by changes to `src/frontend/lib/`, `src/frontend/test/`, or `src/frontend/pubspec.yaml`. Uses `stub_dart_plugins.sh` to create a minimal `bark_plugins` package so `flutter pub get` works without the full plugin codegen.
- **E2E tests** (`.github/workflows/e2e-tests.yml`) — runs hourly via cron (skips if no commits in the last hour) and on manual `workflow_dispatch`. Requires `OLLAMA_API_KEY`, `OLLAMA_BASE_URL`, and `OLLAMA_MODEL` secrets. Runs Playwright against Chromium, Firefox, and WebKit sequentially (browsers from `playwright-driver.browsers` in nixpkgs, one worker per CPU core). Warms up Ollama before each browser run. Uploads test results and per-run backend logs as artifacts on failure.

### Plugin System

All plugins live in `$BARK_PLUGINS_DIR/<name>/` directories. A plugin can contain:

- `extension.ts` — Pi extension with `pi.registerTool()`. Copied to `src/dockerimage/extensions/` at build time.
- `dart/` — Optional Dart package for client-side tools:
  - `dart/pubspec.yaml` — Package definition, depends on `bark_plugin_api` (git)
  - `dart/lib/plugin.dart` — Class extending `ToolPlugin` with action handlers
  - `dart/lib/*.dart` — Supporting Dart files (widgets, utilities)
- `tools/` — Server-side scripts. Everything in this subdirectory is copied to `/opt/bark/plugin-tools/<name>/` in the Docker image.

A plugin needs at minimum an `extension.ts`. The `dart/` subdirectory is only needed for client-side tools that delegate execution to the browser via `ctx.ui.input("HOST_TOOL_REQUEST", ...)`.

**Build integration:**

- `scripts/import_dart_plugins.py` scans `$BARK_PLUGINS_DIR/*/dart/` for plugin Dart packages and generates `$BARK_PLUGINS_DIR/.dart/` (the `bark_plugins` package with path deps and `createAllPlugins()`)
- `dockerbuild` stages `extension.ts` and `tools/` files from all plugins into `$BARK_PLUGINS_DIR/.docker/` and passes them via named Docker build contexts (`plugin-extensions`, `plugin-tools`)
- `flutterbuildweb` runs the codegen before compiling
- `stub_dart_plugins.sh` creates a minimal stub at `$BARK_PLUGINS_DIR/.dart/` so `flutter pub get` works before plugins are fetched (runs automatically at devenv shell startup via `enterShell`; skips if `pubspec_overrides.yaml` already exists)
- Both build tasks are triggered automatically by `devenv up` via `execIfModified`

**Adding a plugin:**

For local development, create files directly in `$BARK_PLUGINS_DIR`:

1. Create `$BARK_PLUGINS_DIR/<name>/extension.ts` with `pi.registerTool()`
2. For client-side tools, add `dart/pubspec.yaml` (depends on `bark_plugin_api`) and `dart/lib/plugin.dart` extending `ToolPlugin`
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
- Workspace files: `workspaces/<user-id>/work/<workspace-id>/` (mounted as `/work`)
- Persistent home: `workspaces/<user-id>/home/<workspace-id>/` (mounted as `/home/bark` — dotfiles, bash history, Pi sessions)
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

Soliplex has its own Bark plugins currently hosted on the `bark-integration` branch of the Soliplex repository within `bark-plugin`.

The Soliplex tools run entirely in the browser, which has the user's Soliplex authentication cookies. When deployed behind nginx on the same domain, the browser can call Soliplex APIs directly with no CORS issues. Set `SOLIPLEX_URL` in `.env` to tell the frontend where Soliplex is (served via the `/api/config` endpoint). Leave it empty when Bark and Soliplex share the same origin (the typical nginx setup). Cross-origin setups require CORS configuration on the Soliplex side.

The query flow: frontend creates a thread in the Soliplex room, posts the user's question as an AG-UI `RunAgentInput`, collects the streamed SSE response, extracts `TEXT_MESSAGE_CONTENT` deltas, and returns the assembled text to the Pi extension.

- **soliplex_list_rooms** (external, via `plugins.yaml`): Lists available Soliplex knowledge base rooms
- **soliplex_query** (external, via `plugins.yaml`): Queries a Soliplex room via AG-UI (creates thread, posts question, collects SSE response). Default room: `search`

The devenv.nix runs nginx as the primary access point:

```
nginx reverse proxy (port 8995)
    ├── /hosted/{ws_id}/{port}/ → container port (direct proxy)
    └── /                       → Bark backend (port 8997)
```

On arctor (production), the external nginx handles the `/bark/` subpath:

```
arctor nginx (443)
    ├── /bark/hosted/{ws_id}/{port}/ → container port (direct proxy)
    └── /bark/                       → bark nginx (port 8995)
                                         └── / → uvicorn (port 8997)
```

## TODO

- **Local files pane**: Add a browser-side file pane where users can upload files into an in-browser-memory filesystem (e.g., using the File System Access API or an in-memory store). These files would be accessible to client-side plugins and could be passed to the REPL as context without uploading to the server. Useful for working with sensitive files that shouldn't leave the browser, or for quick one-off analysis without persisting to the workspace.

- **Pi extensions as Bark plugins**: Allow standard Pi extensions to be used as Bark plugins. This has two sides: (1) wrapping existing Pi extensions in the Bark plugin format (`plugin.yaml`, git-fetchable) so they can be installed via `plugins.yaml` and staged into containers automatically, and (2) letting users install arbitrary Pi extensions directly (e.g., from the Pi extension ecosystem) without needing to create a full Bark plugin wrapper. Investigate what minimal adapter or convention would bridge the gap — Pi extensions are TypeScript files loaded via `--extension`, while Bark plugins add metadata, optional client-side UI, and server-side tools on top.
- **Plugin version numbers**: Plugins may want their own version numbers (in `plugin.yaml` or similar metadata) for compatibility checking, display in the UI, and meaningful pinning beyond git refs.
- **Entrypoint nohup zombie**: The `nohup sh -c "..."` FIFO writer in `entrypoint.sh` leaves one `[rm] <defunct>` or `[sh] <defunct>` zombie per container start. Its parent is `su` (PID 7) which doesn't call `wait()`. Tini (PID 1, via Docker `Init: true`) can't reap it because it's not a direct child — it's a child of `su`. Harmless but cosmetic. Attempted fixes: (1) double-fork — didn't work; (2) `exec rm` on last command — zombie was `[sh]` not `[rm]`, still zombied; (3) removing `rm` calls — left stuck process and didn't clean up FIFOs; (4) `exit 0` as last statement — still zombied; (5) backgrounding `su` + `wait` — broke `docker attach` stdin/stdout pipe that `pi_rpc_client.py` needs. Root cause: `nohup` is required (writer dies without it before Pi reads FIFOs), but `exec su` replaces the shell so it can't reap the nohup child, and `su` doesn't reap either. Needs a fundamentally different approach — possibly a compiled helper that forks+writes+exits cleanly, or replacing `su` with something that reaps children.
- **Container resource limits**: Add CPU/memory limits to containers to prevent runaway processes.
- **Container network isolation**: Restrict container network access to prevent use as an attack platform. Use a custom Docker network with limited egress — allow only the Ollama API endpoint (cloud or self-hosted) and block all other outbound traffic. Consider using `--network=none` with a proxy sidecar for allowlisted domains only.
- **Syntax highlighting language detection**: Improve code block language detection for unlabeled blocks.
- **Terminal focus requires precise click target**: In Firefox, the terminal only receives keyboard focus when clicking very specific areas of the terminal pane. Clicking elsewhere in the pane does nothing — the cursor doesn't appear and typing has no effect. The entire terminal pane should grant focus on click.
- **Simplify container-down UX**: Investigate whether the per-panel "restart" overlays (terminal, chat) are the right approach when the container goes down, or if a single full-page reload overlay would be simpler and more reliable. Currently each panel independently detects `container_stopped` and shows its own restart button — a page-level overlay triggered by the WebSocket could avoid duplicated state and edge cases.
- **Audit container env vars**: Investigate whether `BARK_PORT_MAPPINGS`, `BARK_WORKSPACE_ID`, `BARK_HOSTING_PROTOCOL`, and `BARK_HOSTING_HOSTNAME` need to be in the container environment. If they're only used by the entrypoint or Pi extensions, they may be safe to strip from the terminal session via `env -u`. If they're not needed at all, stop passing them at container creation time.
- **Same-workspace multi-window**: Opening the same workspace in two browser windows simultaneously has undefined behavior — both WebSocket connections share one Pi container/session, and prompts from either window could collide or interleave unpredictably. Consider either locking a workspace to one connection at a time, or multiplexing both windows onto the same event stream.
- **Workspace disk quotas**: Limit how much disk space each workspace can consume. Options: use filesystem quotas (XFS/ext4 project quotas on the host), overlay2 with size limits, or a loopback-mounted filesystem per workspace with a fixed size. Should also surface current disk usage in the UI (file viewer header or workspace list) so users can see how much space they've used.
- **Process state checkpoint/restore across container restarts**: Research checkpointing running process state before container shutdown and restoring it on restart, so long-running servers, REPLs, and background tasks survive idle timeouts. Options: [CRIU](https://criu.org/) (Checkpoint/Restore In Userspace) can freeze and restore Linux processes including open files, sockets, and memory; Docker has experimental `docker checkpoint` support built on CRIU. Trade-offs: CRIU requires `CAP_SYS_ADMIN` or a privileged container, restored processes may fail if ports are remapped or network state changed, and checkpoint images can be large. Lighter alternatives: save enough state to re-run setup scripts on restart (virtualenvs, npm installs, server start commands).
- **Test workspace_page.dart, app.dart, and main.dart**: `workspace_page.dart` depends on the `bark_plugins` package (generated by `import_dart_plugins.py`), so tests require codegen to have run first. `app.dart` needs GoRouter/navigation mocking.
- **E2E: use Flutter semantics tree for testability**: Enabling `RendererBinding.instance.ensureSemantics()` exposes real DOM elements (`<input aria-label="Email">`, `<flt-semantics role="button">Login</flt-semantics>`) that Playwright can interact with via standard selectors instead of coordinate clicks. However, the semantics overlay intercepts keyboard events globally, which breaks xterm.dart (terminal input never reaches the canvas). Investigated `ExcludeSemantics` on the terminal widget and on-demand enable/disable via the "Enable accessibility" button — both still break the terminal. Need a way to selectively disable the semantics overlay for the terminal area, or find an alternative approach to make Flutter canvas apps testable without coordinates.
- **Remove /auth/register endpoint**: With email verification, user creation in production goes through the admin endpoint (`/admin/users`). The `/auth/register` endpoint may no longer be needed — its only remaining use is the `BARK_TEST_MODE` bypass for E2E tests. Consider moving the test-mode registration to the admin endpoint or a dedicated test-only route.
- **BARK_SMTP_PASSWORD_PATH**: Support `BARK_SMTP_PASSWORD_PATH` as an alternative to `BARK_SMTP_PASSWORD`, pointing to a file containing the SMTP password. This avoids storing secrets in `.env` and works with secret management tools like agenix/sops that write decrypted secrets to files at runtime.
- **Test SMTP email delivery**: The email service has unit tests with mocked SMTP, but no integration test for real SMTP delivery. Add a manual test script or a health check endpoint that sends a test email via the configured SMTP/sendmail to verify the mail pipeline works end-to-end. Manually test SMTP mode on arctor by setting `BARK_SMTP_HOST=localhost` in `.env` (arctor's Postfix accepts `permit_mynetworks` without auth for localhost connections).
- **Forgot password flow**: Add a "Forgot password?" link on the login page that sends a password reset email with a signed token link. The reset page lets the user set a new password. Similar to the verification flow — JWT-signed token, 72h expiry, auto-login after reset.
- **Resend verification email**: If the verification email is lost in transit or expires, users need a way to request a new one. Add a "Resend verification email" link on the login page (shown when login fails with "not verified") and/or an admin action to resend. Consider rate-limiting to prevent abuse.
- **Self-service user settings**: Allow users to change their own email and password without admin involvement. Add a settings page accessible from the app bar. Email changes should require re-verification (send a confirmation link to the new address). Password changes should require the current password.
- **Clipboard image paste in chat**: Investigate whether Pi supports image inputs and, if so, allow pasting images from the clipboard into the chat input field. Would need to intercept paste events, detect image MIME types, convert to a format Pi can accept (base64 or URL), and pass via the `images` parameter of `prompt()`.

- **Hosted app URLs should respect external headers**: The `get_hosted_url` tool generates URLs using `BARK_HOSTING_*` env vars passed to the container. These are derived from `X-Forwarded-Host`/`X-Forwarded-Proto` headers at WebSocket connect time, but if the hosting environment changes (e.g., different reverse proxy), the container's cached values become stale. Consider re-deriving hosting info on each `get_hosted_url` call or providing a mechanism to update the container's env vars without restart.
- **Backend busy loop during Pi/WS comms**: The backend occasionally goes into a busy loop during Pi RPC or WebSocket communication. Investigate and fix the root cause.
- **E2E: use UI instead of API for setup/teardown**: E2E tests currently use the API directly to upload files, delete workspaces, etc. These should use the UI instead to better test real user flows and catch UI-level regressions.

- **E2E: end-to-end hosted app test**: Add a test that asks the LLM to create an app (e.g., a pong game) and serve it, then verifies the hosted URL returns a 200 response. Currently the hosted URL test only checks that `get_hosted_url` returns a URL — it doesn't verify a real server is running behind it. This test would exercise the full flow: LLM writes code → starts server on a mapped port → `get_hosted_url` returns the URL → HTTP GET on the URL succeeds. May need a longer timeout and a simpler app prompt to be reliable.
- **devenv MCP memory leak**: The `devenv mcp` process grows to ~18GB virtual memory over time. Investigate root cause — may be in devenv itself or in the MCP server implementation.
- **Production Flutter asset serving**: Create a command/script that produces a directory of pre-built Flutter web assets ready to be served in production without running the build. The current `flutterbuildweb.sh` appends a content hash query string (`?v=<hash>`) to `flutter_bootstrap.js` in `index.html` for cache-busting, but this only works for the dev build. Production deployments (e.g., arctor) copy files rather than running the build, and need the same cache-busting applied.
- **Integrate Pydantic Logfire for AI observability**: Add [Logfire](https://logfire.pydantic.dev/) to the backend for tracing LLM interactions. Instrument the Pi RPC call path (prompts, tool calls, streaming events, latency) and AG-UI event translation so AI request/response flows are visible in the Logfire dashboard. Logfire integrates with OpenTelemetry and has first-class support for FastAPI auto-instrumentation. Add `logfire` to `pyproject.toml`, configure via `LOGFIRE_TOKEN` env var.
- **Containerize the backend**: Create a Docker image for the Bark backend (FastAPI + Flutter web assets + nginx) for production deployment. Currently the backend runs directly via devenv/uvicorn — a container image would simplify deployment, versioning, and scaling. The deployment platform must support Docker-in-Docker (or bind-mount the host Docker socket) since Bark spawns workspace containers via the Docker API.
- **SQLite migration tooling**: Research SQLite migration tools (e.g., Alembic, yoyo-migrations) for when dropping and recreating the database is no longer feasible. Currently schema changes are handled by `CREATE TABLE IF NOT EXISTS` with manual `ALTER TABLE` fallbacks, which won't scale as the schema evolves.
- **E2E: cache Docker image build in CI**: The Docker image is rebuilt from scratch on every CI run. Options: (A) Push to GHCR (`ghcr.io/<repo>/bark-pi:<hash>`) keyed on a hash of Dockerfile + entrypoint + system-prompt + builtin-extensions + plugins — pull if exists, build and push if not. Requires `packages: write` permission but handles large images well. (B) Use GitHub Actions cache with `docker save`/`docker load` — simpler, no registry auth, but the 10 GB total cache limit is tight for a ~2-3 GB compressed tar. (C) Modify `dockerbuild.sh` to check a registry when `BARK_DOCKER_REGISTRY` is set — works for CI and anyone with registry access but couples the build script to a registry. Cache key should hash: `src/dockerimage/Dockerfile`, `src/dockerimage/entrypoint.sh`, `src/dockerimage/*.md`, `src/dockerimage/builtin-extensions/*.ts`, and `plugins/` contents.
