# Process Ledger

The process-launch ledger (#2520) records every process launched inside a
running workspace container — the launch event, argv, timestamps, and a
best-effort **principal attribution**: was this launched by the **agent**
or manually by a **user**? It is an audit data point for forensics
("what installed this at 3am", "which processes did the agent spawn
while I was away"), not real-time policing or gating.

## What gets recorded

One row per captured launch (`birth`) or re-exec (`exec`) event:

| Field                | Meaning                                                                                                                                               |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `pid` / `ppid`       | Process ids at capture time (ppid at first sight — before a daemonize double-fork reparents it away)                                                  |
| `uid`                | Real uid of the process                                                                                                                               |
| `comm` / `argv`      | Process name and full argument vector (as `ps` would show them — read at event time, so a process that rewrote its own argv shows the rewritten form) |
| `started_at`         | Wall-clock timestamp                                                                                                                                  |
| `principal`          | `agent`, `user:<handle>`, or `unknown`                                                                                                                |
| `attribution_method` | `anchor` (ppid-walk to a known anchor pane) or `fallback` (degraded backend)                                                                          |
| `pane_hint`          | Who last typed into the workspace's terminals, and how long before the launch                                                                         |
| `event_kind`         | `birth` or `exec`                                                                                                                                     |

Effective-uid transitions (`euid_change`, e.g. a process gaining root) and
reparent events are detected by the watcher and logged server-side at
warning level; they do not create rows.

## How capture works

klangkd runs a small C watcher subprocess (`procleddy`) that turns
`/proc` into an ordered event stream (NDJSON on stdout; scope pushed on
stdin). The watcher sees workspace processes because rootless-podman
containers are host processes in descendant pid namespaces — every
workspace process is visible and readable from klangkd's own unprivileged
uid. Workspace membership is a ppid-walk to the container init's host pid
(`podman inspect .State.Pid`); attribution anchors are the tmux pane
shells klangkd itself creates (the agent's `service` window, and user
windows opened via the web UI or `klangk shell`).

- **Performance contract:** poll interval ≤ 80 ms (default 20 ms) at ≤1% of one core at
  ~12k processes. The watcher parses `status` at full rate only for new
  and watched pids; everyone else gets a staggered (~20 s) refresh. A
  cost spike stretches the cadence (skipped polls) rather than running
  hot — the heartbeat reports the effective interval.
- **Fallback:** when the watcher binary is missing or crashes
  repeatedly, a Python poller takes over at a budget-derived
  multi-second interval. The active backend, effective interval, and
  coverage-gap count are surfaced via
  `GET /api/v1/workspaces/{id}/process-ledger` — degraded coverage is
  visible, never silent.
- **Retention:** rows are pruned by age (default 7 days) and a global
  row cap (default 50000), both configurable.
- **eBPF backend (spike, `KLANGKD_PROCESS_LEDGER_BACKEND=ebpf`):**
  instead of polling, a tracepoint-backed monitor (`procleddy-ebpf`)
  captures every `execve` exactly — short-lived processes included, no
  polling dark window. `sched_process_fork/exit` maintain a kernel-side
  parent map; `sys_enter_execve[_at]` captures argv + uid and walks the
  map toward the workspace roots in-kernel; the loader emits the same
  NDJSON contract, so klangkd needs no other changes. This is the
  privileged deployment tier: the process needs `CAP_BPF` +
  `CAP_PERFMON` (e.g. systemd `AmbientCapabilities=`) — it cannot run
  in the unprivileged containerized host image, where the `/proc`
  poller remains the backend. Spike limitations: argv truncated to 32
  args / 2 KiB, `sid` not captured, ancestry of processes forked before
  the monitor started is finished from `/proc` by the loader, and
  fork-without-exec events are not recorded (only the merged birth on
  exec — one row per program launched).

## Reading the ledger

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://klangk.example.com/api/v1/workspaces/$WS/processes?limit=100
```

Requires the dedicated `read-proc-ledger` permission on the workspace.
By default **only the owners role group can read the ledger** (its `*`
wildcard covers the permission); no other role group is seeded it, so
granting ledger access to anyone else — including coders and
collaborators — is an explicit operator choice via the ACL editor
(`read-proc-ledger` is in the permission lists).

## Honest limitations

- **Sub-interval processes are dark**: a process that execs and exits
  within one poll interval (~20 ms watched; seconds in fallback) is
  never seen. What the ledger covers is surviving processes and
  longer-lived recon (scans, installs, staging), plus everything
  persistent.
- **argv is read at event time**, not at exec — a deliberately fast
  attacker can rewrite its own argv before the watcher reads it. Same
  trust level as `ps` output: right for audit, not for proof.
- **macOS is unsupported** in v1: klangkd runs on the host where there
  is no `/proc`; containers live inside the podman-machine VM.
- Attribution is best-effort. A nonzero `unknown` rate is itself a
  signal — nothing legitimate should launch un-anchored.

## Configuration

See the `KLANGKD_PROCESS_LEDGER_*` rows in
[Environment Variables](../reference/environment.md). The ledger is
**off by default**; enable with `KLANGKD_PROCESS_LEDGER_ENABLED=true`
(reloadable on SIGHUP).
