# Host Scheduling

Admins can schedule a host **shutdown** or **restart** at an absolute
time or after a delay (#2661). Schedules live in the database (they
survive a `klangkd` restart), fire whether or not anyone is connected,
and every connected client sees a live countdown while one is pending.

This is the "plan ahead" counterpart to the immediate signal paths —
see [Process Signals](../deployment/signals.md) for what TERM/INT/HUP
do on receipt.

## Scheduling an action

There is no dedicated admin UI; schedules are managed through the API
(see [API Reference](../reference/api-endpoints.md) —
`/api/v1/admin/host/schedule`):

```bash
# Restart the host tonight at 23:00 (server-local time of the fire_at)
curl -X POST https://klangk.example.com/api/v1/admin/host/schedule \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "restart", "at": "2026-08-24T23:00:00+02:00"}'

# Shut the host down after a 30-minute grace window
curl -X POST https://klangk.example.com/api/v1/admin/host/schedule \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "shutdown", "in_seconds": 1800}'

# List pending schedules / cancel one
curl https://klangk.example.com/api/v1/admin/host/schedule -H "Authorization: Bearer $TOKEN"
curl -X DELETE https://klangk.example.com/api/v1/admin/host/schedule/<id> \
  -H "Authorization: Bearer $TOKEN"
```

Provide `at` (absolute ISO-8601; a naive timestamp is interpreted as
UTC) or `in_seconds` (positive delay; ignored when `at` is given).
Creating a schedule requires the `admin` permission.

Rows exist only while pending — a schedule that fires or is cancelled
is deleted, so the list is always the authoritative set of upcoming
host actions.

## What clients see

- **Web UI** — a persistent banner above the workspace body showing the
  soonest pending action with a live countdown that ticks locally every
  second (e.g. `⏻ host shutdown in 1h 12m (23:00)`). It is
  non-blocking: it never gates navigation or auto-reconnect, and it
  disappears the moment no schedule is pending.
- **TUI (`klangk monitor`)** — a status line,
  `host: shutdown at 23:00 (in 1h 12m)`, plus a firing notice.
- **WebSocket** — a `host_schedule` snapshot is pushed to every
  authenticated client on change and periodically; new connections get
  the pending snapshot replayed immediately, so a client that connects
  mid-countdown learns about it at once. When the schedule fires, a
  `host_schedule_fired` event surfaces as the same transient notice the
  signal paths use ("server shutting down" / "server restarting").

## What happens when a schedule fires

The sequence mirrors the graceful stop path
([Process Signals](../deployment/signals.md)):

1. **Delete the schedule row** — a crash mid-fire cannot re-fire it.
2. **Notify** — broadcast `host_schedule_fired`; clients surface a
   "happening now" notice.
3. **Refuse new starts**, **quiesce** (wait up to
   `KLANGKD_QUIESCE_TIMEOUT` seconds for in-flight HTTP requests), then
   **drain** every running workspace through the graceful path (terminal
   stop frames + `container_stopped`, same consent/teardown as an
   immediate stop).
4. **Run the OS command** — `KLANGKD_HOST_SHUTDOWN_COMMAND` for a
   shutdown, `KLANGKD_HOST_RESTART_COMMAND` for a restart.

The commands are **empty by default — a dry run**: everything up to the
OS step still happens (notifications, teardown, drain), but no
power-off/reboot is issued, because `klangkd` typically lacks the
privilege to power off its own host. To make a schedule actually take
the host down, wire a privileged command, e.g.:

```ini
# klangkd.yaml
host_shutdown_command: "sudo systemctl poweroff"
host_restart_command: "sudo systemctl reboot"
```

with a sudoers/polkit rule allowing the `klangk` service user to run
exactly those two commands. Both settings are reloadable on SIGHUP.

Like the signal paths, budget the fire sequence inside your service
manager's deadlines if you rely on it (see the timing notes in
[Process Signals](../deployment/signals.md)).
