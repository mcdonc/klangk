# Egress sidecar — FQDN DNS proxy (#2250, #2253)

This image runs the per-workspace egress DNS proxy for FQDN-based egress
filtering. A **filtered** workspace runs as two containers sharing one network
namespace: this sidecar (`--cap-add NET_ADMIN`, owns the iptables ruleset +
the DNS proxy) and the workspace (`--network container:<sidecar>`,
unprivileged). Unfiltered workspaces are unaffected (single container).

## How it works

1. `entrypoint.sh` installs a default-deny `OUTPUT` ruleset + a nat `REDIRECT`
   of the workspace's configured DNS resolvers (`:53`) to the proxy's listen
   port (`127.0.0.1:15353`).
2. The proxy (the `klangksidecar` package, built from `src/klangksidecar` and
   installed into the image as a wheel) applies the FQDN allow-list, forwards
   allowed queries to a **different** upstream, learns the A-record IPs, and
   inserts `iptables -I OUTPUT 1 -d <ip> -j ACCEPT` for each — so the workspace
   can reach exactly the IPs it resolved (solving DNS round-robin). Denied
   names get NXDOMAIN.

The destination-based REDIRECT (only the workspace's resolvers) + a distinct
upstream is the loop-avoidance: the proxy's own forwards aren't redirected.

## Build

The image installs the `klangksidecar` wheel (the proxy package), staged into
the build via a named context, so use the wrapper script (it builds the wheel

- passes `--build-context sidecar=`):

```bash
devenv shell -- bash scripts/build-network-sidecar.sh
```

## Run (klangk wires this via #2254)

```bash
podman run -d --name <ws>-egress --cap-add NET_ADMIN \
  --dns 1.1.1.1 \
  -e KLANGKNETWORK_EGRESS_ALLOW=github.com:443,pypi.org \
  -e KLANGKNETWORK_EGRESS_UPSTREAM=8.8.8.8 \
  klangk-network-sidecar
podman run -d --name <ws> --network container:<ws>-egress <workspace-image> ...
```

Constraint: `KLANGKNETWORK_EGRESS_UPSTREAM` (default `8.8.8.8`) **must differ** from the
workspace's configured resolvers (the sidecar's `--dns`, which the workspace
inherits) — otherwise the proxy's forwards loop back into itself. `entrypoint.sh`
skips any nameserver equal to the upstream as a guard.

### Backend reachability

The workspace must reach the klangkd backend (`/llm-proxy`, the bridge) on
`host.containers.internal` to function. That host is a `/etc/hosts` entry (podman
populates it under `--network container:`), not a DNS lookup, so the FQDN proxy
can never learn its IP. `entrypoint.sh` therefore statically allow-lists
`host.containers.internal:<KLANGKNETWORK_EGRESS_BACKEND_PORT>` (resolved via `getent`),
scoped to that one port — the klangkd backend is itself authenticated. Pass the
klangkd `egress_port` here (#2254 B1).

## Configuration (env)

| var                                   | default    | meaning                                                                                                    |
| ------------------------------------- | ---------- | ---------------------------------------------------------------------------------------------------------- |
| `KLANGKNETWORK_EGRESS_ALLOW`          | _(empty)_  | comma-separated allow-list: `host[:port]`, `*.domain[:port]`, or CIDR                                      |
| `KLANGKNETWORK_EGRESS_MODE`           | `static`   | how off-list names are treated: `static` NXDOMAINs them; `interactive`/`allow` resolve + record (SYN gate) |
| `KLANGKNETWORK_EGRESS_UPSTREAM`       | `8.8.8.8`  | real upstream the proxy forwards to                                                                        |
| `KLANGKNETWORK_EGRESS_BACKEND_PORT`   | _(empty)_  | klangkd backend port on host.containers.internal                                                           |
| `KLANGKNETWORK_EGRESS_LISTEN_PORT`    | `15353`    | UDP port the proxy listens on                                                                              |
| `KLANGKNETWORK_EGRESS_MARK`           | `75`       | fwmark for the proxy's upstream socket (must match entrypoint.sh)                                          |
| `KLANGKNETWORK_EGRESS_SWEEP_INTERVAL` | `5`        | seconds between learned-IP TTL-expiry sweeps                                                               |
| `KLANGKNETWORK_EGRESS_MIN_TTL`        | `30`       | floor for a learned IP's lifetime (a 0-TTL response must not yank the rule)                                |
| `KLANGKNETWORK_IPTABLES`              | `iptables` | iptables binary path                                                                                       |
| `KLANGKNETWORK_EGRESS_DEBUG`          | unset      | if set, log each allow/deny decision                                                                       |

## Allow-list semantics (#2256)

- `host` allows all ports to the host **and its subdomains**; `host:port`
  scopes a learned IP to one TCP port.
- `*.domain[:port]` allows **subdomains only** (not the apex) — distinct
  from a bare `domain`.
- A resolved IP is allow-listed **only for the DNS response's TTL**; the
  proxy re-resolves on each query and a background sweep removes the rule
  once the TTL elapses.
- CIDR specs (`10.0.0.0/8`, `10.0.0.0/8:443`) are installed statically by
  the entrypoint (not resolved); a `cidr:port` scopes the rule to that port.

## References

- #2250 (FQDN egress epic), #2253 (this image), #2254 (klangk lifecycle wiring),
  #2255 (migrate the filter into the sidecar), #2256 (allow-list semantics).
- Spike validation: the sidecar natively sharing the netns intercepts the
  workspace's DNS; the full forward/learn/allow + NXDOMAIN chain is proven.
