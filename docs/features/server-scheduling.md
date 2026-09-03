# Server Scheduling

Admins can schedule a server **stop** or **recycle** at an absolute time
or after a delay. Schedules live in the database (they survive a
`klangkd` restart), fire whether or not anyone is connected, and every
connected client sees a live countdown while one is pending.

"Server" means the `klangkd` process — the thing that owns every
workspace container — not the machine. There are no OS power commands
and no privilege escalation wired into klangkd: a **stop** exits the
process gracefully and hands the lifecycle to the service manager
(systemd decides what happens next); a **recycle** rebuilds the runtime
in-process without ever exiting. To power off or reboot the _machine_,
do it at the service-manager level (a systemd unit, timer, or drop-in
that stops the klangkd unit).

## Actions

| Action    | What fires                                                                                                                                                                                                                                                                                            |
| --------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `stop`    | The graceful TERM/INT path: notify, refuse starts, quiesce, drain every workspace, then exit — the process ends with SIGTERM's status (143), like a normal `systemctl stop`. What happens next is the service manager's decision (`Restart=always` brings klangkd back; `Restart=no` leaves it down). |
| `recycle` | The [SIGHUP](../deployment/signals.md) graceful runtime recycle, always: quiesce, drain, recycle the runtime **in-process** (HTTP listener and DB stay up), `host_started`. The process never exits — a deploy that wants the supervisor to restart klangkd schedules a stop instead.                 |

This is the "plan ahead" counterpart to the immediate signal paths —
see [Process Signals](../deployment/signals.md) for what TERM/INT/HUP do
on receipt.

## Scheduling an action

Schedules are managed from the Admin page (**Admin → Server**) —
the tab is visible to users with the `admin` permission. Pick the
action (**Stop** or **Recycle**) and when it should fire:

- **After a delay** — a human delay like `2h`, `90m`, `45s`, or `2h 30m`
  (a bare number means minutes).
- **At a time** — date and time pickers; the time is your local time.

The form shows when the action will fire and surfaces the API's
validation errors inline. Pending schedules are listed soonest-first
with the same live countdown clients see, and each row has a cancel
button (with a confirm step). The list follows the live
`server_schedule` snapshot, so a schedule created or cancelled from
another session (or via the API) appears and disappears immediately.

For scripting, the same operations are available through the API (see
[API Reference](../reference/api-endpoints.md) —
`/api/v1/server/schedule`):

```bash
# Stop the server tonight at 23:00 (server-local time of the fire_at)
curl -X POST https://klangk.example.com/api/v1/server/schedule \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "stop", "at": "2026-08-24T23:00:00+02:00"}'

# Recycle the server after a 30-minute grace window
curl -X POST https://klangk.example.com/api/v1/server/schedule \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "recycle", "in_seconds": 1800}'

# List pending schedules / cancel one
curl https://klangk.example.com/api/v1/server/schedule -H "Authorization: Bearer $TOKEN"
curl -X DELETE https://klangk.example.com/api/v1/server/schedule/<id> \
  -H "Authorization: Bearer $TOKEN"
```

Provide `at` (absolute ISO-8601; a naive timestamp is interpreted as
UTC) or `in_seconds` (positive delay; ignored when `at` is given).
Creating a schedule requires the `admin` permission.

Rows exist only while pending — a schedule that fires or is cancelled is
deleted, so the list is always the authoritative set of upcoming server
actions.

## What clients see

- **Web UI** — a persistent banner above the workspace body showing the
  soonest pending action with a live countdown that ticks locally every
  second (e.g. `⏻ Server stops at 23:00 (in 1h 12m — workspaces stop)`).
  It is non-blocking: it never gates navigation or auto-reconnect, and
  it disappears the moment no schedule is pending.
- **TUI (`klangk monitor`)** — a status line,
  `server: stop at 23:00 (in 1h 12m)`, plus a firing notice.
- **WebSocket** — a `server_schedule` snapshot is pushed to every
  authenticated client on change and periodically; new connections get
  the pending snapshot replayed immediately, so a client that connects
  mid-countdown learns about it at once. When the schedule fires, a
  `server_schedule_fired` event surfaces as a transient notice; the
  stop/recycle sequences then proceed with the same
  `host_shutdown` / `server_recycle {phase}` → `host_started` events the
  signal paths emit.

## What happens when a schedule fires

The scheduler owns no teardown of its own — it hands off to the
existing graceful lifecycle paths (no OS commands):

1. **Delete the schedule row** — a crash mid-fire cannot re-fire it.
2. **Notify** — broadcast `server_schedule_fired`; clients surface a
   "happening now" notice.
3. **Hand off**:
   - `stop` → SIGTERM to self: the graceful-shutdown path refuses
     new starts, **quiesces** (waits up to `KLANGKD_QUIESCE_TIMEOUT`
     seconds for in-flight HTTP requests), drains every workspace
     through the graceful path (terminal stop frames +
     `container_stopped`), and exits. Budget the stop inside your
     service manager's deadline — see the timing notes in
     [Process Signals](../deployment/signals.md).
   - `recycle` → the SIGHUP graceful recycle, verbatim: validate
     (current config), refuse starts, quiesce, drain, recycle the
     runtime in-process, `host_started`. Never exits.

A recycle firing during a shutdown-in-progress is skipped (the exit
owns the process); its row is still consumed, so it cannot re-fire on
the next boot.

**Past-due schedules fire on the next boot.** A schedule whose fire
time passes while klangkd is down (crash, maintenance) fires ~5s after
the next boot — deliberately, so a stop planned for a maintenance
window still happens even if the server missed the moment. Under
`Restart=always` that means the fresh process drains and exits again
immediately; cancel the schedule first if that's not wanted.
