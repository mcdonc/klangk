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
`auto_start` brings back any that are configured for it.

## SIGHUP — reload configuration + graceful runtime restart (#1212, #1587)

Sent by `kill -HUP $(cat $KLANGK_STATE_DIR/klangk-<instance>.pid)`, or by
your service manager's "reload" action.

SIGHUP is **not** a process restart — the HTTP listener and the database
stay up the whole time. It means "reload config and apply it" (the
nginx/Postgres convention):

1. **Re-resolve configuration** from the environment (`KLANGK_*` env
   vars and/or the YAML config file). If the new configuration is
   **invalid** (bad value, dangling `file:`/`cmd:` ref, unreadable config
   file), the restart is **denied** — the runtime stays running on its
   last-known-good config and the reason is logged at `ERROR` level.
2. **Apply reloadable settings.** The new `KlangkSettings` instance is
   swapped onto `app.state.settings`; all subsystems read it live. The
   OIDC discovery/JWKS caches are cleared and providers re-initialized,
   plugins are re-scanned, SSL trust is re-applied, and the agent user
   is re-seeded (so `KLANGK_AGENT_EMAIL`/`_HANDLE` changes take effect
   in the DB). CORS origins (`KLANGK_CORS_ORIGINS`) are picked up
   automatically by the live CORS middleware; `KLANGK_FRONTEND_DIR` is
   remounted if it changed (#1610).
3. **Close every WebSocket client** with close code `1012` ("service
   restarted"). Both the web UI and `klangkc monitor` reconnect
   automatically with backoff and rebuild their state on reconnect.
4. Tear down chat-agent subprocesses and cancel in-flight agent runs.
5. **Stop and remove all workspace containers** and cancel the
   idle/health background loops (`registry.shutdown()`).
6. Re-run container-side startup: pre-warm podman, adopt/reap leftover
   containers, restart the idle/health loops, and `auto_start` any
   workspaces configured for it.

In-flight HTTP requests are never dropped — only long-lived WebSocket
sessions are, and those reconnect on their own.

### When to use it

- You changed `KLANGK_*` env vars or the YAML config file and want them
  applied without a full process restart.
- You changed OIDC provider configuration, auth modes, the agent handle,
  plugin config, or SSL trust certificates.
- You changed a workspace's auto-start or sandbox configuration and want
  it picked up without bouncing the whole server.
- You want to force every workspace container to be recreated (e.g. after
  rebuilding the workspace image) while keeping the server reachable.

### Settings that require a full process restart

A small set of settings are bound for the life of the process and cannot
be applied by SIGHUP alone. If one of these changes, SIGHUP logs a
`WARNING` naming it — the reloadable settings are still applied, but the
non-reloadable change needs a full `klangkd` restart:

| Setting            | Reason                             |
| ------------------ | ---------------------------------- |
| `KLANGK_PORT`      | The HTTP listener is already bound |
| `KLANGK_LISTEN`    | The HTTP listener is already bound |
| `KLANGK_DATA_DIR`  | The DB engine is already open      |
| `KLANGK_STATE_DIR` | Instance state is already on disk  |

### What it does _not_ do

- It does **not** dispose the database engine or remove the PID file —
  those are process-shutdown-only.

### Concurrency

SIGHUP can be sent several times in quick succession. A second signal
arriving mid-restart queues behind the first via an `asyncio.Lock`, so
restarts never race — they run strictly one after another.
