# Authentication

## Auth Methods

- **Two auth methods**: email/password (local) and OIDC (external Identity Providers). Configurable via `KLANGK_AUTH_MODES`: `password`, `oidc`, or `both` (default: `both` if OIDC configured, `password` otherwise).
- **OIDC authentication**: Supports multiple OIDC providers (e.g., two Keycloak realms for CAC + internal SSO). Configured via a JSON file (`KLANGK_OIDC_CONFIG`). Each provider has its own login/callback endpoints (`GET /auth/oidc/{provider_id}/login`, `GET /auth/oidc/{provider_id}/callback`). Uses Authorization Code flow with PKCE. ID token signature validated against the IdP's JWKS. Login page shows one button per configured provider. JIT user provisioning on first OIDC login — users are created as verified with no password. Existing email/password users are linked to their OIDC identity on first SSO login. Per-provider group mapping syncs IdP group claims to Klangk admin group membership on every login. CLI login (`klangkc login`) opens a browser for the OIDC flow and receives the token via a temporary localhost callback server.
- **Email/password authentication**: bcrypt hashing, email validated at registration

See [OIDC Configuration](../reference/oidc.md) for detailed setup instructions.

## Email Verification

- Registration sends a verification email with a signed token link; user must click to activate account and is auto-logged-in on verification
- Resend via "Resend verification email" link on login page (shown on 403 "not verified" error, rate-limited to 1/min per email)
- Email sent via SMTP (`KLANGK_SMTP_HOST/PORT/USER/PASSWORD/FROM`) or local sendmail (default, configurable via `KLANGK_SENDMAIL_PATH`)

## Password Reset

- User requests reset via `POST /auth/forgot-password` with their email address
- Response is always `{"status": "sent"}` regardless of whether the email exists (prevents email enumeration)
- Rate limited: one reset email per address per 60 seconds (in-memory cooldown, resets on server restart)
- Token: JWT (HS256) containing `{sub: user_id, purpose: "reset", exp: timestamp}`, expires in 1 hour
- Reset URL sent via email: `{proto}://{hostname}/#/reset-password?token={token}`
- User submits new password via `POST /auth/reset-password` with the token
- Tokens are stateless (no database tracking) — the same token can be reused until it expires

## JWT Sessions

- JWT tokens (24hr expiry, secret configurable via `KLANGK_JWT_SECRET`) with token blocklist for logout; no refresh/renewal mechanism — users must re-authenticate when tokens expire
- Workspace containers receive a separate JWT (`KLANGK_WORKSPACE_TOKEN`) at startup for bridge API calls; lifetime controlled by `KLANGK_WORKSPACE_TOKEN_HOURS` (default 24h). No renewal — containers running longer than the token lifetime lose bridge access until restarted. See [Workspace JWT Auth](../architecture/workspace-jwt.md) for details.
- Session persists across page reloads (async token loading before routing)
- Deep link preservation: unauthenticated visits to protected URLs redirect to login, then return to the original URL after successful login

## Brute-Force Protection

- Failed login attempts tracked per email in SQLite
- `KLANGK_LOGIN_LOCKOUT_FAILURES=N` failures within `KLANGK_LOGIN_LOCKOUT_WINDOW=S` (default 300s) triggers a `KLANGK_LOGIN_LOCKOUT_DURATION=D` (default 900s) lockout (429 with remaining seconds)
- Disabled by default (N=0)

## Registration

- Open registration with email verification (test mode auto-verifies for E2E tests)
- Login rejects unverified accounts
