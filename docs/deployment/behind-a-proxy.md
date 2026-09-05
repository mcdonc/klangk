# Behind a Reverse Proxy

This chapter explains how to put klangk behind an outer reverse proxy.
An outer proxy is a separate server that terminates TLS or routes traffic
to klangk. Common examples: nginx, Caddy, Traefik, HAProxy, a cloud
load balancer.

> If klangkd itself is the internet-facing server and you want HTTPS
> without running a second proxy, see
> [HTTPS Hosting](https-hosting.md) instead — klangkd's built-in Caddy
> obtains and renews a CA-issued certificate for a public hostname. To
> also encrypt the proxy→klangkd hop with an auto-generated certificate,
> see [Internal TLS](https-hosting.md#internal-tls-the-hop-behind-an-outer-proxy).

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

If you do not set this correctly, four things break:

1. **Hosted app URLs** use `localhost` instead of the public hostname.
2. **OIDC callbacks** use `http` instead of `https`.
3. **Login links** (password reset, verification) point to the wrong host.
4. **Concurrent-logon audit records** never fire — every session records
   the proxy's address as its workstation, so different workstations are
   never detected. See
   [Concurrent-logon auditing depends on the proxy chain](#concurrent-logon-auditing-depends-on-the-proxy-chain)
   for how to verify the setup.

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

For the full derivation order — pin, trusted forwarded headers,
`Host`, floor — and how this differs from `KLANGKD_TLS_HOSTNAME`
(automatic TLS on an internet-facing klangkd), see
[HTTPS Hosting](https-hosting.md#public-urls-tls-hostname-vs-hosting-hostname).

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

## Concurrent-logon auditing depends on the proxy chain

klangk records the workstation of every login session: the effective
client IP plus the user agent. When one account holds concurrent
sessions from different workstations, klangkd writes an audit record to
the server log (see
[Authentication](../features/authentication.md#concurrent-logon-auditing)).

This works only when the proxy chain preserves the real client IP.
The backend resolves the effective client IP from `X-Real-IP` (or the
first hop of `X-Forwarded-For`) **and only when the immediate peer is
trusted**. When the chain is misconfigured, every session records the
proxy's address instead of the client's.

### Consequences of a misconfigured proxy chain

**Too little trust** (headers missing, `trusted-proxy-cidrs` wrong,
or `reject-proxy-headers: true`):

- **No audit records are ever written.** All logins appear to come
  from one workstation (the proxy's address), so concurrent logons
  from different workstations are never detected. This is exactly the
  event the feature exists to catch — an attacker using stolen
  credentials from a second machine leaves no trace.
- **The admin session list is misleading.**
  `GET /api/v1/users/{id}/sessions` shows the proxy's address
  for every session. An operator cannot tell workstations apart, and
  account sharing looks the same as normal use.
- **The failure is silent.** Nothing errors, no warning is logged, and
  the deployment looks healthy. The only symptom is an audit trail
  that stays empty — which is easy to miss until you need it, and by
  then the missed events cannot be reconstructed (the workstation was
  never recorded).

Note that the **session limit is not affected**: it counts sessions,
not IP addresses, so `KLANGKD_MAX_SESSIONS_PER_USER` still works.
Only the audit signal is lost.

**Too much trust** (a trust list wider than your actual proxies):

- **The audit trail can be forged.** Any client whose forwarded
  headers are honored can claim any `X-Real-IP` — a stolen-credential
  login can be made to appear as the victim's own workstation, or a
  fake "different workstation" record can be planted to distract an
  investigation.

### Checklist for a correct setup

1. The outer proxy must **overwrite** a client-IP header with the
   address it actually saw: `X-Real-IP $remote_addr;` (nginx) or
   Caddy's automatic `X-Forwarded-For` semantics. Do **not** rely on
   an outer proxy that only _appends_ to `X-Forwarded-For` — the
   leftmost entry is then client-controlled, and a client can forge
   its workstation identity (mask a stolen-credential login as the
   victim's machine, or plant fake "different workstation" records).
   klangk validates that the forwarded value parses as an IP and
   ignores garbage, but a syntactically valid forged IP from an
   append-style chain is still honored. See the nginx and Caddy
   examples above for the safe forms.
2. `trusted-proxy-cidrs` must contain the outer proxy's IP or subnet.
   With a wrong list, klangk's Caddy and the backend ignore the
   forwarded headers.
3. `reject-proxy-headers` must be off (the default). It disables all
   forwarded-header trust, which also disables the workstation audit.
4. The trust list must contain **only** your actual proxies (see the
   too-much-trust consequence above).

### Verify it works

1. Log in as a test user from workstation A.
2. Log in as the same user from a different network (workstation B —
   for example, a phone hotspot).
3. As admin, call `GET /api/v1/users/{id}/sessions`. The two
   sessions must show different `source_ip` values, and each must be
   the real client address — not the proxy's.
4. The klangkd log must contain
   `audit: concurrent logon from different workstations`.

If every session's `source_ip` is the proxy's address or `127.0.0.1`,
forwarded headers are dropped or ignored. Fix the outer proxy headers
or `trusted-proxy-cidrs`, then repeat the check.

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
- [Authentication](../features/authentication.md#concurrent-logon-auditing)
  — what the workstation audit records and how to read them.
