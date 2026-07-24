# Per-workspace network egress filtering

Klangk can restrict which external hosts a workspace container may reach,
so a deployment running AI agents or untrusted code isn't an open
exfiltration vector. The filter is **opt-in** per workspace _and_ per
deploy: a workspace with no `allowed_domains` keeps unrestricted outbound
networking exactly as before.

The mechanism uses OCI `createContainer` hooks — there is no proxy, no TLS
interception, and no microVM. Each workspace that declares an allow-list
gets iptables rules injected into its network namespace before its process
starts.

## How it works

1. A workspace carries an `allowed_domains` list (`host` or `host:port`
   specs).
2. On container start, if the deploy has enabled netfilter, the backend
   passes `--annotation klangk.netfilter.rules=<host:port,...>` and
   `--hooks-dir <dir>` to `podman create` (see the caveat below on
   `--hooks-dir` overriding default hook dirs).
3. The OCI hook (`klangk-netfilter.sh`, materialized by the backend into the
   hooks dir) fires at `createContainer` time, reads the annotation from the
   container state, resolves each host to IPs, and installs an iptables
   ruleset in the container's network namespace (via `nsenter` on the init
   pid).
4. The default `OUTPUT` policy is `DROP`; loopback, established
   connections, **DNS to the container's configured resolvers only**
   (read from its `/etc/resolv.conf`, not a blanket `udp/tcp 53` allow),
   the backend gateway (`host.containers.internal`, resolved from the
   container's `/etc/hosts`), and the resolved allowed destinations are
   `ACCEPT`ed. Everything else is dropped.

The hook runs **before** the container process starts, so the ruleset is in
place and immutable before any user code runs — `CAP_NET_ADMIN` is dropped
by the runtime before the container entrypoint executes.

## Enabling it (operator)

Netfilter is **armed by default**. At startup klangkd materializes the hook
script (`klangk-netfilter.sh`) and its config (`klangk-netfilter.json`)
into a hooks directory and registers the OCI `createContainer` hook — no
configuration is required for the common case.

1. Ensure `iptables`, `getent`, and `nsenter` are available where the OCI
   runtime executes (the host, or the Docker-in-Docker outer container —
   _not_ the workspace image). The documented DinD deployment already has
   `CAP_SYS_ADMIN` + `seccomp=unconfined`, which provides the necessary
   privileges.
2. The hooks dir defaults to `<state_dir>/oci-hooks`
   (`KLANGKD_STATE_DIR`/`oci-hooks`). Override
   `KLANGKD_NETFILTER_HOOKS_DIR` only when the OCI runtime can't see
   `state_dir` — a split runtime, a DinD outer container, or a
   `podman machine` CoreOS VM (where it must be inside the VM, since
   `podman machine` does not bind-mount arbitrary host paths the way
   Docker Desktop does):

   ```bash
   export KLANGKD_NETFILTER_HOOKS_DIR=/var/lib/klangk/netfilter-hooks
   ```

3. Restart klangkd. The log shows
   `Netfilter egress filtering enabled: OCI hooks installed in <dir>`.

To **disable** netfilter entirely (e.g. an environment without
`iptables`/`nsenter`, or where the hook can't be granted `CAP_NET_ADMIN`),
set `KLANGKD_NETFILTER_ENABLED=false` (or YAML `netfilter_enabled: false`).
When disabled, `enabled()` reports false, `--hooks-dir` is never passed,
and workspaces with `allowed_domains` fail open with a loud warning
(#1769). (#1774)

## Configuring a workspace

Set `allowed_domains` via the workspace **Settings** panel (an
"Allowed Domains" list editor under Mounts / Environment Variables) or the
API:

```bash
curl -X PUT https://klangkd/api/v1/workspaces/<id> \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"allowed_domains": ["github.com:443", "pypi.org", "registry.npmjs.org"]}'
```

- `host` allows all ports to that host.
- `host:port` allows a single TCP port (port must be 1–65535).
- Each entry is validated server-side; malformed entries are rejected with
  HTTP 400.
- An empty list (or `null`) **inherits the deploy-wide default**
  (`KLANGKD_NETFILTER_DEFAULT_DOMAINS`); if no default is configured, the
  workspace is **unrestricted**. There is currently no per-workspace opt-out
  into truly-unrestricted egress when a deploy default is set — clear the
  default server-side to permit unrestricted workspaces.

A restart of the workspace container applies the change to a running
workspace (the ruleset is set at create time).

## Fail-open behavior

If a workspace declares `allowed_domains` but netfilter is **not armed**
on the server — disabled via `KLANGKD_NETFILTER_ENABLED=false`, the hooks
dir unwritable, or the hook not installed/current (#1771) — the workspace
starts **unrestricted** and the server logs a loud warning. The
`allowed_domains` value is still persisted, so it takes effect the moment
netfilter is armed. The workspace's Settings panel and list row also badge
the gap (#1769), so the user who set the list sees it — not just operator
logs. This is deliberate: a misconfigured deploy degrades to the
unrestricted baseline rather than making workspaces unusable, but the
warning makes the gap visible.

## Caveats

- **DNS resolution at creation time.** iptables matches IPs, so hostnames
  are resolved when the container is created. If a service rotates IPs
  (common with CDNs), access may break until the workspace is restarted.
  Mitigation: allow a port without pinning a host, or allow a CIDR
  range (a possible future enhancement; the initial implementation is
  `host`/`host:port` only).
- **DNS is pinned to resolvers, not blocked entirely.** Outbound `:53` is
  accepted only to the nameservers in the container's `/etc/resolv.conf`,
  so a workspace cannot talk to an arbitrary host on port 53. This does
  **not** prevent DNS tunneling through those permitted resolvers to
  attacker-controlled domains (data can still be encoded in DNS queries).
  Treat the filter as an egress allow-list, not a complete anti-exfiltration
  guarantee against DNS-based channels.
- **Ruleset immutability depends on the runtime capability set.** The
  hook installs the iptables rules before the container entrypoint starts,
  and a filtered workspace also has `NET_ADMIN` dropped explicitly
  (`--cap-drop NET_ADMIN`). `NET_ADMIN` is already absent from podman's
  default capability set, so this is a no-op under defaults and defense
  in depth against an operator override. It is **not** a hard guarantee:
  running the workspace `--privileged`, adding `--cap-add NET_ADMIN`, or a
  permissive seccomp profile hands the entrypoint `iptables -F OUTPUT`,
  which flushes the ruleset and lets it exfiltrate freely. Do not run
  filtered workspaces privileged or grant `NET_ADMIN`.
- **`--hooks-dir` overrides podman's default hook dirs.** Podman's
  `--hooks-dir` flag _replaces_ (does not append to) the default OCI hook
  search paths, so passing only klangk's hooks dir for a filtered
  workspace would silently disable every _other_ `createContainer` hook
  an operator relies on (monitoring, secrets injection, GPU, corporate
  integrations). To avoid that, a filtered container passes klangk's hooks
  dir **and** the two standard default dirs
  (`/usr/share/containers/oci/hooks.d`, `/etc/containers/oci/hooks.d`),
  preserving operator hooks. Podman tolerates a dir that doesn't exist (it
  simply finds no hooks there). Limitation: a _non-standard_ hooks dir
  configured only via `containers.conf` is still clobbered by an explicit
  `--hooks-dir`; unrestricted workspaces are unaffected (the flag isn't
  passed). See #1770.
- **Port granularity.** The initial implementation supports `host` and
  `host:port`. CIDR ranges and port-only rules may follow.
- **`macOS` hosts.** The `createContainer` hook runs inside the
  container's Linux network namespace, never the macOS (XNU) kernel, so
  `iptables` availability is not host-dependent. For the DinD deployment
  there is no macOS-specific concern. For a native-on-mac deployment
  driving `podman machine`, ensure the `--hooks-dir` path and
  `klangk-netfilter.sh` are resolvable from inside the CoreOS VM.

## References

- [Podman maintainer discussion on OCI hooks for iptables][podman-disc]
- [Working OCI hooks + iptables implementation][jerabaul29]
- [OCI runtime spec — hooks][oci-hooks]

[podman-disc]: https://github.com/containers/podman/discussions/27099
[jerabaul29]: https://github.com/jerabaul29/2025_podman_iptable_rules
[oci-hooks]: https://github.com/opencontainers/runtime-spec/blob/main/config.md#posix-platform-hooks
