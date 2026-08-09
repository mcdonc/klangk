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
2. `proxy.py` applies the FQDN allow-list, forwards allowed queries to a
   **different** upstream, learns the A-record IPs, and inserts
   `iptables -I OUTPUT 1 -d <ip> -j ACCEPT` for each — so the workspace can
   reach exactly the IPs it resolved (solving DNS round-robin). Denied names
   get NXDOMAIN.

The destination-based REDIRECT (only the workspace's resolvers) + a distinct
upstream is the loop-avoidance: the proxy's own forwards aren't redirected.

## Build

```bash
podman build -t klangk-egress-sidecar -f src/containers/egress-sidecar/Dockerfile src/containers/egress-sidecar
```

## Run (klangk wires this via #2254)

```bash
podman run -d --name <ws>-egress --cap-add NET_ADMIN \
  --dns 1.1.1.1 \
  -e KLANGK_EGRESS_ALLOW=github.com:443,pypi.org \
  -e KLANGK_EGRESS_UPSTREAM=8.8.8.8 \
  klangk-egress-sidecar
podman run -d --name <ws> --network container:<ws>-egress <workspace-image> ...
```

Constraint: `KLANGK_EGRESS_UPSTREAM` (default `8.8.8.8`) **must differ** from the
workspace's configured resolvers (the sidecar's `--dns`, which the workspace
inherits) — otherwise the proxy's forwards loop back into itself. `entrypoint.sh`
skips any nameserver equal to the upstream as a guard.

## Configuration (env)

| var                         | default    | meaning                                           |
| --------------------------- | ---------- | ------------------------------------------------- |
| `KLANGK_EGRESS_ALLOW`       | _(empty)_  | comma-separated allow-list: `host[:port]` or CIDR |
| `KLANGK_EGRESS_UPSTREAM`    | `8.8.8.8`  | real upstream the proxy forwards to               |
| `KLANGK_EGRESS_LISTEN_PORT` | `15353`    | UDP port the proxy listens on                     |
| `KLANGK_EGRESS_IPTABLES`    | `iptables` | iptables binary path                              |
| `KLANGK_EGRESS_DEBUG`       | unset      | if set, log each allow/deny decision              |

## Limitations (#2256)

Learned IPs are allow-listed on **all** ports (no per-domain port scoping yet),
wildcard domains aren't supported, and learned IPs never expire (no TTL cleanup).

## References

- #2250 (FQDN egress epic), #2253 (this image), #2254 (klangk lifecycle wiring),
  #2255 (migrate the filter into the sidecar), #2256 (allow-list semantics).
- Spike validation: the sidecar natively sharing the netns intercepts the
  workspace's DNS; the full forward/learn/allow + NXDOMAIN chain is proven.
