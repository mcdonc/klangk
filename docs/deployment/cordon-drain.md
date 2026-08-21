# Cordon & drain (operator maintenance workflow)

The k8s cordon/drain pair for klangkd (#2527): quiesce a host before an
upgrade, reboot, or maintenance window without dirty container deaths.

- **Cordon** — the node refuses _new_ workspace starts. Existing
  workspaces keep running. Every start path honors it: the API start /
  restart endpoints (503 with a clear message), WebSocket connects (an
  error frame), eager starts on create, boot auto-start on a klangkd
  restart, and crash-recovery restarts.
- **Drain** — gracefully stop every running workspace through the same
  path as logout / idle timeout: connected clients receive terminal
  status frames and a `container_stopped` event carrying the reason, so
  the UI shows a clean shutdown instead of a dropped WebSocket.

The cordon flag is persisted in the database (`server_state` table), so
it survives klangkd restarts: a crash-looping service that comes back up
stays cordoned and does **not** re-start user workspaces — mirroring
k8s's cordon-on-boot. This is deliberate: an operator investigating a
sick host wants the quiesce to hold.

## Commands (admin)

```bash
klangk admin cordon          # refuse new starts
klangk admin drain           # cordon + stop all running workspaces
klangk admin cordon-status   # show the flag
klangk admin uncordon        # re-enable starts
```

`drain` cordons first by default so nothing restarts behind the drain's
back; `--no-cordon` skips that (users may restart workspaces
mid-drain), and `--uncordon` uncordons afterwards for scripted
maintenance windows that end automatically. The exit status reflects
API failures, so wrappers can wait on completion; `drain` reports the
number of workspaces stopped.

The HTTP surface behind them (if you script against the API directly):

| Method | Path                   | Effect                                        |
| ------ | ---------------------- | --------------------------------------------- |
| GET    | `/api/v1/admin/cordon` | Read the flag                                 |
| PUT    | `/api/v1/admin/cordon` | Set/clear (`{"cordoned": true}`)              |
| POST   | `/api/v1/admin/drain`  | Stop all running workspaces, `{"stopped": n}` |

Authenticated clients also see `cordoned` in `GET /api/v1/config` (the
web UI can badge the state); the pre-auth payload does not include it.

## systemd deployment (system Python)

For hosts running klangkd under systemd with the system Python
(Ubuntu/Arch `pip install klangk` + a unit file), the full upgrade
cycle is:

```bash
# 1. Quiesce: no new starts, then stop everything gracefully.
klangk admin cordon
klangk admin drain

# 2. Upgrade (unit restart / host reboot / pip install of the new version).
sudo systemctl restart klangkd      # or: reboot

# 3. Resume.
klangk admin uncordon
```

Because drain already quiesced every workspace, the unit stop cannot
kill containers mid-flight (klangkd's own SIGTERM path stops
containers too, but with drain you choose _when_ it happens, and
clients saw clean stop frames).

A unit can also wire the drain into its stop:

```ini
[Service]
ExecStart=/usr/local/bin/klangkd --config /etc/klangk/klangkd.yaml
ExecStop=/usr/bin/klangk admin drain --uncordon
```

`ExecStop` runs on `systemctl stop`/reboot, giving a graceful drain
before the process goes away. The `--uncordon` there is a policy
choice: it assumes the _next_ start (after upgrade or reboot) should
serve traffic again. Omit it to stay cordoned across the restart and
uncordon by hand once you've verified the upgrade — the safer default
when a human is watching the window.

The klangkd package does not ship unit files; the snippet above is the
shape that matters (`ExecStop` = drain), adapted to wherever your
`klangk`/`klangkd` binaries live.

## Docker deployment (host container)

The docker host-container deployment gets the same protection from the
volume-mounted database (`/home/klangk/data` carries the cordon flag
across container replacements):

```bash
# 1. Quiesce.
klangk admin cordon
klangk admin drain

# 2. Replace the container (drain already stopped the nested workspace
#    containers, so docker's SIGTERM -> SIGKILL window has nothing
#    left to kill mid-flight).
docker stop klangk && docker rm klangk
docker run ... ghcr.io/mcdonc/klangk/klangk-host:<version>

# 3. Resume.
klangk admin uncordon
```

Docker has no `ExecStop` equivalent; run the drain explicitly before
`docker stop` as shown. A crash-looping container restarted by a docker
restart policy reads the persisted flag and stays cordoned — no
auto-started workspaces until an operator uncordons.

## What cordon does NOT do

- It does not disconnect connected clients — terminals to _running_
  workspaces keep working between cordon and drain.
- It does not evict users or block the API otherwise; only workspace
  _starts_ are refused.
- Drain does not delete workspaces — everything restarts on the next
  start (or boot auto-start, once uncordoned).
