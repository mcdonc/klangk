# Behind a Reverse Proxy

This chapter explains how to put klangk behind an outer reverse proxy.
An outer proxy is a separate server that terminates TLS or routes traffic
to klangk. Common examples: nginx, Caddy, Traefik, HAProxy, a cloud
load balancer.

## The two-tier model

klangk runs its own Caddy proxy internally. When you add an outer proxy,
requests pass through two proxies:

```text
Client (browser)
  -> Outer proxy (TLS termination, port 443)
     -> klangk Caddy (KLANGKD_LISTEN:KLANGKD_PORT)
        -> klangk backend (UDS)
```

The outer proxy forwards requests to klangk's browser listener. klangk's
Caddy then forwards them to the backend over a Unix domain socket.

## Required settings

Add these to `klangkd.yaml` on the klangk host. Each setting also has
an environment variable equivalent (shown in parentheses). Env vars
override config-file values.

### Bind address

klangk's browser listener defaults to `127.0.0.1`. If the outer proxy
runs on a different host, change this to all interfaces:

```yaml
listen: "0.0.0.0"
```

(`KLANGKD_LISTEN`)

This is safe. The proxy's workspace-token gate and container-source IP
ACL protect all endpoints.

To restrict the bind to a specific interface, use that interface's IP
address instead.

### Trusted proxy CIDRs

klangk ignores forwarded headers (`X-Real-IP`, `X-Forwarded-For`,
`X-Forwarded-Host`, `X-Forwarded-Proto`) from untrusted peers. The
default trust list is `127.0.0.1,::1` (loopback only).

Add the outer proxy's IP or subnet:

```yaml
trusted-proxy-cidrs: "127.0.0.1,::1,10.0.0.0/24"
```

(`KLANGKD_TRUSTED_PROXY_CIDRS`)

Use a comma-separated list. Each entry is an IP address or a CIDR range.
Only the immediate TCP peer is checked against this list.

If you do not set this correctly, three things break:

1. **Hosted app URLs** use `localhost` instead of the public hostname.
2. **OIDC callbacks** use `http` instead of `https`.
3. **Login links** (password reset, verification) point to the wrong host.

### Browser port

Set the port that klangk's Caddy listens on:

```yaml
port: 8997
```

(`KLANGKD_PORT`)

The outer proxy forwards traffic to this port.

### Public URL overrides (optional)

klangk derives the public hostname, protocol, and base path from the
forwarded headers that the outer proxy sends. To pin these values
explicitly:

```yaml
hosting-hostname: "klangk.example.com"
hosting-proto: "https"
hosting-base-path: "/klangk"
```

(`KLANGKD_HOSTING_HOSTNAME`, `KLANGKD_HOSTING_PROTO`,
`KLANGKD_HOSTING_BASE_PATH`)

These override the forwarded-header values. Set them when the outer
proxy does not send `X-Forwarded-Host` or `X-Forwarded-Proto`.

### Complete klangkd.yaml example

A minimal config for a klangk instance behind an outer TLS proxy:

```yaml
listen: "0.0.0.0"
port: 8997
auth-modes: password
trusted-proxy-cidrs: "127.0.0.1,::1,10.0.0.0/24"
```

## Outer proxy configuration

The outer proxy must forward the correct headers and handle WebSocket
upgrades.

### Forwarded headers

Set these headers on the outer proxy (nginx example):

```nginx
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Host  $host;
proxy_set_header X-Forwarded-Proto $scheme;
```

If you serve klangk on a subpath (e.g. `/klangk/`), also set:

```nginx
proxy_set_header X-Forwarded-Prefix /klangk;
```

### WebSocket upgrade

klangk uses WebSockets at `/ws` for live terminal and container status
updates. The outer proxy must pass the upgrade headers:

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade    $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_read_timeout 86400s;
```

Set a long read timeout. WebSocket connections stay open for the full
terminal session.

### Full nginx example

This example terminates TLS and forwards to klangk on port 8997:

```nginx
server {
    listen 443 ssl;
    server_name klangk.example.com;

    ssl_certificate     /etc/ssl/klangk.crt;
    ssl_certificate_key /etc/ssl/klangk.key;

    location / {
        proxy_pass http://klangk-host:8997;

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host  $host;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;
    }
}
```

Replace `klangk-host` with the klangk host's IP or hostname.

### Full Caddy example

```caddyfile
klangk.example.com {
    reverse_proxy klangk-host:8997
}
```

Caddy sets all forwarded headers automatically. It also handles TLS
automatically via Let's Encrypt.

## The no-auth bind-safety gate

In `none` auth mode, klangk restricts the `/auth/local` endpoint to
loopback clients. This prevents unauthenticated token minting from the
network.

When an outer proxy forwards requests, klangk reads the real client IP
from `X-Real-IP` (or `X-Forwarded-For`). It does this only when the
immediate peer is in `KLANGKD_TRUSTED_PROXY_CIDRS`.

If you run `none` auth mode behind a non-loopback proxy, klangk refuses
to start unless you set `KLANGKD_ALLOW_INSECURE_NO_AUTH=1`. Do not use
`none` mode in production with network-reachable deployments. Use
`password`, `oidc`, or `both` instead.

## Reject all forwarded headers

To ignore all forwarded headers unconditionally:

```yaml
reject-proxy-headers: true
```

(`KLANGKD_REJECT_PROXY_HEADERS`)

This disables the trusted-proxy logic entirely. klangk treats every
request as if it came directly from the immediate TCP peer. Use this
when klangk faces the internet directly with no outer proxy.

## Egress listener

The container-egress listener (`KLANGKD_EGRESS_PORT`, default `8995`)
serves workspace container traffic. It is separate from the browser
listener. Containers connect directly via podman's pasta NAT — no outer
proxy is involved.

The egress listener defaults to `0.0.0.0`. The container-source IP ACL
and workspace-token gate protect it. You do not need to change it when
you add an outer proxy.

## See also

- [Hosting & Proxy](hosting.md) — ports, topology, Tailscale notes.
- [Environment Variables](../reference/environment.md) — full settings
  reference.
- [Auth Modes](../features/auth-modes.md) — `none`, `password`, `oidc`,
  `both`.
