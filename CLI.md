# Bark CLI — Containerized Development Shell

## Context

Today Bark is consumed exclusively through the Flutter web UI. You want to use Bark's container isolation for your normal development workflow — dropping into a shell inside a Bark-managed container to work on projects like Bark itself. The CLI should work both locally and against a remote Bark server (e.g., from a coffee shop connecting to arctor).

The primary interaction is a **terminal shell** (bash) inside the container. Pi is available inside the container as a tool you can invoke from the shell, but the CLI itself doesn't manage Pi — you just run `pi` when you want it.

## Broader Direction: Terminals as the Universal Interface

The Flutter web UI is moving toward replacing Pi chat bubbles with an xterm-based Pi REPL pane. This means both the web UI and the CLI converge on the same model: **everything is a terminal session**. The web UI will have multiple terminal panes (a dedicated Pi pane that auto-starts `pi` in interactive mode, plus general shell panes), while the CLI provides a single terminal session.

This validates the CLI design — no AG-UI/RPC rendering needed in the CLI. The AG-UI/RPC layer will remain as an option in the web UI but the xterm approach is the new primary.

Implications for the backend:

- `terminal_manager.py` will need to support **multiple concurrent terminal sessions** per workspace (separate work, needed for the web UI refactor, not for the CLI)
- Pi can run in normal interactive mode (not `--mode rpc`) when used via terminal panes
- A future `bark pi` CLI command could auto-launch Pi in the shell (like the web UI's dedicated Pi pane) but isn't needed for phase 1

## Approach

A `bark` CLI that lives inside the existing backend package as a thin client. It talks to the running Bark backend over HTTP + WebSocket — same infrastructure the Flutter UI uses. The shell session uses the backend's existing `terminal_manager.py` (WebSocket → `docker exec` PTY).

No backend changes are needed for phase 1 — the CLI uses the existing WebSocket terminal protocol as-is. Workspaces use the default data directory mounts.

## Package Structure

```text
src/backend/bark_backend/cli/
    __init__.py
    main.py          # typer app, command group
    auth.py          # login/logout, token storage
    client.py        # HTTP + WebSocket client wrapper
    config.py        # CLI config (~/.config/bark/cli.toml)
```

Add to `pyproject.toml`:

- Dependencies: `typer[all]>=0.12.0` (brings rich, click)
- Entry point: `bark = "bark_backend.cli.main:app"`

## Commands

| Command                     | Description                                                                  |
| --------------------------- | ---------------------------------------------------------------------------- |
| `bark login [--server URL]` | Prompt for email/password, store JWT in `~/.config/bark/cli.toml`            |
| `bark logout`               | Clear stored token                                                           |
| `bark workspaces`           | List workspaces (GET /workspaces)                                            |
| `bark create NAME`          | Create a workspace                                                           |
| `bark delete NAME`          | Delete a workspace                                                           |
| `bark shell [WORKSPACE]`    | **Main command.** Connect to workspace, drop into bash inside the container. |
| `bark status`               | Show connection info (server, user, active workspaces)                       |

## `bark shell` — The Core Flow

1. Resolve workspace by name via REST API (GET /workspaces, match by name)
2. Open WebSocket to `ws://<server>/ws?token=<jwt>`
3. Send `workspace_connect` with `workspaceId`
4. Wait for `workspace_ready` response
5. Send `terminal_start` with current terminal dimensions (`os.get_terminal_size()`)
6. Put local terminal in raw mode (`tty.setraw` / `termios`)
7. Run two concurrent tasks:
   - **stdin → WebSocket**: read local stdin, send as `terminal_input` messages
   - **WebSocket → stdout**: receive `terminal_output` messages, write to local stdout
8. Handle `SIGWINCH` → send `terminal_resize` with new dimensions
9. On exit (Ctrl+D, connection drop): restore terminal, send `terminal_stop`, close WebSocket

This reuses the existing `terminal_manager.py` backend code that the Flutter web UI's terminal tab already uses. No new backend terminal infrastructure needed.

### Terminal correctness

`bark shell` must be a well-behaved terminal program that works inside tmux, herdr, cmux, and similar orchestrators. This means:

- Proper raw mode setup and teardown (always restore terminal state on exit, including on crashes/signals)
- Correct SIGWINCH propagation (resize events must reach the container PTY)
- Clean exit codes (0 on normal exit, non-zero on error)
- No interference with the parent terminal's state (no leftover escape sequences)
- Transparent pass-through of all control sequences (colors, cursor movement, alternate screen buffer, etc.)
- Handle SIGINT/SIGTERM gracefully — clean up and exit, don't leave the terminal broken

## Config Storage

`~/.config/bark/cli.toml`:

```toml
[server]
url = "http://localhost:8997"

[auth]
token = "eyJ..."
email = "admin"
```

## Implementation Phases

### Phase 1 (this work): CLI with shell access

- Create `cli/` package: `__init__.py`, `config.py`, `auth.py`, `client.py`, `main.py`
- Add typer dependency + `[project.scripts]` entry to `pyproject.toml`
- `bark login`, `bark logout`, `bark status`
- `bark workspaces`, `bark create`, `bark delete` (HTTP client via httpx)
- `bark shell` — WebSocket terminal with raw mode, stdin/stdout forwarding, SIGWINCH
- Tests for all of the above
- No backend changes needed

### Phase 2 (future): Host path mounting

- Backend change: accept `hostPath` in `workspace_connect` (admin-only)
- `--mount PATH` flag on `bark shell`
- Local mount: path on same machine as Docker
- Remote mount: path on server filesystem

### Phase 3 (future): SSH access

- Add SSH server to container image
- Expose SSH port via port allocation
- `bark ssh` command or direct `ssh` with key management
- Near-native terminal responsiveness

### Phase 4 (future): Custom Docker images

- Allow specifying a Docker image per user and/or per workspace
- Currently hardcoded to `bark-pi` image via `IMAGE_NAME` in `container_manager.py`
- Store image preference in workspace/user DB records
- Enables different toolchains (e.g., Rust image, Go image, data science image)
- CLI impact: add `--image` flag to `bark create`, no other commands change
- Note: Phase 1 CLI commands are pure REST/WebSocket clients with no Docker or image references, so no refactoring needed

### Phase 5 (future): Local Docker exec optimization

- Detect when backend is local
- Use `docker exec` directly instead of WebSocket PTY for native performance
- Fall back to WebSocket for remote

## Key Files to Modify

| File                                  | Change                                   |
| ------------------------------------- | ---------------------------------------- |
| `src/backend/pyproject.toml`          | Add typer dep, `[project.scripts]` entry |
| `src/backend/bark_backend/cli/` (new) | All CLI code                             |

## Key Files to Reference

- `src/backend/bark_backend/terminal_manager.py` — PTY session over `docker exec`, the backend half of what `bark shell` connects to
- `src/backend/bark_backend/ws_handler.py` — WebSocket message handling, `terminal_start`/`terminal_input`/`terminal_output`/`terminal_resize`/`terminal_stop` protocol
- `src/frontend/lib/agui/agui_client.dart` — WebSocket command shapes (reference for client.py)
- `src/frontend/lib/terminal/container_terminal.dart` — Flutter terminal implementation (reference for how the web UI does it)

## Verification

1. `devenv shell -- test-backend` — all existing + new tests pass
2. Start Bark normally (`devenv up`), then in another terminal:
   - `bark login --server http://localhost:8997` with admin/admin
   - `bark workspaces` — lists workspaces
   - `bark create cli-test` — creates a workspace
   - `bark shell cli-test` — drops into bash inside container
   - Verify: `ls /work` shows workspace files, `git status` works, `pi` is available
   - Ctrl+D exits cleanly, terminal is restored
   - `bark delete cli-test` — cleans up
3. Resize terminal window during `bark shell` — verify the shell adapts
