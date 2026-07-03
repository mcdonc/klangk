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

## SIGHUP — graceful runtime restart (#1212)

Sent by `kill -HUP $(cat $XDG_RUNTIME_DIR/klangk-<instance>.pid)`, or by
your service manager's "reload" action.

SIGHUP is **not** a process restart — the HTTP listener and the database
stay up the whole time. Instead it recycles the _runtime layer_:

1. **Close every WebSocket client** with close code `1012` ("service
   restarted"). Both the web UI and `klangkc monitor` reconnect
   automatically with backoff and rebuild their state on reconnect.
2. Tear down chat-agent subprocesses and cancel in-flight agent runs.
3. **Stop and remove all workspace containers** and cancel the
   idle/health background loops (`registry.shutdown()`).
4. Re-run container-side startup: pre-warm podman, adopt/reap leftover
   containers, restart the idle/health loops, and `auto_start` any
   workspaces configured for it.

In-flight HTTP requests are never dropped — only long-lived WebSocket
sessions are, and those reconnect on their own.

### When to use it

- You changed a workspace's auto-start or sandbox configuration and want
  it picked up without bouncing the whole server.
- You want to force every workspace container to be recreated (e.g. after
  rebuilding the workspace image) while keeping the server reachable.
- A demo/ops beat where you want to show `stopped → running → healthy`
  transitions without a full restart that would make `klangkc ls` see a
  refused connection mid-bounce.

### What it deliberately does _not_ do

- It does **not** re-read `.env` / re-run OIDC provider discovery / reload
  plugins. Those are process-startup concerns; for them, fully restart
  the server (SIGTERM + relaunch).
- It does **not** dispose the database engine or remove the PID file —
  those are process-shutdown-only (`_process_shutdown`).

### Concurrency

SIGHUP can be sent several times in quick succession. A second signal
arriving mid-restart queues behind the first via an `asyncio.Lock`, so
restarts never race — they run strictly one after another.
