# Process signals

The klangk backend (a `uvicorn` process) reacts to a small set of POSIX
signals. Knowing which is which matters when you operate a deployment by
hand — the difference between a _reload_ and a _full stop_ is whether
running containers survive.

## SIGINT / SIGTERM — stop the server

The normal shutdown path (Ctrl-C, `systemctl stop`, container-runtime
graceful exit). uvicorn handles these natively.

What happens, in order:

1. Accept no new requests.
2. Close every WebSocket client.
3. Tear down chat-agent subprocesses and cancel in-flight agent runs.
4. **Stop and remove all workspace containers** (the idle-timeout cleanup
   runs to completion).
5. Dispose the database engine and remove the PID file.

Net effect: a _full_ stop. Workspaces go away; on the next start,
`auto_start` brings back any that are configured for it. To control
_when_ that happens (and give clients clean stop frames first), see
[Cordon & Drain](cordon-drain.md) — drain before the stop, and cordon
keeps a restarting/crash-looping klangkd from re-starting workspaces.

## SIGHUP — graceful restart (#1212, #1587, #2527)

Sent by `kill -HUP $(cat $KLANGKD_STATE_DIR/klangk-<instance>.pid)`, or by
your service manager's "reload" action.

SIGHUP is **not** a process restart — the HTTP listener and the database
stay up the whole time. It is a **graceful host restart**: finish what's
in flight, refuse new work, drain the containers, then apply the new
configuration and bring the runtime back up. Each phase is logged, and
authenticated WebSocket clients receive a `host_restart` event with a
`phase` field (`draining`, `restarting`) as it progresses, plus a final
`host_started` broadcast when the restart completes.

1. **Validate** — re-resolve configuration from the environment
   (`KLANGK_*` env vars and/or the YAML config file). If the new
   configuration is **invalid** (bad value, dangling `file:`/`cmd:` ref,
   unreadable config file), the restart is **denied** — the runtime stays
   running on its last-known-good config, nothing is drained, and the
   reason is logged at `ERROR` level.
2. **Refuse new starts** — broadcast `host_restart {phase: "draining"}`
   and set an in-memory drain flag: every path that could start a
   container (API start/restart, WS connect, boot auto-start,
   crash-recovery restart) refuses with a clear error until the restart
   completes. Unlike an operator cordon, this flag is never persisted —
   a crashed restart cannot leave the node refusing starts.
3. **Quiesce** — wait up to `KLANGKD_RESTART_INFLIGHT_TIMEOUT` seconds
   (default 15) for in-flight HTTP requests to finish. Requests still
   running at expiry are logged (WARNING) and left to finish against the
   recycling runtime; nothing is dropped mid-response.
4. **Drain** — stop every running workspace through the graceful path
   (the same one `klangk admin drain` uses): clients get terminal status
   frames and a `container_stopped` event with reason `host restart`,
   not a dropped socket. Previously running workspaces are **not**
   remembered — only workspaces configured for `auto_start` come back
   (in step 7), exactly as on a fresh boot.
5. **Apply reloadable settings.** The new `KlangkSettings` instance is
   swapped onto `app.state.settings`; all subsystems read it live. The
   OIDC discovery/JWKS caches are cleared and providers re-initialized,
   features are re-scanned, SSL trust is re-applied, and the agent user
   is re-seeded (so `KLANGKWS_FEATURE_CHAT_AGENT_EMAIL`/`_HANDLE`
   changes — set in the `features_config:` block — take effect in the DB). CORS origins (`KLANGKD_CORS_ORIGINS`) are picked up
   automatically by the live CORS middleware; `KLANGKD_FRONTEND_DIR` is
   remounted if it changed (#1610).
6. **Recycle the runtime** — close every WebSocket client with close
   code `1012` ("service restarted"), tear down chat-agent subprocesses
   and in-flight agent runs, stop the idle/health/crash background
   loops, then re-run container-side startup: pre-warm podman,
   adopt/reap leftover containers, restart the loops, and `auto_start`
   any workspaces configured for it.
7. **Resume** — broadcast `host_started`. Both the web UI and
   `klangk monitor` reconnect automatically with backoff and rebuild
   their state on reconnect.

For an operator-driven cordon + drain (which survives restarts and
needs an explicit uncordon), see
[Cordon & drain](cordon-drain.md) — that is the tool for host
maintenance; SIGHUP is the tool for config reload with a clean slate.

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
arriving mid-restart queues behind the first via an `asyncio.Lock`, so
restarts never race — they run strictly one after another.

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
