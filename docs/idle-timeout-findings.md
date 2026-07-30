# Idle Timeout Mechanism: Code Audit Findings

## 1. Idle Timeout Value & Configuration

**Default:** 30 minutes (1800 seconds)

**Configuration:** `KLANGKD_IDLE_TIMEOUT_SECONDS` environment variable, mapped to `settings.idle_timeout_seconds`.

- Set to `0` to disable idle-out entirely.
- Set to any positive integer to override the default.
- Invalid (non-integer) values log a warning and fall back to 1800s.

**Per-workspace override:** `IdleMonitor.set_workspace_idle_timeout(workspace_id, seconds)` sets `ContainerState.idle_timeout` for a single workspace. When set, it takes precedence over the global default. There is no user-facing UI or API to set this today; it's internal-only.

**Scope:** Global by default, with per-workspace override capability (unused externally).

**Code:** `container.py:743-760` (`_parse_idle_timeout`), `container.py:109-112` (`get_idle_timeout` fallback chain).

## 2. Check Interval

The idle monitor wakes periodically to scan all containers:

- **Default interval:** `timeout // 3`, clamped to `[10, 60]` seconds. At the default 1800s timeout, this is **60 seconds**.
- **With per-workspace overrides:** `max(2, min(per_workspace_timeouts) // 2)` — adapts to the shortest workspace timeout.
- The monitor can also be woken early via `asyncio.Event` (e.g. when a per-workspace timeout is changed).

**Code:** `container.py:246-263` (`cleanup_idle_containers` loop).

## 3. What Counts as "Activity"

Activity is recorded by calling `ContainerState.record_activity()`, which sets `last_activity = time.time()`.

### Signals that reset the idle timer

| Signal                                        | File             | Line      | Notes                                                        |
| --------------------------------------------- | ---------------- | --------- | ------------------------------------------------------------ |
| Client heartbeat (`{"cmd": "heartbeat"}`)     | `connection.py`  | 621-625   | Both TUI and Flutter clients send this every 60s             |
| Terminal input (keystrokes)                   | `controllers.py` | 798-800   | User typing in a terminal session                            |
| Terminal output (data from container)         | `controllers.py` | 1137-1139 | Any output from the container's PTY                          |
| Terminal resize                               | `controllers.py` | 1090-1092 | Window resize events                                         |
| Terminal session activation (attach/reattach) | `controllers.py` | 1090      | When a terminal session starts or is reattached              |
| Exec session start                            | `controllers.py` | 370       | Starting an exec session                                     |
| Exec input                                    | `controllers.py` | 382-384   | Sending input to an exec session                             |
| Exec output                                   | `controllers.py` | 426-428   | Receiving output from an exec session                        |
| Container restart                             | `connection.py`  | 527       | After a manual restart                                       |
| File API operations                           | `files.py`       | 39        | list/read/delete/rename/download/upload                      |
| Container start                               | `container.py`   | 861       | `ContainerState.__init__` sets `last_activity = time.time()` |

### Signals that do NOT reset the idle timer

| Signal                                              | Why not                                                         |
| --------------------------------------------------- | --------------------------------------------------------------- |
| HTTP traffic through the reverse proxy (`proxy.py`) | Proxy has no `record_activity` call                             |
| Container CPU/memory activity (busy loops)          | No resource-based activity tracking                             |
| Volume/filesystem writes inside the container       | Only the File API (host-side) records activity                  |
| Health check polling                                | Health monitor is separate; checks don't count as user activity |
| WebSocket connection/disconnection                  | Connecting alone doesn't reset the timer                        |

**Notable gap:** If a user has a hosted app running through the proxy and is actively using it in a browser (HTTP requests), this does **not** keep the container alive. The container could idle out while the user is actively using a proxied web app, unless a terminal or the Flutter/TUI client is also connected (sending heartbeats).

## 4. Idle-Out Action: Stop, Not Suspend

On idle timeout, the container is **stopped and removed** — not suspended or checkpointed.

**Flow:**

1. `cleanup_idle_containers` detects `now - last_activity > timeout` (`container.py:275`)
2. Idle callbacks are invoked — each connected client gets a `container_stopped` event with reason `"idle timeout"` (`connection.py:317-325`)
3. `notify_workspace_killed` is called (triggers any registered workspace-kill callback) (`container.py:291`)
4. `stop_and_remove_container` calls `podman.remove_container(container_id)` to stop and remove the container (`container.py:1724-1725`)
5. Registry state is cleaned up: `_cid_to_wsid` mapping removed, workspace browsers revoked, `ContainerState` popped (`container.py:1739-1742`)

**There is no suspend/checkpoint mechanism.** All container state (running processes, in-memory data) is lost on idle-out. Only the workspace volume (persistent storage) survives.

## 5. Client Keep-Alive Heartbeat

Both clients send a `{"cmd": "heartbeat"}` message every **60 seconds**:

- **Flutter Web client:** `Timer.periodic(Duration(seconds: 60), ...)` in `ws_client.dart:726-729`. Starts after `container_ready` event; stops on disconnect.
- **CLI/TUI client:** `heartbeat_loop()` in `cli/client.py:1259-1268`. Runs as an async task during terminal/exec sessions; sends every 60s until the session stops.

**Consequence:** An active client connection (with a terminal open or the Flutter app connected to a workspace) will never idle out, because the heartbeat resets the timer every 60s, well within the 1800s (or any reasonable) timeout.

**Edge case:** If the client crashes or the network drops without a clean disconnect, the server won't receive heartbeats. The container will idle out after the configured timeout (default 30 min) from the last received heartbeat.

## 6. Wake from Idle

**No automatic wake.** A user must explicitly restart the container.

- **WebSocket command:** `restart_container` handler in `connection.py:496-527`. Requires admin permission. Calls `cleanup()` then `start_workspace_container()` to launch a fresh container.
- **Client behavior:** When the client receives a `container_stopped` event, it shows a "container stopped" message. The user can click "Restart" (Flutter) or use the restart command (TUI/CLI) to bring it back.

The restarted container is a **fresh container** — same workspace volume, but new container ID, new processes. It's equivalent to a first-time start.

## 7. Code Locations Summary

| Component                              | File             | Key Lines       |
| -------------------------------------- | ---------------- | --------------- |
| `ContainerState` (per-workspace state) | `container.py`   | 56-113          |
| `IdleMonitor` (idle detection loop)    | `container.py`   | 200-306         |
| `_parse_idle_timeout` (config parsing) | `container.py`   | 743-760         |
| `stop_and_remove_container`            | `container.py`   | 1703-1747       |
| `record_activity` (registry-level)     | `container.py`   | 872-877         |
| `handle_heartbeat` (server-side)       | `connection.py`  | 621-625         |
| `handle_restart_container`             | `connection.py`  | 496-527         |
| Idle callback registration             | `connection.py`  | 317-330         |
| Terminal I/O activity                  | `controllers.py` | 798, 1090, 1137 |
| Exec I/O activity                      | `controllers.py` | 370, 382, 426   |
| File API activity                      | `files.py`       | 39              |
| Heartbeat dispatch                     | `dispatch.py`    | 53              |
| Flutter heartbeat timer                | `ws_client.dart` | 726-729         |
| CLI heartbeat loop                     | `cli/client.py`  | 1259-1268       |
| Settings field                         | `settings.py`    | 595             |
| Proxy (no activity tracking)           | `proxy.py`       | —               |

## 8. Potential Issues / Drift

1. **Proxy traffic doesn't reset idle timer.** Users with hosted web apps that receive only HTTP traffic (no terminal open, no Flutter client connected) will see their container killed after the idle timeout despite active use of the proxied app.

2. **No suspend/restore.** Idle-out is destructive — all running processes are lost. This is simple but may surprise users who expect their running services to survive idle-out.

3. **Heartbeat interval (60s) vs. check interval (60s).** These are coincidentally equal at the default timeout. With very short custom timeouts (e.g. 30s), the heartbeat interval remains 60s, meaning a heartbeat might arrive too late. The check interval adapts (`timeout // 3` clamped to 10-60s), but the client heartbeat does not.

4. **Per-workspace timeout is internal-only.** The `set_workspace_idle_timeout` API exists but is not exposed via any user-facing command or REST endpoint.
