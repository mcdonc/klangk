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
   `--hooks-dir <dir>` to `podman create`.
3. The OCI hook (`klangk-netfilter.sh`, materialized by the backend into the
   hooks dir) fires at `createContainer` time, reads the annotation from the
   container state, resolves each host to IPs, and installs an iptables
   ruleset in the container's network namespace (via `nsenter` on the init
   pid).
4. The default `OUTPUT` policy is `DROP`; loopback, established
   connections, DNS (udp/tcp 53), the backend gateway
   (`host.containers.internal`), and the resolved allowed destinations are
   `ACCEPT`ed. Everything else is dropped.

The hook runs **before** the container process starts, so the ruleset is in
place and immutable before any user code runs — `CAP_NET_ADMIN` is dropped
by the runtime before the container entrypoint executes.

## Enabling it (operator)

1. Ensure `iptables`, `getent`, and `nsenter` are available where the OCI
   runtime executes (the host, or the Docker-in-Docker outer container —
   _not_ the workspace image). The documented DinD deployment already has
   `CAP_SYS_ADMIN` + `seccomp=unconfined`, which provides the necessary
   privileges.
2. Set `KLANGKD_NETFILTER_HOOKS_DIR` to a directory the OCI runtime can
   read. At startup klangkd writes the hook script (`klangk-netfilter.sh`)
   and its config (`klangk-netfilter.json`) into that directory, so it just
   needs to exist (or be creatable). The path must be resolvable where the
   runtime executes — for the DinD / bare-Linux deployments this is any
   host path; for `podman machine` on macOS it must be inside the CoreOS VM
   (install into the VM or bake into a custom machine image), since
   `podman machine` does not bind-mount arbitrary host paths the way Docker
   Desktop does.

   ```bash
   export KLANGKD_NETFILTER_HOOKS_DIR=/var/lib/klangk/netfilter-hooks
   ```

3. Restart klangkd. The log shows
   `Netfilter egress filtering enabled: OCI hooks installed in <dir>`.

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
- `host:port` allows a single TCP port.
- Each entry is validated server-side; malformed entries are rejected with
  HTTP 400.
- An empty list (or `null`) means **unrestricted** — the workspace is never
  filtered.

A restart of the workspace container applies the change to a running
workspace (the ruleset is set at create time).

## Fail-open behavior

If a workspace declares `allowed_domains` but netfilter is **not** enabled
on the server (`KLANGKD_NETFILTER_HOOKS_DIR` unset or unwritable), the
workspace starts **unrestricted** and the server logs a loud warning. The
`allowed_domains` value is still persisted, so it takes effect the moment
the operator enables netfilter. This is deliberate: a misconfigured deploy
degrades to the unrestricted baseline rather than making workspaces
unusable, but the warning makes the gap visible.

## Caveats

- **DNS resolution at creation time.** iptables matches IPs, so hostnames
  are resolved when the container is created. If a service rotates IPs
  (common with CDNs), access may break until the workspace is restarted.
  Mitigation: allow a port without pinning a host, or allow a CIDR
  range (a possible future enhancement; the initial implementation is
  `host`/`host:port` only).
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
