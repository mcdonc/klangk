# Workspaces

![Workspaces page](../assets/workspaces.png)

- Multiple workspaces per user
- Each workspace gets its own podman container + bind-mounted directory
- URL-based workspace routing (survives page reload via hash URL reading)
- Workspace name and logged-in user email shown in app bar, browser tab title
- Containers stay alive after disconnect — idle timeout handles cleanup
- On logout, containers are only stopped if no other users are actively connected to shared workspaces
- Container lifecycle visible in debug panel

## Workspace Sharing

Owners can grant access to other users via the edit dialog (autocomplete user search) or API (`POST /workspaces/{id}/members`). Shared users connect to the same container and see the workspace in a "Shared with Me" section on their workspace list. File API resolves paths using the owner's directory. Shared member avatars (first letter of email) shown on workspace cards.

## Pi Agent Integration

- One podman container per workspace running Pi in interactive terminal mode
- Users interact with Pi directly through the terminal pane
- Native Pi session persistence (JSONL files in workspace `.pi/sessions/`)
- Session resume on reconnect via `--session` CLI flag (passed as `KLANGK_RESUME_SESSION` env var to the container)
- LLM provider/model configured via `settings.json` (merged from build-time settings + runtime LLM config by entrypoint)
- `models.json` written by entrypoint with proxy URL (no real API key — nginx proxy injects it)
- All provider env vars (`KLANGK_LLM_*`, `ANTHROPIC_*`, etc.) stripped from Pi's process environment before exec
- `/bin/sh` symlinked to `/bin/bash` in the base image so Pi's bash tool supports bashisms (`source`, etc.)
- System prompt (`src/containers/system-prompt.md`) copied into image at build time

## Port Allocation

Per-workspace port allocation: well-known container ports (8000+) mapped to host ports (9000+), persisted in SQLite (`port_allocations` table with per-port PRIMARY KEY preventing overlap). Ports allocated at workspace creation, stable across restarts, freed by CASCADE on workspace delete. `num_ports` column on workspaces table (default 5) controls how many; on container start, ports are added/removed to match. `KLANGK_PORT_MAPPINGS` env var passes container:host pairs to the container.

Built-in `get_hosted_url` tool converts container port to full user-facing URL using `KLANGK_PORT_MAPPINGS`, `KLANGK_HOSTING_HOSTNAME`, `KLANGK_HOSTING_PROTO`, and `KLANGK_HOSTING_BASE_PATH`.

Hosted app proxy: user apps are accessible at `{base_path}/hosted/{workspace_id}/{port}/` — nginx proxies requests directly to `localhost:{port}` on the host (bypassing the Python backend). No authentication required for hosted app URLs.

## Idle Timeout

30-minute idle timeout (configurable via `KLANGK_IDLE_TIMEOUT_SECONDS`) with automatic container stop, debug notification, and terminal overlay with restart button. Activity is recorded on user actions (prompt, steer, terminal input) and on every Pi event (tool calls, text streaming), so containers stay alive during long-running LLM requests as long as events are flowing. Stuck tool executions (e.g., foreground server) produce no events and will eventually time out.

## Container Details

- `/home/klangk` — bind mount to host (`$KLANGK_DATA_DIR/workspaces/<user>/home/<workspace>/`). Contains `work/` subdirectory for user files, plus dotfiles (`.bashrc`, `.vimrc`, `.gitconfig`), bash history, and Pi sessions. All persist across container restarts. Pi agent config (`.pi/agent/`) is cleaned and regenerated each start.
- `klangk` user baked into the image at build time; `--userns=keep-id:uid=1000,gid=1000` maps the host user to uid/gid 1000 inside the container so bind-mounted files have correct ownership
- Root escalation prevented: root password locked, suid removed from `su`/`chsh`/`chfn`/`newgrp`
- Containers labeled with `klangk.managed=true`, `klangk.instance=<KLANGK_INSTANCE_ID>`, and `klangk.workspace-id=<id>` for identification, cleanup, and orphan detection
- `--init` runs an init process as PID 1 to reap zombie processes from terminal sessions and tool executions

## Export/Import

Admins can export a workspace as a `.tar.gz` archive (home directory + metadata) via `GET /workspaces/{id}/export`. Any user can import an archive via `POST /workspaces/import` to create a new workspace. CLI commands: `klangkc export` / `klangkc import`. See [Export & Import](export-import.md) for details.
