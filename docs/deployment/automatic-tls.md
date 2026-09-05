# Automatic TLS

This chapter explains how to run klangkd as the **internet-facing server**
with HTTPS managed end to end — no outer proxy, no operator-supplied
certificate. klangkd's built-in Caddy proxy obtains and renews a
CA-issued certificate automatically (ACME, via Let's Encrypt and ZeroSSL)
for a public hostname you choose (#3192).

This is one of three TLS models:

| Model                                       | Who terminates TLS                                               | Chapter                |
| ------------------------------------------- | ---------------------------------------------------------------- | ---------------------- |
| **Automatic TLS** (this chapter)            | klangkd's built-in Caddy, certificate issued + renewed by the CA | here                   |
| [Behind a reverse proxy](behind-a-proxy.md) | an outer nginx/Caddy/HAProxy/load balancer                       | Behind a Reverse Proxy |
| Operator-provided certificate               | planned (#2167, e.g. `tailscale cert`)                           | —                      |

Use automatic TLS when klangkd runs on a host with a **public DNS name**
and ports **80/443 reachable from the internet**. Use the outer-proxy
model when something else already owns ports 80/443, terminates TLS for
several services, or the host has no public name.

## Requirements

- A DNS **A/AAAA record** pointing a public hostname (e.g.
  `klangk.example.com`) at the host.
- Ports **80** and **443** reachable from the internet. Port 80 serves
  the ACME HTTP-01 challenge and the HTTP→HTTPS redirect; port 443
  serves HTTPS (and the TLS-ALPN challenge).
- A Caddy binary new enough for klangkd's full global options block
  (anything from the last few years; klangkd probes the binary at boot
  and fails with a clear message if it is too old, rather than silently
  serving plain HTTP).
- Permission for the caddy binary to bind ports below 1024 when klangkd
  does not run as root:

  ```console
  sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which caddy))
  ```

  (Re-run after every caddy upgrade — a replaced binary loses the
  capability.)

## Configuration

Add these to `klangkd.yaml` (each key also has an env-var equivalent;
env vars override file values):

```yaml
listen: "0.0.0.0"
port: 443
public-hostname: "klangk.example.com"
acme-email: "ops@example.com"
```

(`KLANGKD_LISTEN`, `KLANGKD_PORT`, `KLANGKD_PUBLIC_HOSTNAME`,
`KLANGKD_ACME_EMAIL`)

- **`public-hostname`** — the public FQDN. Setting it **arms** automatic
  TLS: the browser listener is rendered as `https://<fqdn>:<port>`,
  Caddy's automatic HTTPS takes over (certificate issuance, renewal,
  HTTP→HTTPS redirect on port 80). Unset (the default) keeps plain HTTP
  with `auto_https off` — byte-identical to the pre-#3192 behavior, so
  outer-proxy deployments are untouched.
- **`acme-email`** — strongly recommended. The CA sends certificate
  expiry and renewal-failure notices there, and it registers the ACME
  account. Must be a plain address (`ops@example.com`) — a display-name
  form (`Ops <ops@example.com>`) is rejected at construction, because
  the value is passed verbatim into the proxy config.
- **`port`** — required (arming without it refuses to boot). `443` is
  the canonical choice for an internet-facing server; any other port
  works too (the certificate is issued for the hostname, not the port),
  but then browsers must use the explicit port in the URL.
- **`listen`** — must not stay at the `127.0.0.1` default: the HTTPS
  listener would be unreachable off-host **and** the ACME challenge
  could not be answered, so issuance would fail (klangkd logs a warning
  at render time; `0.0.0.0` binds every interface).

Both `public-hostname` and `acme-email` are reloadable: after editing,
send `SIGHUP` (see [Process Signals](signals.md)) and klangkd pushes the
re-rendered proxy config to the running Caddy — arming or disarming TLS
does not need a process restart (a `port` change does, as always).

## How it works

klangkd renders the Caddy global block **without** `auto_https off`,
adds `email` (when set) and an explicit certificate storage path under
its state directory (`<state_dir>/caddy-storage`), and addresses the
browser site as `https://<public-hostname>:<port>`.

On boot, Caddy:

1. Opens the ACME account with the email (Let's Encrypt first, ZeroSSL
   as fallback issuer).
2. Obtains a certificate for the hostname via the HTTP-01 challenge
   (port 80) and/or TLS-ALPN (port 443).
3. Serves HTTPS on `listen:port` and redirects HTTP→HTTPS on port 80.
4. Renews the certificate automatically before it expires (~30-day
   renewal window; no operator action).

Issued certificates, private keys, and ACME account state persist in
`<state_dir>/caddy-storage`, so restarts reuse the existing certificate
instead of re-issuing (which would walk into CA rate limits). The
storage directory is klangkd-owned runtime state: include it in backups
the same way as the database directory if you want instant-restore of
the TLS identity.

Everything inside the site block — workspace-token gates, container
ACLs, the hosted-apps proxy, WebSocket upgrades, the frontend hardening
headers — is identical over HTTPS and plain HTTP; only the listener
scheme changes. The container-egress listener
(`KLANGKD_EGRESS_LISTEN:KLANGKD_EGRESS_PORT`) stays plain HTTP in all
modes: it is internal container wiring, never exposed to the internet.

Browser secure-context APIs (the terminal clipboard, for example) work
once the site is served over HTTPS.

## Checking the setup

Before the first boot, run the pre-flight checker with the arming var
exported:

```console
KLANGKD_PUBLIC_HOSTNAME=klangk.example.com klangkd doctor
```

When automatic TLS is armed, doctor checks that ports 80/443 are
bindable. This is an **error-grade** check, not a warning: with TLS
armed those ports are part of the proxy config, so an unbindable port
does not merely break certificate issuance — caddy refuses to load the
config and the klangkd proxy (browser **and** container-egress
listeners) does not start at all. The fix hint grants the caddy binary
permission to bind privileged ports:

```console
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f $(which caddy))
```

After boot, certificate trouble surfaces in the logs at `ERROR` level
(lines from Caddy's `tls.obtain` logger, e.g.
`could not get certificate from issuer`). Common causes:

- The DNS record does not point at this host yet (or has not
  propagated).
- Port 80 and/or 443 is firewalled or already bound by another server.
- The caddy binary lacks `cap_net_bind_service` (see above).
- Too many certificates issued for this hostname recently (CA rate
  limit) — usually a symptom of a non-persistent storage dir, which the
  explicit `<state_dir>/caddy-storage` prevents.

## Troubleshooting

- **Boot fails with "automatic TLS ... requires a newer caddy"** — the
  detected caddy binary cannot load the global options automatic TLS
  needs. Upgrade caddy (e.g. the official caddy repository package), or
  unset `KLANGKD_PUBLIC_HOSTNAME` and use the
  [outer-proxy model](behind-a-proxy.md) instead.
- **Boot fails with "KLANGKD_PUBLIC_HOSTNAME requires KLANGKD_PORT"** —
  arming needs the browser listener; set `port` (443 is conventional).
- **HTTPS listener unreachable from outside** — `listen` is still
  `127.0.0.1`; set it to `0.0.0.0` or a specific interface IP.
- **Certificate not issued** — run `klangkd doctor` (see above) and
  check the logs for `tls.obtain` errors. Note that an unbindable
  port 80/443 does not degrade to plain HTTP: with TLS armed, caddy
  refuses the whole config (see the doctor section above).

## Notes

- **No HSTS header.** The browser site's response headers are identical
  in armed and unarmed mode; klangkd does not add
  `Strict-Transport-Security`. Put an HSTS policy in an outer proxy if
  you need one, or watch #2167 for TLS-header options.
- The HTTP→HTTPS redirect on port 80 is installed by caddy's automatic
  HTTPS and redirects to the armed `https://<hostname>:<port>`.
