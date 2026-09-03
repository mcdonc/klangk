<!-- markdownlint-disable MD013 -->

# Workspace Admission

A klangk host can be overcommitted until the kernel OOM killer picks a
victim at random — possibly klangkd itself, possibly the proxy,
possibly an unrelated workspace that was behaving. Container [memory
limits](../reference/environment.md) bound a _single_ workspace, but
nothing stopped N users from together starting more than the host
could serve: the failure surfaced later, as an opaque outage, instead
of sooner, as a clear refusal.

Admission control is the k8s-scheduler/ResourceQuota analogue: every
workspace start is checked for _admissibility_ before the container is
created, and a start the host cannot serve fails fast with an
actionable message.

## Where the check runs

Both gates run at the single container-start choke point, which every
start path funnels through:

- the API (`POST /workspaces/{id}/start`, `POST .../restart`),
- a WebSocket connect (the normal open-a-workspace flow),
- the eager start of a `auto_start` create,
- boot [auto-start](auto-start.md), and
- [crash recovery](crash-recovery.md)'s restart.

A reconnect to an **already-running** workspace is never re-admitted
(its capacity is already committed), and a draining node (a graceful
restart in progress) refuses starts before capacity is even
considered.

## Gate 1: host memory fit

Before the container is created, klangkd compares available host
memory against the workspace's **resolved memory limit** (a
per-workspace `memory_limit` settings-bag override wins over the
deploy-wide `KLANGKD_CONTAINER_MEMORY_LIMIT`) plus a reserve for the
server itself (`KLANGKD_ADMISSION_MEMORY_MARGIN`, default `1g`):

```text
host at capacity: 1.2 GB available, workspace wants 9.0 GB
(memory limit 8.0 GB + 1.0 GB reserve). Stop an idle workspace,
free host memory, or lower the workspace memory limit
(KLANGKD_CONTAINER_MEMORY_LIMIT).
```

Semantics worth knowing:

- **Advisory against the limit, not a live usage gauge.** Limits are
  what scheduler-style accounting can rely on, and the read stays
  cheap (one `/proc` read per start). A running workspace's actual
  usage is already reflected in `MemAvailable`.
- **Platform-aware measurement.** Linux reads `MemAvailable`
  (`MemFree + Cached` on old kernels); when klangkd itself runs inside
  a cgroup with a finite memory limit (Docker `-m`, a systemd slice
  `MemoryMax`), the cgroup's own headroom is measured too and the
  smaller value wins — meminfo inside a container shows the host. On
  macOS, `sysctl` + `vm_stat` measure the Mac, **capped by the podman
  machine's configured memory**: containers live in that VM, whose
  default 2048 MiB is far below the Mac's RAM. The cap is read via
  `podman machine ls` (works while the machine is stopped) and cached
  for 5 minutes.
- **Concurrent starts cannot each fit against the same stale
  reading.** Sibling starts in flight (per-workspace operation lock
  held, container not yet created) have their resolved limits
  subtracted from the measured availability.
- **No limit configured → no check.** An unbounded workspace has
  nothing to admit against.
- **Unmeasurable fails open.** A host whose memory cannot be read
  admits with a one-time warning — an exotic platform must not become
  unable to start workspaces.

The check is off by default (`KLANGKD_ADMISSION_MEMORY_ENABLED=false`):
it is advisory against the _limit_, and with the default 8g limit +
1g reserve a host with under ~9 GB available — including a default
2048 MiB podman machine on macOS — would be refused every start.
Multi-user deployments (the motivation) should enable it with limits
sized to the host:

```bash
KLANGKD_ADMISSION_MEMORY_ENABLED=true
```

Before turning it on, check that the host's typical available memory
comfortably exceeds `KLANGKD_CONTAINER_MEMORY_LIMIT` (plus the
reserve) — or lower the limit, or size the host / podman machine
(`podman machine set --memory ...`) first.

## Gate 2: per-user running quota

`KLANGKD_MAX_RUNNING_WORKSPACES_PER_USER` (default `0` = unlimited)
caps how many of a user's workspaces may be **running** concurrently.
A user at the cap gets:

```text
workspace quota reached: 2 of this user's workspaces are already
running and the server caps it at 2
(KLANGKD_MAX_RUNNING_WORKSPACES_PER_USER). Stop a workspace first,
or ask the operator to raise the cap.
```

Counting details:

- The count is per **owner** (not per connected member), so a shared
  workspace counts once against its owner.
- Workspaces that are mid-start or mid-stop count too (the operation
  lock is held), which closes the two-workspaces-starting-at-once
  race. A stop in flight transiently counts — conservative for a few
  seconds, then self-clears.
- A workspace being restarted by its owner is never double-counted:
  the restart's stop removes it from the running set before the start
  re-checks.

## What a refusal looks like

A capacity refusal is a _deterministic, operator-actionable_ refusal,
deliberately distinguishable from config errors (HTTP 400) and runtime
failures (HTTP 500):

- **HTTP**: `503` with the message above as the `detail` (from
  `/start` and `/restart`).
- **WebSocket**: an `error` frame carrying the message plus a
  machine-readable `code: "capacity"` — the web UI renders it as a
  page-level error ("stop a workspace first / free host memory")
  instead of a generic failure. The socket stays open; the client can
  retry once capacity frees.
- A capacity-refused **eager start on create** degrades to a warning:
  the workspace row exists (creation is not capacity-gated) and runs
  once capacity frees.
- **Boot auto-start** logs one clear warning per refused workspace and
  continues with the rest.
- **Crash recovery** treats a capacity refusal like any other start
  failure: bounded retries with backoff — which is the right posture
  for memory pressure (the [memory-pressure
  evictor](../reference/environment.md) may free capacity meanwhile),
  though a quota refusal will exhaust its budget and surface as a
  `crash-loop` state until the user stops something.

## How the pieces fit

| Stage            | Feature                             | What it does                                                |
| ---------------- | ----------------------------------- | ----------------------------------------------------------- |
| Before the start | **Admission control** (this page)   | Refuse starts the host cannot serve                         |
| While running    | Memory-pressure eviction            | Gracefully stop idle workspaces when availability stays low |
| After a death    | [Crash recovery](crash-recovery.md) | Classify the death, optionally restart with backoff         |

## Settings

| Setting                                   | Default | Meaning                                                           |
| ----------------------------------------- | ------- | ----------------------------------------------------------------- |
| `KLANGKD_ADMISSION_MEMORY_ENABLED`        | `false` | Enable the host-memory fit gate                                   |
| `KLANGKD_ADMISSION_MEMORY_MARGIN`         | `1g`    | Reserve kept for the server itself when fitting a limit           |
| `KLANGKD_MAX_RUNNING_WORKSPACES_PER_USER` | `0`     | Per-user cap on concurrently running workspaces (`0` = unlimited) |

All three are reloadable on SIGHUP and apply to starts after the
change. See the [environment reference](../reference/environment.md)
for the full field documentation.
