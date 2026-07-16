# OIDC Configuration

Klangk supports OIDC authentication via one or more external Identity Providers (e.g., Keycloak). This is used for CAC card login and other SSO scenarios. Klangk is a standard OIDC relying party — the IdP handles all certificate/credential complexity.

## Setup

OIDC providers can be configured in two ways: **inline** in the `klangkd` config file (recommended), or in a **separate file** via `KLANGK_OIDC_CONFIG`.

### Option 1: Inline in the config file (recommended)

Add an `oidc_providers` section to your `klangkd` config file:

```yaml
# /etc/klangkd.conf
auth_modes: both

oidc_providers:
  - id: cac
    display-name: CAC Login
    issuer: https://keycloak.example.com/realms/company
    client-id: klangk
    client-secret: "file:/run/secrets/cac-secret"
    scopes: openid email profile
    ca-cert: /etc/pki/tls/certs/company-ca-bundle.pem
    logout-redirect: true

  - id: internal
    display-name: Internal SSO
    issuer: https://keycloak.example.com/realms/corp
    client-id: klangk
    client-secret: "file:/run/secrets/corp-secret"
```

See [Configuration File](klangkd-config.md) for full config-file documentation.

### Option 2: Separate file via `KLANGK_OIDC_CONFIG`

Create a standalone YAML file with the provider list:

```yaml
- id: cac
  display-name: CAC Login
  issuer: https://keycloak.example.com/realms/company
  client-id: klangk
  client-secret: "file:/run/secrets/cac-secret"
  scopes: openid email profile
  ca-cert: /etc/pki/tls/certs/company-ca-bundle.pem
  logout-redirect: true

- id: internal
  display-name: Internal SSO
  issuer: https://keycloak.example.com/realms/corp
  client-id: klangk
  client-secret: "file:/run/secrets/corp-secret"
```

Set `KLANGK_OIDC_CONFIG` in `.env`:

```bash
KLANGK_OIDC_CONFIG=/path/to/oidc.yaml
```

> When both `KLANGK_OIDC_CONFIG` (separate file) and `oidc_providers` (inline) are set, the separate file wins — consistent with the global precedence rule (env vars override config-file values).

1. Optionally set `KLANGK_AUTH_MODES` to control which login methods are available:
   - `both` — SSO buttons + email/password form
   - `oidc` — SSO buttons only, email/password disabled
   - `password` — email/password only
   - `none` — no-login single-user (local-dev) mode; OIDC config is ignored. See [Auth Modes](../features/auth-modes.md).

   The default (when `KLANGK_AUTH_MODES` is unset) is `none`; configuring OIDC no longer implies `both` — set `KLANGK_AUTH_MODES=oidc` (or `both`) to turn OIDC login on (#1419).

## Provider Config Fields

| Field                  | Required | Description                                                                                                                                                                           |
| ---------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id`                   | Yes      | URL-safe slug, used in endpoint paths (`/api/v1/auth/oidc/{id}/login`) and stored as `provider` on users                                                                              |
| `display-name`         | Yes      | Button label on the login page (e.g., "CAC Login", "Google")                                                                                                                          |
| `issuer`               | Yes      | OIDC issuer URL. Discovery via `{issuer}/.well-known/openid-configuration`                                                                                                            |
| `client-id`            | Yes      | OIDC client ID registered with the IdP                                                                                                                                                |
| `client-secret`        | Yes      | OIDC client secret. Supports `file:`/`cmd:` prefix for secret management                                                                                                              |
| `scopes`               | No       | Space-separated scopes (default: `openid email profile`)                                                                                                                              |
| `ca-cert`              | No       | Path to a CA certificate PEM file for IdPs with custom/private CAs                                                                                                                    |
| `token-validation-pem` | No       | Inline RSA/EC public key PEM for static token validation (skips JWKS discovery)                                                                                                       |
| `logout-redirect`      | No       | If `true`, logout redirects to the IdP's `end_session_endpoint` (RP-Initiated Logout). Default: `false` (local-only logout)                                                           |
| `trust-email`          | No       | If `true`, skip the `email_verified` check for this provider. Default: `false` (require `email_verified: true` in the ID token). See [Email Verification](#email-verification) below. |

## Email Verification

By default, Klangk requires the OIDC ID token to contain `email_verified: true`. If the claim is missing or `false`, the login is rejected with HTTP 403. This prevents an IdP that asserts an unverified email from being used to take over an existing account.

Not all IdPs include the `email_verified` claim. If your IdP does not emit it, or you trust it to only return verified emails, set `trust-email: true` on that provider entry:

```yaml
- id: company-idp
  display-name: Company SSO
  issuer: https://sso.company.com
  client-id: klangk
  client-secret: "file:/run/secrets/oidc-secret"
  trust-email: true # skip email_verified check — we trust this IdP
```

This is configured per provider, so a deployment with multiple IdPs can trust some and not others.

## How It Works

- **Web**: Login page shows one button per provider. Clicking redirects to the IdP via Authorization Code flow with PKCE. After authentication, the IdP redirects back to Klangk which exchanges the code for tokens, validates the ID token, and issues a Klangk JWT.
- **CLI**: `klangkc login` detects OIDC from the server config, opens a browser for authentication, and receives the token via a temporary localhost callback server.
- **Login hook**: A Python script (`KLANGK_OIDC_LOGIN_HOOK`) can handle login validation and group mapping. See [OIDC Login Hook](#oidc-login-hook) below.
- **User provisioning**: On first OIDC login, a user is created automatically (verified, no password). If a local user with the same email already exists, the OIDC identity is linked to it.
- **OIDC users** cannot use forgot-password, change-password, or change-email.
- **Logout**: By default, logout only kills the Klangk session. With `logout-redirect: true`, the user is also redirected to the IdP's logout endpoint to end the SSO session (requires full re-authentication on next login).

## IdP Setup (Keycloak Example)

1. Create a client in your Keycloak realm:
   - Client ID: `klangk`
   - Client authentication: On (confidential)
   - Valid redirect URIs: `https://your-klangk-host/api/v1/auth/oidc/cac/callback` (one per provider ID)
   - Web origins: `https://your-klangk-host`
2. Copy the client secret to a file or set it directly in the OIDC config
3. For CAC: configure the X.509 client certificate authenticator in the Keycloak authentication flow

## OIDC Login Hook

A Python script can handle login validation and group mapping on every OIDC login. The hook runs after the `email_verified` check (see [Email Verification](#email-verification)), so it only sees logins that have already passed that gate.

**Configuration:**

```bash
KLANGK_OIDC_LOGIN_HOOK=/etc/klangk/login_hook.py
```

The value is a file path to a Python script. The file is loaded directly — it does **not** need to be on `PYTHONPATH`. Optionally append `:func_name` to specify the function; if omitted it defaults to `on_login`:

```bash
KLANGK_OIDC_LOGIN_HOOK=/etc/klangk/login_hook.py:require_invitation
```

If not set, all OIDC logins that pass the `email_verified` check are accepted with no group sync.

**Hook signature:**

```python
def on_login(provider, claims, email, tokens):
    """Validate the login and optionally return group names.

    Args:
        provider: OIDCProvider object (id, issuer, client_id, etc.)
        claims: decoded ID token payload (sub, email, custom claims)
        email: user's email
        tokens: raw token response (id_token, access_token, etc.)

    Returns:
        None — login allowed, no group sync
        set[str] — login allowed, sync memberships to these groups

    Raises:
        Any exception — login rejected (message shown to user)
    """
    # Example: map IdP roles to groups
    groups = set()
    roles = claims.get("realm_access", {}).get("roles", [])
    if "klangk-admin" in roles:
        groups.add("admin")
    return groups or None
```

Async hooks are also supported (`async def`). The hook script can import from `klangkd` (e.g. `from klangkd.model import get_db`) since the backend packages are on `sys.path` at runtime.

**Behavior:**

- Called after ID token validation and `email_verified` check, before user provisioning
- **Raise** an exception — login rejected (HTTP 403, exception message shown)
- **Return** `None` — login allowed, no group sync
- **Return** a `set[str]` — login allowed, memberships synced to those groups
- Groups returned by the hook are auto-created if they don't exist
- Memberships are tracked with `source='oidc_sync'` — only these are added/removed
- Manual group memberships (`source='manual'`) are never touched

**Example:** see `customize/login_hook.py` for a hook that restricts logins to invited users.
