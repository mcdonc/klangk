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

## Idle Timeout

30-minute idle timeout (configurable via `KLANGK_IDLE_TIMEOUT_SECONDS`) with automatic container stop, debug notification, and terminal overlay with restart button. Activity is recorded on user actions (prompt, steer, terminal input) and on every Pi event (tool calls, text streaming), so containers stay alive during long-running LLM requests as long as events are flowing. Stuck tool executions (e.g., foreground server) produce no events and will eventually time out.

## Container Details

- `/home/klangk` — bind mount to host (`$KLANGK_DATA_DIR/workspaces/<user>/home/<workspace>/`). Contains `work/` subdirectory for user files, plus dotfiles (`.bashrc`, `.vimrc`, `.gitconfig`), bash history, and Pi sessions. All persist across container restarts. Pi agent config (`.pi/agent/`) is cleaned and regenerated each start.
- `klangk` user baked into the image at build time; `--userns=keep-id:uid=1000,gid=1000` maps the host user to uid/gid 1000 inside the container so bind-mounted files have correct ownership
- Root escalation prevented: root password locked, suid removed from `su`/`chsh`/`chfn`/`newgrp`
- Containers labeled with `klangk.managed=true`, `klangk.instance=<KLANGK_INSTANCE_ID>`, and `klangk.workspace-id=<id>` for identification, cleanup, and orphan detection
- `--init` runs an init process as PID 1 to reap zombie processes from terminal sessions and tool executions

## Export/Import

Workspaces can be exported as archives and imported to create new ones. See [Export & Import](export-import.md).
