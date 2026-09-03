<!-- markdownlint-disable MD013 -->

# Crash Recovery

A workspace container can die unexpectedly: the kernel's OOM killer
reaps it when it exceeds its [memory
limit](../reference/environment.md) (`KLANGKD_CONTAINER_MEMORY_LIMIT`
or the workspace's `memory_limit` override), the main process crashes,
or something on the host removes the container outright. Previously,
such a death was invisible until a human noticed — the workspace list
kept showing the container as running, and recovery was entirely
manual.

Klangk now watches for exactly that.

## Death classification

A liveness sweep inspects every tracked workspace container (every 15
seconds). A container that is gone or no longer running — while the
server still tracks it and no stop is in flight — died unexpectedly,
and `podman inspect` says why:

| Cause     | Meaning                                                                                                                    |
| --------- | -------------------------------------------------------------------------------------------------------------------------- |
| `oom`     | OOM-killed — reported against the workspace's effective memory limit, e.g. "OOM-killed at 8g memory limit (exit code 137)" |
| `exited`  | Main process exited (non-zero code, a named signal, or a clean `0`)                                                        |
| `removed` | The container no longer exists — removed externally                                                                        |

The cause rides every surface a consumer can watch:

- the terminal `service_health` death frame (its `message` field, for
  workspaces with a [health check](health-check.md)),
- a `container_died` custom event on the workspace's WebSocket session
  (mirroring the `container_stopped` event a user stop sends; it shows
  up in the [debug panel](debug-panel.md), which surfaces all custom
  events, even though the workspace UI does not yet render it),
- `GET /api/v1/workspaces/{id}/status` → `restart.last_cause`, and
- the server log.

The dead container is torn down and its registry state removed either
way, so the workspace list stops lying and the next connect starts a
fresh container.

## Auto-restart (opt-in)

Classification always happens. **Restarting** is opt-in:

```bash
KLANGKD_CONTAINER_RESTART_ENABLED=true
```

With it on, an unexpectedly-dead workspace is restarted after an
exponential backoff — attempt _n_ waits
`KLANGKD_CONTAINER_RESTART_BACKOFF_SECONDS * 2^(n-1)` seconds, capped
at 60s (5s → 10s → 20s → 40s → 60s … by default). Because workspace
state lives in named volumes and the home bind mount, not in the
container, a restart loses nothing but running processes; the
[service command](service-command.md) re-fires on the fresh container.

Two bounds keep a broken workspace from spinning forever or
accumulating a lifetime grudge:

- **`KLANGKD_CONTAINER_RESTART_MAX_RETRIES`** (default `5`) caps the
  attempts per crash episode. Exhausting the budget leaves the
  workspace stopped in a visible **`crash-loop`** terminal state —
  surfaced on `GET /api/v1/workspaces/{id}/status` as
  `restart.state` with the attempts and last death cause — instead of
  an infinite restart loop.
- The counter **resets** once a restarted container has stayed up for
  10 minutes (or immediately on any user-driven start or stop), so
  three crashes in three months are three independent episodes, not a
  crash-loop.

A restart whose start itself fails (e.g. a workspace whose configured
mount source disappeared) retries inside the same bounded budget.

## What never restarts

Expected deaths never enter the restart path: a user stop, the idle
timeout, workspace deletion, logout, and server shutdown all cancel
any pending restart for the workspace. A restart disabled by a SIGHUP
reload mid-backoff simply doesn't fire.

All three settings are reloadable on SIGHUP and validated at startup —
a malformed value aborts boot rather than silently disabling the
recovery policy.
