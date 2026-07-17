# Auth Modes

Klangk's `KLANGK_AUTH_MODES` setting is the single knob that selects how users
authenticate. The same application binary supports four modes, and each one maps
to a real-world deployment profile — there is no architecture change per
customer, only configuration.

| Mode       | Login method(s)                    | Deployment profile                            |
| ---------- | ---------------------------------- | --------------------------------------------- |
| `none`     | none — auto-login                  | **local-dev** (single user, your own browser) |
| `oidc`     | SSO buttons only                   | **customer-locked**                           |
| `password` | email/password only                | small team                                    |
| `both`     | SSO buttons **and** email/password | **team**                                      |

The **default is `none`**. A fresh klangk with nothing set boots in
no-login single-user mode, bound to loopback — it "just works" locally with
no password and is unreachable from the network. See
[The default](#the-default) below for the upgrade implications.

## Choosing a mode

- **`none`** — **the default**. You run klangk on your own machine for
  development or testing and don't want to type a password. The server
  auto-logs you in as the seeded default user (see
  [no-auth mode](#no-auth-mode-none) below). Must bind loopback.
- **`password`** — a small trusted group logs in with email/password.
- **`oidc`** — your organisation manages identity through an OIDC provider
  (Keycloak, Okta, Azure AD, …) and you want to disable local passwords.
  See [OIDC](../reference/oidc.md).
- **`both`** — SSO for most users, plus email/password as a fallback.

## The default

`KLANGK_AUTH_MODES` defaults to **`none`** — a fresh klangk with nothing
configured boots in no-login single-user mode, bound to loopback
(`127.0.0.1`). It "just works" locally: open the browser, you're in, no
password. OIDC settings (`KLANGK_OIDC_*`) do **not** change this default —
configuring a provider only takes effect once the mode is `oidc` or `both`
(set explicitly). Set `KLANGK_AUTH_MODES` explicitly to enable password,
OIDC, or combined login.

> **Upgrading from an earlier version:** if you previously relied on OIDC
> being configured _implying_ `both` (the old "OIDC turns auth on" rule),
> your server will now boot in `none` mode instead — no-login single-user,
> loopback-bound. That is safe by construction — `none` refuses to start on
> a non-loopback bind (see [why this is safe](#why-this-is-safe)) — but you
> should set `KLANGK_AUTH_MODES=oidc` (or `both`) explicitly before
> redeploying to preserve your intended auth posture. See
> [Switching modes](#switching-modes).

## No-auth mode (`none`)

`none` is the foundation for a no-friction single-user dev/test loop —
without standing up the multi-user tier or logging in each session.

In `none` mode the server freely issues a JWT for the seeded default user
(`KLANGK_DEFAULT_USER`, defaulting to `admin@example.com`) with no credentials
required from the caller:

- The **frontend** calls `POST /api/v1/auth/local` on load and stores the
  token, skipping the login form entirely.
- The **CLI** (`klangk`) probes the server's auth mode on each
  command (via `GET /config`) and auto-calls `/auth/local` when it's `none`,
  so no `klangk login` is ever needed — the first command after registering
  the server with `klangk login <server>` just works (and re-registration
  isn't: a saved token that 401s triggers the same auto-login fallback).
- **Workspace terminals** (WebSocket) flow the token through the existing
  `?token=` path unchanged.

The freely-issued token is indistinguishable from a password-login token to
the refresh and blocklist machinery — it reuses the standard `create_token`
claims (`sub`, `email`, `jti`, `exp`) and the seeded default user is a real
database row.

### Why this is safe

Two complementary controls keep `none` mode local:

1. **Loopback bind gate.** The server **refuses to start** in `none` mode
   unless `KLANGK_LISTEN` is a loopback address (any of the IPv4 loopback
   range `127.0.0.0/8`, IPv6 `::1`, or the `localhost` hostname). The loopback
   bind is the identity boundary: only the operator's own browser can reach
   `/auth/local`. To expose a no-auth server on another interface (e.g. an
   isolated throwaway VM), set `KLANGK_ALLOW_INSECURE_NO_AUTH=1` explicitly —
   you will get a warning, and anyone who can reach that address is
   effectively logged in as admin.

2. **nginx per-location ACL.** `POST /api/v1/auth/local` is wrapped in a
   `location` block that does `allow 127.0.0.1; allow ::1; deny all;`.
   Workspace containers reach the host via pasta NAT and appear as the host's
   _non-loopback_ IP, so a container hitting `/auth/local` is denied with 403
   at nginx — while the host browser (127.0.0.1) succeeds. nginx itself stays
   bound to `0.0.0.0` (hosted apps and remote browsers rely on it).

3. **Backend source-IP self-check.** As a third layer (and to close the
   front-proxy bypass), the `local_login` handler independently verifies the
   _effective_ client is loopback: it trusts `X-Real-IP` / `X-Forwarded-For`
   only when the immediate peer is itself a trusted (loopback) proxy, so a
   non-loopback caller can't spoof them. This matters when a loopback proxy
   (caddy, traefik, a sidecar) sits in front of nginx — then every proxied
   request has `$remote_addr=127.0.0.1` and the nginx ACL alone would admit
   everyone; the backend re-check catches the real client via the forwarded
   header and refuses non-loopback values.

> **Do not place a non-loopback proxy in front of nginx in `none` mode.**
> A loopback front-proxy makes `$remote_addr` loopback for all requests, so
> the nginx ACL admits everyone; control #3 above still refuses non-loopback
> real clients via `X-Real-IP`, so you stay safe, but the cleaner topology is
> to let klangk's own nginx be the edge or to use a real auth mode
> (`password`/`oidc`/`both`) when exposing the server beyond loopback.

### Why the token is kept

Even though the token is free, every authenticated request still carries it as
a `Bearer` header. CORS already stops a cross-origin `evil.com` from _reading_
the `/auth/local` response (origins default to the hosting origin/localhost,
never `*`), so the token can't be stolen that way. The custom `Authorization`
header is then belt-and-suspenders **CSRF defense**: any endpoint that mutates
on a _simple_ request (no custom header → no CORS preflight) is closed off,
because a forged cross-origin request can't carry the header. JSON
content-types already force a preflight; the token covers the un-audited
simple endpoints — cheap to keep, risky to drop.

## Other modes

For `password`, `oidc`, and `both`, see [Authentication](authentication.md)
and [OIDC configuration](../reference/oidc.md). These modes behave exactly as
before; `none` is purely additive.

## Switching modes

Mode switching is **just changing `KLANGK_AUTH_MODES` and restarting** — no
migration, no data loss, no re-seed. Two directions matter; both work because
the CLI probes the server's mode **live** on every command (it is not cached),
so the new mode takes effect the moment the server restarts.

### `none` -> `password` / `oidc` / `both` (adding real login)

This is the common upgrade path: you've been running solo in `none` mode and
now want real logins (for yourself and/or teammates). One thing to sort out
first: **the password for the default user.** The seeded admin user always
has a password — either `KLANGK_DEFAULT_PASSWORD` if you set it, or a random
one the server printed to stderr at first boot. If you didn't capture that
random password, set a known one now while you're still holding the free
admin token:

```bash
# 1. Still in none mode — you're auto-logged-in as the admin default user.
#    Give that user a real password via the admin endpoint:
klangk admin users set-password admin@example.com

# 2. (Optional) invite teammates while you're still admin-with-token:
klangk admin invitations send teammate@example.com

# 3. Flip the mode and restart the substrate:
#    (set KLANGK_AUTH_MODES=password in your substrate env, then restart)

# 4. Log in for real — you and your invitees now use the login form / CLI:
klangk login
```

`klangk admin users set-password` resolves the email to a user id and
`PATCH`es the password (admin-gated). Run it **while still in `none` mode**,
when you're holding the free admin token — after the flip, the old free token
still authorizes until it expires, but it's simplest to do the password set
first. Confirm you're the admin with `klangk status` (it reports
`admin: yes`).

### `password` / `oidc` / `both` -> `none` (dropping back to solo)

Going the other way needs no preparation — flip `KLANGK_AUTH_MODES=none` and
restart (remember the [loopback bind](#why-this-is-safe) gate). Existing
issued tokens remain valid until they expire; once they do, the CLI auto-logs
in as the default user, and the browser skips the login form. No one is
locked out, because `none` requires no credential at all.

### What carries over across a switch

- **User accounts and their data** are unaffected — modes only change _how_
  you authenticate, not what's stored. The same users, workspaces, volumes,
  groups, and ACLs survive every switch.
- **Tokens already in flight** keep working until they expire (or are
  blocklisted on logout). A mode switch is not a global logout.
- **The seeded default user** (`KLANGK_DEFAULT_USER`) is always present; in
  `none` it's who you become, in the other modes it's a normal account.

### One OIDC caveat

If you switch to `oidc`/`both`, users (including the default user) link to an
OIDC identity on their **first SSO login**, keyed on the identity's `sub` and
email. If the default user's seeded email doesn't match a real SSO account,
that first SSO login creates a _new_ user row and the default user's solo-mode
data stays under the old email. To avoid orphaning data, set
`KLANGK_DEFAULT_USER` to your SSO email **before** the first seed, or assign
the default user's workspaces to the SSO identity via the admin API after
linking.
