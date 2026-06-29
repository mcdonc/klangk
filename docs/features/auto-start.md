<!-- markdownlint-disable MD013 -->

# Auto-Start

[![A service workspace with `openclaw gateway` already running via default command](../assets/auto-start.png)](../assets/auto-start.png)

Workspaces can be configured to start their containers automatically
when the Klangk server starts. This is primarily useful for service
workspaces — workspaces that run a long-lived process via
[Default Command](default-command.md) and should be available
immediately, without waiting for a user to connect.

## Enabling auto-start on the server

Auto-start is disabled by default. To allow workspaces to use it,
set the `KLANGK_ALLOW_AUTOSTART` environment variable:

```bash
KLANGK_ALLOW_AUTOSTART=1
```

When this is not set, the auto-start option is hidden in the web
UI, CLI, and API. Existing workspaces with auto-start enabled will
not start on boot.

## Configuring a workspace for auto-start

### Web UI

Enable auto-start from the workspace **Settings** tab.

### CLI

```bash
# Enable during creation
klangkc create my-service --auto-start

# Enable on an existing workspace
klangkc edit my-service --auto-start

# Disable
klangkc edit my-service --no-auto-start
```

### Sandbox config

In `.klangk-sandbox.yaml`:

```yaml
workspace:
  default-command: openclaw gateway
  auto-start: true
```

## How it works

When the Klangk server starts, after initializing the database and
cleaning up orphaned containers, it queries all workspaces with
auto-start enabled and starts their containers. If a workspace also
has a [default command](default-command.md), the command is sent as
keystrokes into tmux window 0 — so the service is already running
by the time any user connects.

Users connect later with `klangkc shell` and see the service
output in tmux window 0. They can open a second window for a shell
alongside the running service.

## Typical setup

A service workspace usually combines auto-start with a default
command, and often a health check to confirm the service is actually
serving:

```yaml
workspace:
  default-command: openclaw gateway
  auto-start: true
  health-check: curl -sf http://localhost:8080/health
```

This gives you:

1. Server starts → container starts → `openclaw gateway` runs
2. Health check confirms the gateway is responding — see
   [Health Check](health-check.md)
3. User runs `klangkc shell my-service` → sees gateway output
4. User runs `klangkc shell my-service shell` → gets a bash prompt
   in a separate tmux window
5. Ctrl+C in the gateway window stops it; up-arrow + Enter restarts
