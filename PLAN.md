# Bark — Multi-User Pi Coding Agent Web REPL

## Overview

Bark is a multi-user web app that gives each user their own isolated Pi coding agent (pi.dev) running in a Docker container. Users authenticate with a simple login, can create multiple named workspaces, and interact with Pi through an IDE-like split-pane UI with a chat interface, file viewer, and debug panel.

## Architecture

```
Browser (Flutter Web + Chat UI + AG-UI)
    ↕ AG-UI events over WebSocket (authenticated)
Python/FastAPI backend (port 8997, serves API + frontend)
    ├── Auth (JWT sessions, SQLite user store)
    ├── Workspace registry (user → [workspace] → container)
    ├── Pi-to-AG-UI translator (Pi RPC events → AG-UI events)
    ├── Message history (SQLite)
    ↕ docker attach subprocess
Pi container per workspace (stdin/stdout JSON-RPC)
    ↕ bind mount
$DEVENV_STATE/.bark/workspaces/<user-id>/<workspace-name>/
```

### Components

- **Backend** (`backend/`): Python/FastAPI — auth, workspace management, Docker containers, Pi RPC → AG-UI translation, message history
- **Frontend** (`frontend/`): Flutter Web — chat-style interface with markdown rendering, syntax-highlighted code blocks, file viewer, debug panel
- **Docker** (`docker/`): Custom Dockerfile for Pi agent containers with Python3, Node.js, Dart, Flutter, Rust, build-essential

### Key Technologies

- **AG-UI Protocol**: Standardized agent-user interaction protocol for event streaming
- **Pi Coding Agent**: Minimal terminal coding harness (pi.dev) running in RPC mode with native session persistence
- **Ollama**: LLM provider — supports both Ollama Cloud and self-hosted instances, configurable model via `OLLAMA_MODEL` env var
- **devenv**: Nix-based development environment with auto-setup

## Project Structure

```
bark/
  devenv.nix                    # Dev environment: Python (uv), Flutter, Docker CLI
  devenv.yaml                   # devenv inputs
  .envrc                        # direnv integration
  .env                          # API keys (OLLAMA_API_KEY, BARK_DEFAULT_USER/PASSWORD)
  .gitignore
  README.md
  PLAN.md

  docker/
    Dockerfile                  # Pi agent image: node:22-slim + Pi + Python3 + Dart + Flutter + Rust + build-essential
    models.json                 # Ollama provider config (generated from env vars at startup)
    settings.json               # Default model selection
    entrypoint.sh               # Injects API key, copies AGENTS.md, starts Pi in RPC mode
    AGENTS.md                   # Default agent instructions (write files, run code, test before reporting)

  backend/
    pyproject.toml              # Python deps: fastapi, aiodocker, aiosqlite, bcrypt, python-jose
    backend/
      main.py                   # FastAPI app, lifespan, routes, default user seeding
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
      utils/page_title.dart     # Browser tab title updates
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
- JWT tokens (24hr expiry) with token blocklist for logout
- Default user auto-seeded on startup (configurable via BARK_DEFAULT_USER/PASSWORD in .env)
- Session persists across page reloads (async token loading before routing)

### Workspaces
- Multiple workspaces per user
- Each workspace gets its own Docker container + bind-mounted directory
- URL-based workspace routing (survives page reload via hash URL reading)
- Workspace name shown in app bar and browser tab title
- Containers stop when navigating away (browser back, in-app back, logout)
- Containers auto-restart transparently when user sends next prompt

### Pi Agent Integration
- One Docker container per workspace running Pi in RPC mode
- Container communicates via stdin/stdout JSON-RPC (docker attach subprocess)
- Pi RPC events translated to AG-UI events in real-time
- Native Pi session persistence (JSONL files in workspace `.pi/sessions/`)
- Session resume on reconnect via `switch_session` RPC command
- 5 TCP ports allocated per workspace (9000-9004, 9005-9009, etc.) for user apps
- API keys passed via environment variables
- 15-minute idle timeout with automatic container stop and debug notification
- All user containers stopped on logout and backend shutdown
- AGENTS.md instructs Pi to write files directly and test code before reporting

### Chat Interface
- Markdown rendering for assistant responses (flutter_markdown)
- Syntax-highlighted code blocks (Monokai Sublime theme, highlight.dart, JetBrains Mono)
- Collapsible tool call cards showing arguments and results
- Streaming indicator while agent is thinking
- Enter to send, Shift+Enter for newline
- Abort button (red when agent running)
- Conversation history persisted to SQLite and restored on workspace reload

### File Viewer
- Directory tree with file sizes
- Click to view file contents (16pt JetBrains Mono, left-aligned)
- Auto-refresh when Pi writes/edits files or runs file-creating bash commands
- Drag-and-drop file upload

### Debug Panel
- Container lifecycle events (starting, ready with port info, idle stop, restart)
- Query text shown for each prompt sent
- Tool call entries from Pi
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

## Development

### Prerequisites
- Nix with devenv installed
- Docker daemon running

### Setup & Run
```bash
# Enter dev environment (auto-installs deps, builds Docker image)
devenv shell

# Start backend + frontend
devenv up

# Open in browser
open http://localhost:8997

# Default login: admin/admin (configurable via .env)
```

### Ports
- `8997`: Web UI + API (single FastAPI/uvicorn server)
- `9000+`: User app ports (5 per workspace)

### Rebuild
```bash
# Full rebuild (Docker + Flutter)
rebuild

# Or manually:
docker build -t bark-pi docker/
cd frontend && flutter pub get && flutter build web
```

### Data
- All data stored in `$DEVENV_STATE/.bark/`
- SQLite database: `bark.db` (users, workspaces, messages, token blocklist)
- Workspace files: `workspaces/<user-id>/<workspace-name>/`
- Pi sessions: `workspaces/<user-id>/<workspace-name>/.pi/sessions/`
- Database persists across restarts and rebuilds

## Client-Side Tools (Planned)

Pi's RPC mode supports **host tools** — tools registered by the RPC client that the LLM can call, with execution delegated back to the caller. This allows the Flutter frontend to handle certain tasks locally in the browser:

**Architecture**: Frontend registers tools via `set_host_tools` RPC command. When the LLM calls a host tool, Pi sends `host_tool_call` back through the backend to the frontend. The frontend executes the tool in Dart and returns the result via `host_tool_result`.

**Initial tools**:
- `analyze_csv` — Parse CSV, return column names, row count, data types, basic statistics
- `validate_json` — Validate and summarize JSON structure
- `count_lines` — Line/word/character count

**Benefits**: Much faster than sending large files to the LLM. A 100KB CSV takes seconds locally vs minutes through the LLM.

## TODO

- **Client-side tools**: Implement the host tool delegation architecture described above.
- **Stop running Pi as root**: Create a non-root user (e.g., `bark`) in the Dockerfile, set ownership of `/workspace` and `/opt/*` to that user, and use `USER bark` before the entrypoint. This improves security and prevents files created by Pi from being owned by root on the host bind mount.
- **Read-only root filesystem**: Use `--read-only` Docker flag to make the container's root filesystem unwritable. Only `/workspace` (bind mount) and necessary tmpfs mounts (`/tmp`, `/root/.pi`) should be writable. This prevents the agent from modifying system files or installing packages outside the workspace.
- **Container resource limits**: Add CPU/memory limits to containers to prevent runaway processes.
- **Multiple LLM providers**: Support selecting different models per workspace.
- **Syntax highlighting language detection**: Improve code block language detection for unlabeled blocks.
