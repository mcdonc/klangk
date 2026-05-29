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

| Command                                                           | Description                                                                      |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------- |
| `bark login [EMAIL] [--server URL] [--password-file FILE]`        | Authenticate. Reuses saved token if valid. `--password-file -` reads from stdin. |
| `bark logout`                                                     | Clear stored token                                                               |
| `bark status [--plain]`                                           | Show connection info (server, user, login status)                                |
| `bark list [--plain]`                                             | List workspaces                                                                  |
| `bark create NAME [--mount SRC:DST] [--env KEY=VALUE]`            | Create a workspace (with optional mounts and env vars)                           |
| `bark edit NAME [--name] [--image] [--command] [--mount] [--env]` | Edit workspace settings (flags mode or interactive)                              |
| `bark dup SOURCE NEW_NAME`                                        | Duplicate a workspace (copies image, command, mounts, env)                       |
| `bark rm NAME`                                                    | Delete a workspace                                                               |
| `bark shell [WORKSPACE]`                                          | **Main command.** Connect to workspace, drop into bash inside the container.     |
| `bark exec WORKSPACE COMMAND...`                                  | Run a command in a container. Also usable as an rsync transport.                 |
| `bark sync SRC DEST`                                              | Sync files to/from a container via rsync (wraps `bark exec`).                    |
| `bark volumes ls [--plain]`                                       | List bark-managed Docker volumes                                                 |
| `bark volumes create NAME`                                        | Create a named Docker volume                                                     |
| `bark volumes rm NAME`                                            | Delete a named Docker volume                                                     |

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
url = "http://localhost:8995"

[auth]
token = "eyJ..."
email = "admin@example.com"
```

## Implementation Phases

### Phase 1 (done): CLI with shell access

- `cli/` package: `__init__.py`, `config.py`, `auth.py`, `client.py`, `main.py`
- typer dependency + `[project.scripts]` entry in `pyproject.toml`
- `bark login [EMAIL]`, `bark logout`, `bark status [--plain]`
- `bark list [--plain]`, `bark create`, `bark rm` (HTTP client via httpx)
- `bark shell` — WebSocket terminal with raw mode, stdin/stdout forwarding, select-based interruptible stdin, SIGWINCH via polling
- `--password-file` for non-interactive login (scripting)
- Token reuse: `bark login` verifies saved token before prompting
- Rich output: styled tables, colored error messages
- 100% test coverage, no global stdin/stdout mutation in tests

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
- Currently hardcoded to `bark` image via `IMAGE_NAME` in `container.py`
- Store image preference in workspace/user DB records
- Enables different toolchains (e.g., Rust image, Go image, data science image)
- CLI impact: add `--image` flag to `bark create`, no other commands change
- Note: Phase 1 CLI commands are pure REST/WebSocket clients with no Docker or image references, so no refactoring needed

### Phase 5 (done): Exec, sync, and E2E tests

- `bark exec WORKSPACE COMMAND...` — raw command execution in containers via WebSocket (`ExecSession` with piped stdin/stdout, no PTY)
- `bark sync SRC DEST` — rsync wrapper using `bark exec` as the transport
- Uses `os.read(0)`/`os.write(1)` for unbuffered I/O (Python's buffered I/O breaks rsync pipe protocol)
- `select()` with timeout in stdin_forward for prompt exit
- Base64 encoding for binary-safe WebSocket transport
- Container image includes rsync
- CLI E2E tests (`src/backend/e2e-tests/`) — 16 tests against real server + Docker containers
- GitHub Actions workflow for CI

### Phase 6 (future): Local Docker exec optimization

- Detect when backend is local
- Use `docker exec` directly instead of WebSocket PTY for native performance
- Fall back to WebSocket for remote

## Key Files

| File                                           | Purpose                                        |
| ---------------------------------------------- | ---------------------------------------------- |
| `src/backend/bark_backend/cli/main.py`         | Typer app, top-level + `ws` subcommand group   |
| `src/backend/bark_backend/cli/auth.py`         | Login/logout with token reuse and rich prompts |
| `src/backend/bark_backend/cli/client.py`       | HTTP + WebSocket client, shell/exec forwarding |
| `src/backend/bark_backend/cli/config.py`       | Config storage (~/.config/bark/cli.toml)       |
| `src/backend/bark_backend/exec_session.py`     | Backend raw exec session (no PTY)              |
| `src/backend/bark_backend/terminal_manager.py` | Backend PTY session (docker exec)              |
| `src/backend/bark_backend/ws_handler.py`       | WebSocket terminal + exec protocol             |
| `src/backend/e2e-tests/test_cli_e2e.py`        | CLI E2E tests against real server              |

## Verification

1. `devenv shell -- test-backend` — unit tests pass with 100% coverage
2. `devenv shell -- test-cli-e2e` — E2E tests pass against real server + Docker
3. Manual smoke test:
   - `bark login admin@example.com` — authenticate
   - `bark create cli-test` — creates a workspace
   - `bark shell cli-test` — drops into bash inside container
   - `bark exec cli-test ls /work` — runs a command
   - `bark sync ~/project cli-test:/work/project` — syncs files
   - `bark rm cli-test` — cleans up
