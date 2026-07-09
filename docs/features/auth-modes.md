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
| `both`     | SSO buttons **and** email/password | **team** (default when OIDC is configured)    |

## Choosing a mode

- **`none`** — you run klangk on your own machine for development or testing
  and don't want to type a password. The server auto-logs you in as the seeded
  default user (see [no-auth mode](#no-auth-mode-none) below). Must bind
  loopback.
- **`password`** — a small trusted group logs in with email/password.
- **`oidc`** — your organisation manages identity through an OIDC provider
  (Keycloak, Okta, Azure AD, …) and you want to disable local passwords.
  See [OIDC](../reference/oidc.md).
- **`both`** — the default when an OIDC provider is configured: SSO for most
  users, plus email/password as a fallback.

## The default

`KLANGK_AUTH_MODES` defaults to `both` when an OIDC provider is configured
(via `KLANGK_OIDC_CONFIG`), and `password` otherwise. Set it explicitly to
pin a mode regardless of OIDC config.

## No-auth mode (`none`)

`none` is the foundation for a no-friction single-user dev/test loop —
including the soliplex (pi plugin + browser-delegate) flow against your own
browser — without standing up the multi-user tier or logging in each session.

In `none` mode the server freely issues a JWT for the seeded default user
(`KLANGK_DEFAULT_USER`, defaulting to `admin@example.com`) with no password:

- The **frontend** calls `POST /api/v1/auth/local` on load and stores the
  token, skipping the login form entirely.
- The **CLI** (`klangkc`) auto-logs in the first time any command runs — no
  `klangkc login` required (the server must be registered once with
  `klangkc login <server>`; thereafter every command works).
- **Workspace terminals** (WebSocket) flow the token through the existing
  `?token=` path unchanged.

The freely-issued token is indistinguishable from a password-login token to
the refresh and blocklist machinery — it reuses the standard `create_token`
claims (`sub`, `email`, `jti`, `exp`) and the seeded default user is a real
database row.

### Why this is safe

Two complementary controls keep `none` mode local:

1. **Loopback bind gate.** The server **refuses to start** in `none` mode
   unless `KLANGK_LISTEN` is a loopback address (`127.0.0.1`, `::1`,
   `localhost`). The loopback bind is the identity boundary: only the
   operator's own browser can reach `/auth/local`. To expose a no-auth server
   on another interface (e.g. an isolated throwaway VM), set
   `KLANGK_ALLOW_INSECURE_NO_AUTH=1` explicitly — you will get a warning, and
   anyone who can reach that address is effectively logged in as admin.

2. **nginx per-location ACL.** `POST /api/v1/auth/local` is wrapped in a
   `location` block that does `allow 127.0.0.1; allow ::1; deny all;`.
   Workspace containers reach the host via pasta NAT and appear as the host's
   _non-loopback_ IP, so a container hitting `/auth/local` is denied with 403
   at nginx — while the host browser (127.0.0.1) succeeds. nginx itself stays
   bound to `0.0.0.0` (soliplex, hosted apps, and remote browsers rely on it).

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
