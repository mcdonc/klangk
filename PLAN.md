# Bark — Multi-User Web Coding Agent

## Overview

Bark is a multi-user web app that gives each user their own isolated Pi coding agent (pi.dev) running in a Docker container. Users authenticate with a simple login, can create multiple named workspaces, and interact with Pi through a split-pane UI with a chat interface, file viewer, and debug panel.

## Architecture

```
Browser (Flutter Web + Chat UI + AG-UI)
    ↕ AG-UI events over WebSocket (authenticated)
Python/FastAPI backend (port 8997, serves API + frontend static files)
    ├── Auth (JWT sessions, SQLite user store)
    ├── Workspace registry (user → [workspace] → container)
    ├── Pi-to-AG-UI translator (Pi RPC events → AG-UI events)
    ├── Message history (SQLite)
    ↕ docker attach subprocess
Pi container per workspace (stdin/stdout JSON-RPC)
    ├── Pi extensions (TypeScript tools: word_count, pig_latin, etc.)
    ├── Server-side tools (Python scripts in /usr/local/bin/bark-tools/)
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
- **devenv**: Nix-based development environment with auto-setup

## Project Structure

```
bark/
  devenv.nix                    # Dev environment: Python (uv), Flutter, Docker CLI
  devenv.yaml                   # devenv inputs
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
      widgets/bark_logo.dart    # Bark logo widget (orange paw icon)
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

### Chat Interface
- Markdown rendering for assistant responses (flutter_markdown)
- Syntax-highlighted code blocks (Monokai Sublime theme, highlight.dart, JetBrains Mono)
- Collapsible tool call cards showing arguments and results
- Streaming indicator while agent is thinking
- Enter to send, Shift+Enter for newline
- Abort button (red when agent running)
- Conversation history persisted to SQLite and restored on workspace reload
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
```bash
devenv shell -- rebuild
devenv processes restart
```

### Adding Extension Tools
1. Create a TypeScript file in `docker/extensions/` (see existing examples)
2. Use `pi.registerTool()` with name, description, parameters, and execute function
3. Optionally add a Python helper script in `docker/tools/`
4. Rebuild Docker image: `docker build --platform linux/amd64 -t bark-pi docker/`
5. AGENTS.md will auto-include the tool on next container start

### Data
- All data stored in `$DEVENV_STATE/.bark/`
- SQLite database: `bark.db` (users, workspaces, messages, token blocklist)
- Workspace files: `workspaces/<user-id>/<workspace-name>/`
- Pi sessions: `workspaces/<user-id>/<workspace-name>/.pi/sessions/`
- Database persists across restarts and rebuilds

## Tool Delegation (Research Notes)

Pi's RPC mode supports **host tools** (`set_host_tools`, `host_tool_call`, `host_tool_result`) — tools registered by the RPC client that the LLM can call, with execution delegated back to the caller. We investigated using this to run tools in the Flutter frontend (browser-side Dart).

**Findings**:
- Speed, library access, and reliability are all better with server-side tools in the container.
- Since Pi runs inside the container, the container must be running for any tool call regardless.
- The LLM still needs an inference step to decide which tool to call and to process the result.
- Privacy is limited: files live on the server (uploaded or created by Pi), so client-side processing still requires downloading the file from the server first.
- A local-only analysis mode (file never leaves the browser) would require a different UX that doesn't exist yet.

**Current approach**: Pi extensions (TypeScript) registered as first-class tools, with dynamic AGENTS.md generation listing available tools. Extensions can be pure TypeScript or call Python helper scripts.

**Host tool delegation remains interesting** for future use cases — e.g., local-only file analysis without server upload, browser-native capabilities (clipboard, camera, microphone), or offloading work from resource-constrained containers. The Pi RPC protocol supports it whenever we find the right application.

## TODO

- **Stop running Pi as root**: Create a non-root user (e.g., `bark`) in the Dockerfile, set ownership of `/workspace` and `/opt/*` to that user, and use `USER bark` before the entrypoint. This improves security and prevents files created by Pi from being owned by root on the host bind mount.
- **Read-only root filesystem**: Use `--read-only` Docker flag to make the container's root filesystem unwritable. Only `/workspace` (bind mount) and necessary tmpfs mounts (`/tmp`, `/root/.pi`) should be writable. This prevents the agent from modifying system files or installing packages outside the workspace.
- **Container resource limits**: Add CPU/memory limits to containers to prevent runaway processes.
- **Container network isolation**: Restrict container network access to prevent use as an attack platform. Use a custom Docker network with limited egress — allow only the Ollama API endpoint (cloud or self-hosted) and block all other outbound traffic. Consider using `--network=none` with a proxy sidecar for allowlisted domains only.
- **Multiple LLM providers**: Support selecting different models per workspace.
- **Syntax highlighting language detection**: Improve code block language detection for unlabeled blocks.
- **Folder drag-and-drop upload**: Support dropping entire folders (with contents) into the file pane, preserving directory structure. Requires using the browser's File System Access API or `webkitGetAsEntry()` to traverse directory entries recursively.
- **Container terminal pane**: Add a terminal panel (xterm.dart) that gives the user direct shell access to the workspace container via `docker exec`. Would allow users to run commands, inspect processes, debug code, and interact with running servers without going through the AI agent.
