# Architecture Overview

[![Architecture Overview](../assets/architecture-overview.svg)](../assets/architecture-overview.svg)

```text
Browser (Flutter Web + Terminal + Files + Chat)
    ├── WebSocket (authenticated): terminal I/O, exec, browser bridge, chat, presence, lifecycle events
    ├── Browser delegate: handles bridge requests from Pi extensions (fetch, plugin actions)
    ├── Auto-reconnect with exponential backoff on disconnect
nginx reverse proxy (browser port 8997 = UI + API + hosted app proxy; egress port 8995 = container→host endpoints)
    ↕ LLM proxy: container → host.containers.internal:8995/llm-proxy/ → ${KLANGK_LLM_BASE_URL}
    ↕ auth_request: validates per-workspace JWT on container→host endpoints
    ↕
Python/FastAPI backend (UDS, serves API + frontend static files)
    ├── Auth (JWT sessions, SQLite user store)
    ├── Workspace registry (user → [workspace] → container)
    ├── Browser bridge (/api/v1/browser-delegate → WebSocket → Flutter)
    ├── Chat (messages, @mentions, pagination, message types, container-to-chat REST API)
    ├── Presence (who's connected per workspace, join/leave broadcasts)
    ├── Terminal/exec session management
    ↕ podman exec subprocess
Pi container per workspace (interactive terminal mode)
    ├── Pi extensions (from $KLANGK_PLUGINS_DIR/*/extension.ts)
    ├── AGENTS.md (dynamically generated on container start)
    ├── /tmp/klangk/workspace-token (per-workspace JWT, auto-renewed)
    ↕ bind mount
$KLANGK_DATA_DIR/workspaces/<user-id>/home/<workspace-id>/
```

## Components

- **Backend** (`src/backend/`): Python/FastAPI — single-port server for API, WebSocket, and frontend static files
- **CLI** (`src/cli/klangkc/`): `klangkc` command — typer-based thin client that talks to the backend over HTTP + WebSocket for terminal access to containers
- **Frontend** (`src/frontend/`): Flutter Web — chat (markdown rendering, syntax-highlighted code blocks, @mentions, message types, pagination, history recall), file viewer, debug panel, workspace presence
- **Containers** (`src/containers/`): Custom Dockerfile for Pi agent containers with Python3, Node.js, build-essential, SQLite, vim, emacs, network tools, Pi extensions (built and run via podman)

## Data

- All data stored in `$KLANGK_DATA_DIR` (defaults to `$DEVENV_STATE/klangk/data`)
- SQLite database: `klangk.db` (users, workspaces, groups, ACL entries, port allocations, chat messages, chat mentions, token blocklist, login attempts, invitations)
- Workspace files: `workspaces/<user-id>/home/<workspace-id>/work/` (inside the `/home/klangk` bind mount)
- Persistent home: `workspaces/<user-id>/home/<workspace-id>/` (mounted as `/home/klangk` — dotfiles, bash history, Pi sessions)
- Database persists across restarts and rebuilds
