# Process signals

The klangk backend (a `uvicorn` process) reacts to a small set of POSIX
signals. Knowing which is which matters when you operate a deployment by
hand — the difference between a _reload_ and a _full stop_ is whether
running containers survive.

## SIGINT / SIGTERM — graceful stop (#2527)

The normal shutdown path (Ctrl-C, `systemctl stop`, container-runtime
graceful exit). Each phase is logged.

What happens, in order:

1. **Notify** — broadcast a `host_shutdown` WebSocket event to every
   authenticated client, so the UI can render "server went away"
   instead of silently reconnect-looping. (This runs at signal-receipt
   time, before uvicorn closes the sockets — a lifespan-time broadcast
   would reach nobody.)
2. **Refuse new starts** — new workspace starts (API, WS, eager,
   auto-start, crash-restart) return a clear 503/error frame for the
   rest of the process's life; a start racing the shutdown is refused
   rather than killed mid-create.
3. **Quiesce** (#2664) — wait up to `KLANGKD_QUIESCE_TIMEOUT` seconds
   (default 15) for in-flight HTTP requests to finish, so an upload or
   terminal snapshot in progress isn't cut off by the drain below.
   Stragglers at expiry are logged (WARNING) and left to finish against
   the exiting process (uvicorn's own in-flight wait only starts after
   this phase, when the containers are already stopped, so the wait
   has to happen here to buy them anything).
4. **Drain** — stop every running workspace through the same graceful
   path as SIGHUP's drain (concurrently per workspace, 5s podman stop
   grace): clients get terminal stop frames and a `container_stopped`
   event with reason `host shutdown`, not a bare socket drop. A SIGHUP
   arriving after the signal is ignored (the runtime is being torn
   down; a restart would race the exit).
5. Accept no new requests, close every WebSocket client (uvicorn's own
   exit sequence). A second Ctrl-C during the quiesce or drain skips the
   rest of the graceful work and forces the exit immediately.
6. Dispose the database engine and remove the PID file.

Net effect: a _full_ stop with clean client-visible shutdown frames.
Workspaces go away; on the next start, `auto_start` brings back any
that are configured for it. For a config reload with the same drain
treatment while keeping the listener up, use SIGHUP (below).

A drain failure is logged and never blocks the exit — the process always
terminates. Budget the quiesce + drain inside your service manager's
stop deadline (`TimeoutStopSec` under systemd): up to
`KLANGKD_QUIESCE_TIMEOUT` seconds (default 15) of request quiesce, plus
one per-workspace stop grace (5s; stops run concurrently across
workspaces). Raising `KLANGKD_QUIESCE_TIMEOUT` past ~85s blows the
default 90s `TimeoutStopSec`.

## SIGHUP — graceful runtime recycle (#1212, #1587, #2527, #2661)

Sent by `kill -HUP $(cat $KLANGKD_STATE_DIR/klangk-<instance>.pid)`, or by
your service manager's "reload" action.

SIGHUP is **not** a process restart — the HTTP listener and the database
stay up the whole time. It is a **graceful runtime recycle**: finish what's
in flight, refuse new work, drain the containers, then apply the new
configuration and bring the runtime back up. Each phase is logged, and
authenticated WebSocket clients receive a `server_recycle` event with a
`phase` field (`draining`, `recycling`) as it progresses, plus a final
`host_started` broadcast when the recycle completes.

1. **Validate** — re-resolve configuration from the environment
   (`KLANGK_*` env vars and/or the YAML config file). If the new
   configuration is **invalid** (bad value, dangling `file:`/`cmd:` ref,
   unreadable config file), the recycle is **denied** — the runtime stays
   running on its last-known-good config, nothing is drained, and the
   reason is logged at `ERROR` level.
2. **Refuse new starts** — broadcast `server_recycle {phase: "draining"}`
   and set an in-memory drain flag: every path that could start a
   container (API start/restart, WS connect, boot auto-start,
   crash-recovery restart) refuses with a clear error until the recycle
   completes. This flag is never persisted — a crashed recycle
   cannot leave the node refusing starts.
3. **Quiesce** — wait up to `KLANGKD_QUIESCE_TIMEOUT` seconds
   (default 15) for in-flight HTTP requests to finish. Requests still
   running at expiry are logged (WARNING); ordinary requests finish
   against the recycling runtime, but a long-lived streaming response
   (`/llm_proxy`, `/browser-delegate/stream`) cannot outlive the drain
   and will be interrupted by the recycle.
4. **Drain** — stop every running workspace through the graceful path
   (concurrently per workspace, each with a 5s podman stop grace):
   clients get terminal status
   frames and a `container_stopped` event with reason `server recycle`,
   not a dropped socket. Previously running workspaces are **not**
   remembered — only workspaces configured for `auto_start` come back
   (in step 7), exactly as on a fresh boot.
5. **Apply reloadable settings.** The new `KlangkSettings` instance is
   swapped onto `app.state.settings`; all subsystems read it live. The
   OIDC discovery/JWKS caches are cleared and providers re-initialized,
   features are re-scanned, SSL trust is re-applied, and the agent user
   is re-seeded (reconciled to the fixed identity, #2718 — the identity
   config keys are gone). CORS origins (`KLANGKD_CORS_ORIGINS`) are picked up
   automatically by the live CORS middleware; `KLANGKD_FRONTEND_DIR` is
   remounted if it changed (#1610).
6. **Recycle the runtime** — close every WebSocket client with close
   code `1012` ("service restarted"), stop the idle/health/crash
   background loops, then re-run container-side startup: pre-warm podman,
   adopt/reap leftover containers, restart the loops, and `auto_start`
   any workspaces configured for it.
7. **Resume** — broadcast `host_started`. Both the web UI and
   `klangk monitor` reconnect automatically with backoff and rebuild
   their state on reconnect. New container starts stay refused
   (503, "a recycle is in progress") through step 6's podman pre-warm
   and container reaps — a client that reconnects and starts a
   workspace in that window gets a clean refusal instead of having its
   fresh container reaped — then auto-start runs once starts are
   allowed again.

If any step fails, the failure is logged, a recovery pass re-runs the
startup sequence, and `host_started` is broadcast on recovery; if the
recovery itself fails the process exits (code 1) so the service manager
restarts it — the node never lingers half-restarted while its HTTP
listener keeps serving.

### When to use it

- You changed `KLANGK_*` env vars or the YAML config file and want them
  applied without a full process restart.
- You changed OIDC provider configuration, auth modes, the agent handle,
  feature config, or SSL trust certificates.
- You changed a workspace's auto-start or sandbox configuration and want
  it picked up without bouncing the whole server.
- You want to force every workspace container to be recreated (e.g. after
  rebuilding the workspace image) while keeping the server reachable.

### Settings that require a full process restart

A small set of settings are bound for the life of the process and cannot
be applied by SIGHUP alone. If one of these changes, SIGHUP logs a
`WARNING` naming it — the reloadable settings are still applied, but the
non-reloadable change needs a full `klangkd` restart:

| Setting             | Reason                             |
| ------------------- | ---------------------------------- |
| `KLANGKD_PORT`      | The HTTP listener is already bound |
| `KLANGKD_LISTEN`    | The HTTP listener is already bound |
| `KLANGKD_DATA_DIR`  | The DB engine is already open      |
| `KLANGKD_STATE_DIR` | Instance state is already on disk  |

### What it does _not_ do

- It does **not** dispose the database engine or remove the PID file —
  those are process-shutdown-only.

### Concurrency

SIGHUP can be sent several times in quick succession. A second signal
arriving mid-recycle queues behind the first via an `asyncio.Lock`, so
recycles never race — they run strictly one after another.

## Scheduled stop / recycle (#2661)

The signal paths above act **on receipt**. For a planned action, an
admin can schedule a server stop or recycle ahead of time
(`POST /api/v1/server/schedule` — see
[Server Scheduling](../features/server-scheduling.md)). Schedules
persist in the DB across `klangkd` restarts, and every connected
client sees a live countdown (web UI banner, TUI status line) from the
moment the schedule is created.

When the schedule fires, the scheduler owns no teardown of its own —
it deletes the row (a crash mid-fire cannot re-fire it on the next
boot), notifies clients (`server_schedule_fired`), and hands off to
the paths documented above:

- **`stop`** → SIGTERM to self: the SIGINT/SIGTERM graceful-stop path
  (refuse starts, quiesce, drain, exit with SIGTERM's status) runs
  verbatim. What happens next is the service manager's decision.
- **`recycle`** → the SIGHUP graceful recycle, verbatim (quiesce,
  drain, recycle the runtime in-process, `host_started`); the process
  never exits. A recycle firing during a shutdown-in-progress is
  skipped — its row is still consumed, so it cannot re-fire on the
  next boot.

There are no OS power commands: klangkd never powers off or reboots
the machine it runs on. To take the _host_ down at the machine level,
stop the klangkd unit from the service manager (systemd unit, timer,
or drop-in).

## Exit statuses

`klangkd`'s exit status tells a supervisor _why_ the process left — in
particular, whether restarting can help (#2666):

| Status | Meaning                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `0`    | Clean shutdown (SIGINT/SIGTERM).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| `78`   | **Configuration error** (`EX_CONFIG`, sysexits.h). Startup was refused over bad configuration — e.g. a `KLANGKD_DEFAULT_PASSWORD` that violates the password policy, `auth_modes: password` without a staged password, an insecure JWT secret with prevention on, `auth_modes: none` on a non-loopback bind, a missing OIDC login hook, or a containerized FIPS backend whose OpenSSL is not FIPS-enforcing. **Restarting cannot fix this** — fix the config/image first. The refusal reason is logged at `ERROR` right before exit. |
| `3`    | uvicorn startup failure that is _not_ a config refusal (e.g. an unusable database). Retrying makes sense once the underlying fault is repaired.                                                                                                                                                                                                                                                                                                                                                                                      |
| `1`    | Launcher pre-flight refusals (another instance already running, a browser/egress port already owned) or a UDS bind failure.                                                                                                                                                                                                                                                                                                                                                                                                          |

### Telling systemd not to restart-loop a config error

A config refusal is permanent, so a supervisor that restarts on any
failure will loop on it forever. With systemd, pin the status:

```ini
[Service]
Restart=on-failure
RestartPreventExitStatus=78
```

A bad password then stops the unit in a single failed attempt instead of
burning CPU in a restart loop; `journalctl -u <unit>` shows the
`ConfigurationError` naming the setting to fix.
