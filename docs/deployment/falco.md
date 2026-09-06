# Falco Exec Audit

Falco is a host-wide syscall monitor. Run as a privileged container next to
the klangk host container, it records every command execution (`execve`,
`execveat`) on the machine — including commands typed in klangk workspace
terminals — to a JSON file that an unprivileged consumer can read back.
This chapter documents the verified deployment procedure and the field
semantics a consumer can rely on (#2780).

The verification ran Falco 0.44.1 (modern eBPF engine, no kernel module or
driver download) on the deployment host alongside a klangk host container
running a workspace through the web terminal.

## Run the Falco container

The container needs `--privileged` (the eBPF engine loads BPF programs and
opens per-CPU ring buffers) and a read-only view of host `/proc`, `/sys`,
and `/dev` for container enrichment and machine identity. `--pid=host`
runs Falco in the host PID namespace so its BPF iterators can enumerate
existing threads at startup — the same shape the Falco Helm chart uses.

```bash
mkdir -p /opt/falco/rules.d /opt/falco/out

docker run -d --name falco \
  --privileged --pid=host \
  --restart unless-stopped \
  -v /sys:/host/sys:ro \
  -v /proc:/host/proc:ro \
  -v /dev:/host/dev \
  -v /etc/machine-id:/etc/machine-id:ro \
  -v /var/run/docker.sock:/host/var/run/docker.sock \
  -v /opt/falco/falco.yaml:/etc/falco/falco.yaml:ro \
  -v /opt/falco/rules.d:/etc/falco/rules.d:ro \
  -v /opt/falco/out:/var/log/falco \
  falcosecurity/falco:0.44.1
```

Mounting `/var/run/docker.sock` read-only lets Falco's container engine
resolve Docker container names and images. It is optional; without it,
enrichment falls back to cgroup-path parsing and still yields container
ids, but `container.name` stays empty for Docker containers.

## Configuration

Three edits to the stock `/etc/falco/falco.yaml` (the file inside the
image, `docker run --rm falcosecurity/falco:0.44.1 cat /etc/falco/falco.yaml`,
makes a fine starting point):

```yaml
# Structured output, one JSON object per line, continuously written.
json_output: true
syslog_output:
  enabled: false
file_output:
  enabled: true
  keep_alive: true
  filename: /var/log/falco/events.json

# Heartbeat + counters every 10s: this is the stall/drift signal a
# consumer watchdog reads (see "Livelock" below).
metrics:
  enabled: true
  interval: 10s
  output_rule: true

# The stock engine stanza already selects modern_ebpf. Widen the
# buffers if the host is busy:
engine:
  kind: modern_ebpf
  modern_ebpf:
    cpus_for_each_buffer: 1
    buf_size_preset: 5

# Trace only what exec auditing needs. This cuts the syscall torrent
# (and Falco's CPU) dramatically on busy hosts.
base_syscalls:
  custom_set:
    [
      clone,
      clone3,
      fork,
      vfork,
      execve,
      execveat,
      setresuid,
      setresgid,
      setsid,
      setuid,
      setgid,
      setpgid,
      capset,
    ]
```

The `rules_files` list keeps its stock entry for `/etc/falco/rules.d`, so a
rule file dropped there loads automatically. If you audit with a custom
rule only, point `rules_files` at the rules directory alone (`rules_files:
[/etc/falco/rules.d]`) — the stock rule set forces a much wider syscall
net.

## The exec rule

Drop into `/opt/falco/rules.d/klangk-exec.yaml`:

```yaml
- rule: klangk exec stream
  desc: >
    Every command execution on the host with full argv, parent argv, uid,
    namespace-relative pid (vpid), init-namespace pid, and container
    metadata.
  condition: evt.type in (execve, execveat) and evt.dir=<
  output: >
    klangk-exec
    container_id=%container.id
    container_name=%container.name
    exe=%proc.exepath
    vpid=%proc.vpid
    pid=%proc.pid
    uid=%user.uid
    cmdline=%proc.cmdline
    pcmdline=%proc.pcmdline
  priority: INFORMATIONAL
  tags: [klangk, exec]
```

Host processes carry `container.id=host`; keep them in the stream so a
failed enrichment shows up as an unlabeled event instead of a silent gap.
Note that `proc.cmdline` reflects the kernel's `argv`: shell builtins
(`echo`, `cd`) never appear because no `execve` occurs, and BusyBox
`ash` runs many applets in-process (fork without `execve`), so those do
not appear either — a workspace terminal runs `bash`, where `echo` is a
builtin but `ls`, `cat`, `true` (via `exec /bin/true`), `setsid`, etc.
all surface.

## Verified field semantics (workspace containers)

Commands were typed into a workspace terminal (the web-terminal WebSocket
path: `workspace_connect` → `terminal_start` → `terminal_input`) in a
workspace container nested rootless-podman-inside the klangk host
container, and matched against the Falco stream. Results:

| Typed command                             | Surfaced | `container.id`    | `proc.vpid`         |
| ----------------------------------------- | -------- | ----------------- | ------------------- |
| `ls -al /etc/os-release`                  | yes      | host container id | in-workspace pid    |
| `cat /etc/os-release`                     | yes      | host container id | in-workspace pid    |
| `exec /bin/true` (sub-millisecond)        | yes      | host container id | 46 = tmux pane pid  |
| `setsid /bin/sleep 600`                   | yes      | host container id | in-workspace pid    |
| klangkd plumbing (`podman exec … tmux …`) | yes      | host container id | outer-container pid |

- **Capture works through the nesting.** Every `execve` inside the
  workspace container reaches the Falco stream with full `proc.cmdline`,
  `proc.pcmdline`, `proc.exepath`, and `user.uid`.
- **`proc.vpid` is the join anchor.** Falco reports the pid in the
  process's own (innermost) PID namespace — the same numbering `ps`
  inside the workspace shows and the number tmux reports via
  `#{pane_pid}`. In the verification, the pane's shell and its
  sub-millisecond `exec /bin/true` surfaced with `proc.vpid` equal to
  the tmux pane pid the terminal itself echoed (46). A consumer joins
  exec events to terminal attribution on exactly this number.
- **`container.id` names the klangk host container, not the workspace
  container.** Nested rootless podman runs the workspace container
  inside the host container's cgroup — without host-side cgroup
  delegation it stays in the flat `docker-<hostcontainer>.scope`, so the
  workspace container's own id appears nowhere in the cgroup path. Falco
  enrichment attributes every workspace process to the outer container
  (id, name, image). Attributing an exec to a _workspace_ requires
  correlating on klangkd side channels (pid + argv + timestamp), not on
  Falco's container fields.
- **`user.uid` is the host-side uid.** The workspace user (`klangk`,
  uid 1000 inside the workspace) maps to uid 1000 on the host through
  both user namespaces, and Falco reports 1000.
- **Bare-metal klangkd is different.** When klangkd runs directly on the
  host (devenv, packaged binary), its rootless-podman workspace
  containers get their own cgroup scopes and Falco's cgroup parsing
  enriches them with the workspace container's own (12-char truncated)
  id — `container.name` stays empty because Falco cannot reach the
  rootless podman socket.

## Consumption channel

Falco writes `/var/log/falco/events.json` — one JSON object per line,
world-readable (`0644`), growing append-only. A consumer needs only a
read-only bind mount of the output directory; the verification read the
file from an unprivileged container (no capabilities, `--user 1000`)
mounted `-v /opt/falco/out:/falco:ro`. Falco performs no rotation; the
consumer tails and rotates.

Each line carries `time`, `rule`, `output_fields` (the structured
fields the rule named: `container.id`, `proc.vpid`, `proc.pid`,
`user.uid`, `proc.cmdline`, `proc.pcmdline`, `proc.exepath`), and the
metrics snapshots arrive on the same file as `"rule": "Falco internal:
metrics snapshot"` lines.

Falco also offers a gRPC output (`grpc_output`, Unix socket) — richer
streaming semantics, at the cost of more moving parts. It stayed
unconfigured in this verification; the JSON file is the verified channel.

## Livelock: stall watchdog is mandatory

Falco 0.44.1's modern eBPF engine wedges on this heavily loaded host:
output stops entirely (both file and stdout) while the process keeps
burning ~1.3 CPU cores, the kernel counters keep climbing, and zero
drops are reported. Observed time-to-stall ranged from 80 seconds to 19
minutes depending on load. This matches the open upstream livelock
report (falcosecurity/falco#3822); buffer and syscall-set tuning reduced
CPU but did not prevent it.

Falco emits no owned heartbeat, so treat the metrics snapshot as one:
with `metrics.interval: 10s`, a consumer that sees no snapshot line (and
no event) for a few intervals declares the feed dead. On the deployment
host, run a watchdog that restarts the container when the output file
goes stale — coverage resumes in seconds:

```bash
#!/usr/bin/env bash
# Restart Falco when its output goes stale (livelock, falco#3822).
set -u
while true; do
  sleep 30
  [ "$(docker inspect -f '{{.State.Running}}' falco 2>/dev/null)" = "true" ] || continue
  age=$(( $(date +%s) - $(stat -c %Y /opt/falco/out/events.json 2>/dev/null || echo 0) ))
  if [ "$age" -gt 120 ]; then
    echo "$(date -u +%FT%TZ) falco stalled (${age}s), restarting" >> /opt/falco/watchdog.log
    docker restart falco
  fi
done
```

A restart loses only the events of the stall window; the ring buffers
are recreated and capture resumes immediately.
