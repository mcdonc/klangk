<!-- markdownlint-disable MD013 -->

# Health Check

A workspace can have a **health check** — a shell command that Klangk
runs inside the container at regular intervals to tell whether the
service running there is actually healthy. Without one, a running
container only proves the container is alive; it says nothing about
the process inside it.

Health checks are most useful for **service workspaces** that
combine two other features:

- an **[Auto-start](auto-start.md)** workspace whose container boots
  on server startup (before any user connects), and
- a **[Default Command](default-command.md)** that launches a
  long-running process (a dev server, AI gateway, daemon).

In that combination the container is up and the process is launched
unattended — the health check is what turns "the container is
running" into "the service inside it is actually responding." For an
interactive workspace where you're already in the terminal watching
the output, a health check adds little; for an auto-started service
no one is watching, it's the difference between _available_ and
_known-good_.

Less essential, but still handy: a health check lets a shared
workspace's other members see at a glance (via the status icon and
`GET /api/v1/workspaces/{id}/status`) whether the service is up,
without opening a terminal.

## How it works

Klangk polls container health from **outside** the container using
`podman exec`. There is no agent baked into the container image and no
extra connection back to the server — the container stays a clean
sandbox that knows nothing about Klangk internals.

For each workspace with a health check configured:

1. Every 30 seconds (configurable), Klangk runs your health check
   command inside the container as the creating user, with that user's
   `HOME` set.
2. **Exit code 0 = healthy.** Any non-zero exit code, a timeout, or an
   error counts as **unhealthy**.
3. When the status changes, every connected client gets a
   `service_health` event so the UI can update in real time.
4. The current status and the time of the last check are exposed via
   `GET /api/v1/workspaces/{id}/status`.

The check runs through `sh -c "<your command>"`, so any shell syntax
works — pipes, `&&`, redirects, etc.

### When checks are skipped

Health checks are deliberately conservative:

- **No health check configured** — nothing is polled; status stays
  `null` (unknown).
- **Setup not finished** — checks are skipped until the workspace's
  [`setup_state`](sandbox.md#setup-scripts) is `complete`. Polling
  during setup would report false negatives (the service isn't running
  yet because `setup.sh` hasn't installed it).
- **Container stopped/removed** — its health state goes away with the
  container.

### Informational only

Health status is **informational only**. Klangk does not automatically
restart an unhealthy container. Auto-restart may build on this later.

## Seeing health status

Health is surfaced in several places so a failing service is hard to
miss:

- **Web UI** — the workspace list shows a health-colored icon (green =
  healthy, amber = unhealthy, grey = stopped), updated live as checks run.
  The **Settings** tab shows the configured check command.
- **`GET /api/v1/workspaces/{id}/status`** — returns `health` plus the
  time of the last check (`health_checked_at`).
- **`klangkc monitor`** — stream events and optionally **run a command**
  when something changes. This is the automation hook.

### Reacting to failures automatically with `klangkc monitor`

`klangkc monitor` connects to the server and receives the same events
the web UI does — health transitions, container starts/stops, workspace
changes — and can run a command for each one. The event JSON is piped
to the command's stdin, and details are exposed as environment
variables (`KLANGK_EVENT_TYPE`, `KLANGK_WORKSPACE_ID`, `KLANGK_HEALTHY`).

Fire a desktop notification when a service goes unhealthy:

```bash
klangkc monitor --type service_health -- \
  sh -c '[ "$KLANGK_HEALTHY" = false ] && notify-send "klangk" "Service unhealthy"'
```

Page yourself (or a Slack webhook) on any health change:

```bash
klangkc monitor --type service_health --workspace $WS_ID -- \
  sh -c 'curl -s -d "klangk health: $KLANGK_HEALTHY" https://hooks.example.com/alerts'
```

Just watch the stream (pipe to `jq`):

```bash
klangkc monitor --type service_health | jq .
```

`monitor` reconnects automatically — by default forever, with capped
exponential backoff — and refreshes its login token on auth failures,
so it survives server restarts and token expiry as a long-running
daemon. Bound it with `--max-reconnects N`, or disable reconnect with
`--no-reconnect`. See `klangkc monitor --help`.

## Setting the health check

### Web UI

Set the health check when creating a workspace, or change it later in
the workspace **Settings** tab.

### CLI

```bash
# Set during creation
klangkc create my-service --health-check 'curl -sf http://localhost:8080/health'

# Change it later
klangkc edit my-service --health-check 'pgrep -f "openclaw gateway"'

# Clear it
klangkc edit my-service --health-check ''
```

### Sandbox config

In `.klangk-sandbox.yaml` (see [Sandbox](sandbox.md)):

```yaml
workspace:
  health-check: curl -sf http://localhost:8080/health
```

## Example commands

A health check is any command that exits 0 when things are good:

```yaml
# HTTP health endpoint
curl -sf http://localhost:8080/health

# Process is running
pgrep -f 'openclaw gateway'

# A unix socket exists
test -S /tmp/my.sock

# A port is accepting connections
nc -z localhost 5432
```

## Server tuning

The polling interval and the per-check timeout are configurable via
environment variables on the server:

| Variable                       | Default | Description                                                                 |
| ------------------------------ | ------- | --------------------------------------------------------------------------- |
| `KLANGK_HEALTH_CHECK_INTERVAL` | `30`    | Seconds between polls for each workspace.                                   |
| `KLANGK_HEALTH_CHECK_TIMEOUT`  | `10`    | Seconds before a single `podman exec` check is killed and marked unhealthy. |

`podman exec` is a local Unix socket call to the Podman API, not a
network round-trip, so running a check every 30 seconds per container
is negligible overhead.

## Related

- [Default Command](default-command.md) — the command that usually
  runs the service being health-checked.
- [Auto-start](auto-start.md) — start service workspaces on server
  boot.
- [Sandbox](sandbox.md) — the `workspace.health-check` config field.
