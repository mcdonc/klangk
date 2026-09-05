# HTTPS Hosting

This chapter explains how to serve klangkd over HTTPS — and how klangkd
builds the public URLs embedded in hosted-app links, login emails, and
OIDC callbacks. The main path is running klangkd as the
**internet-facing server** with HTTPS managed end to end — no outer
proxy, no operator-supplied certificate: klangkd's built-in Caddy proxy
obtains and renews a CA-issued certificate automatically (ACME, via
Let's Encrypt and ZeroSSL) for a public hostname you choose (#3192).

This is one of four TLS models:

| Model                                                | Who terminates TLS                                               | Chapter                |
| ---------------------------------------------------- | ---------------------------------------------------------------- | ---------------------- |
| **Automatic TLS** (this chapter)                     | klangkd's built-in Caddy, certificate issued + renewed by the CA | here                   |
| **Behind a proxy + internal TLS hop** (this chapter) | klangkd's built-in Caddy, self-generated internal-CA certificate | here                   |
| [Behind a reverse proxy](behind-a-proxy.md), plain   | an outer nginx/Caddy/HAProxy/load balancer                       | Behind a Reverse Proxy |
| Operator-provided certificate                        | planned (#2167, e.g. `tailscale cert`)                           | —                      |

Use automatic TLS when klangkd runs on a host with a **public DNS name**
and ports **80/443 reachable from the internet**. Use an outer proxy when
something else already owns ports 80/443, terminates TLS for several
services, or the host has no public name — and add the internal TLS hop
(below) when the path from that proxy to klangkd must also be encrypted.

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
tls-hostname: "klangk.example.com"
acme-email: "ops@example.com"
```

(`KLANGKD_LISTEN`, `KLANGKD_PORT`, `KLANGKD_TLS_HOSTNAME`,
`KLANGKD_ACME_EMAIL`)

- **`tls-hostname`** — the public FQDN. Setting it **arms** automatic
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

Both `tls-hostname` and `acme-email` are reloadable: after editing,
send `SIGHUP` (see [Process Signals](signals.md)) and klangkd pushes the
re-rendered proxy config to the running Caddy — arming or disarming TLS
does not need a process restart (a `port` change does, as always).

## How it works

klangkd renders the Caddy global block **without** `auto_https off`,
adds `email` (when set) and an explicit certificate storage path under
its state directory (`<state_dir>/caddy-storage`), and addresses the
browser site as `https://<tls-hostname>:<port>`.

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

## Internal TLS: the hop behind an outer proxy

Many deployments terminate TLS at an outer proxy (the real public HTTPS
endpoint) but also require encryption on the hop between that proxy and
klangkd — internal policy, compliance scans, or defense in depth.
`tls-issuer: internal` serves that case with **no certificate to
generate and none to renew**: the built-in Caddy runs its own internal
certificate authority, self-generates the key and certificate for the
armed name, and renews the (short-lived) certificate continuously.

```yaml
listen: "0.0.0.0" # or the interface the proxy reaches
port: 8997
tls-hostname: "klangkd.internal" # any host name or IPv4 literal
tls-issuer: "internal"
trusted-proxy-cidrs: "127.0.0.1,::1,10.0.0.0/24"
```

(`KLANGKD_LISTEN`, `KLANGKD_PORT`, `KLANGKD_TLS_HOSTNAME`,
`KLANGKD_TLS_ISSUER`, `KLANGKD_TRUSTED_PROXY_CIDRS`)

Differences from automatic (ACME) TLS:

- **No public name needed** — single-label names (`klangkd`,
  `localhost`) and IPv4 literals arm fine; the strict public-FQDN
  grammar applies only to the ACME issuer.
- **No ACME account, no reachable ports 80/443** — nothing leaves the
  host. `acme-email` has no effect (klangkd warns if you set it).
- **No HTTP→HTTPS redirect** — the outer proxy owns port 80 and does
  its own redirecting; klangkd disables the automatic one (which also
  keeps the config loadable for unprivileged service users).
- The **internal root CA and issued leaves live under
  `<state_dir>/caddy-storage`** — back that directory up: a lost root
  mints a new CA and breaks the proxy's trust until you redistribute
  the new root.

**Trust the hop, don't just encrypt it.** With no verification on the
outer proxy this is encryption-in-transit only — a root whose private
key lives on the same host cannot defend against that host. Configure
the outer proxy to verify klangkd's certificate against the internal
root CA. Fetch the root certificate from the admin endpoint (owner-only
Unix socket):

The endpoint answers with a JSON object; extract the embedded root
certificate (PEM) from its `root_certificate` field:

```console
sudo -u <klangkd-user> curl --unix-socket <state_dir>/caddy-admin.sock \
  http://localhost/pki/ca/local | jq -r .root_certificate > klangkd-root.crt
```

Then, for an nginx outer proxy:

```nginx
proxy_pass https://klangkd.internal:8997;
proxy_ssl_trusted_certificate /etc/nginx/klangkd-root.crt;
proxy_ssl_verify on;
```

(Mutual TLS — the proxy also presenting a client certificate — is not
wired up yet; watch #2167 for TLS-policy options.)

Everything else behaves as in [Behind a Reverse
Proxy](behind-a-proxy.md): set `trusted-proxy-cidrs` so forwarded
headers are honored, and pin `hosting-hostname` if the proxy mangles
`Host`. See the URL derivation order in the next section.

## Public URLs: `tls-hostname` vs `hosting-hostname`

Two settings sound alike and do different jobs. Keeping them straight
is the whole game:

- **`tls-hostname`** is _listener identity_. It names the DNS name the
  certificate is issued for and the HTTPS listener serves. It changes
  what the proxy binds, it is a bare FQDN (the port comes from `port`),
  and a bad value refuses to boot. It is **not** used to build URLs.
- **`hosting-hostname`** (`KLANGKD_HOSTING_HOSTNAME`) is a _URL
  override_. It never changes any listener; it only pins the authority
  — `host[:port]`, port allowed — that klangkd writes into generated
  URLs (hosted-app links, login emails, OIDC callbacks). Its documented
  job is the behind-a-proxy model, where the `Host` klangkd sees is not
  the public name.

Neither is needed most of the time. When no override is set, klangkd
derives every public URL from the request itself, in this order:

1. `KLANGKD_HOSTING_HOSTNAME` (the explicit pin), else
2. `X-Forwarded-Host` — trusted only when the immediate peer is in
   `KLANGKD_TRUSTED_PROXY_CIDRS`, else
3. the `Host` header, verbatim including its port, else
4. the floor `localhost:<KLANGKD_PORT>` (no request in hand, e.g. a
   CLI handshake).

The scheme and subpath follow the same shape: `KLANGKD_HOSTING_PROTO`
over a trusted `X-Forwarded-Proto`, and `KLANGKD_HOSTING_BASE_PATH`
over a trusted `X-Forwarded-Prefix` (subpath deployments — see
[Behind a Reverse Proxy](behind-a-proxy.md)).

In the automatic-TLS model this needs **zero extra configuration**:
the built-in Caddy terminates TLS and forwards the real `Host` with
`X-Forwarded-Proto: https`, and its loopback peer is trusted by
default — so URLs derive as `https://<your-fqdn>[:<port>]` on their
own. `tls-hostname` arms the listener; the headers carry the name into
URLs.

Which to set, by deployment:

| Deployment                                           | Hostname settings to set                      |
| ---------------------------------------------------- | --------------------------------------------- |
| Internet-facing, automatic TLS (this chapter)        | `tls-hostname` only — URLs derive from `Host` |
| Behind an outer proxy that forwards truthful headers | nothing (still set `trusted-proxy-cidrs`)     |
| Behind an outer proxy that mangles `Host`/forwarded  | `hosting-hostname` as the URL pin             |
| Behind an outer proxy, encrypted hop wanted          | `tls-hostname` + `tls-issuer: internal`       |
| Plain HTTP, direct browser access                    | nothing                                       |
| URLs come out wrong despite correct headers          | `hosting-hostname` as an explicit override    |

## Checking the setup

Before the first boot, run the pre-flight checker with the arming var
exported:

```console
KLANGKD_TLS_HOSTNAME=klangk.example.com klangkd doctor
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
  unset `KLANGKD_TLS_HOSTNAME` and use the
  [outer-proxy model](behind-a-proxy.md) instead.
- **Boot fails with "KLANGKD_TLS_HOSTNAME requires KLANGKD_PORT"** —
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
