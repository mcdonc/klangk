# OIDC Configuration

Klangk supports OIDC authentication via one or more external Identity Providers (e.g., Keycloak). This is used for CAC card login and other SSO scenarios. Klangk is a standard OIDC relying party — the IdP handles all certificate/credential complexity.

## Setup

1. Create a YAML config file with your OIDC providers:

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

1. Set `KLANGK_OIDC_CONFIG` in `.env`:

```bash
KLANGK_OIDC_CONFIG=/path/to/oidc.yaml
```

1. Optionally set `KLANGK_AUTH_MODES` to control which login methods are available:
   - `both` (default when OIDC configured) — SSO buttons + email/password form
   - `oidc` — SSO buttons only, email/password disabled
   - `password` — email/password only (same as no OIDC config)

## Provider Config Fields

| Field                  | Required | Description                                                                                                                 |
| ---------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------- |
| `id`                   | Yes      | URL-safe slug, used in endpoint paths (`/api/v1/auth/oidc/{id}/login`) and stored as `provider` on users                    |
| `display-name`         | Yes      | Button label on the login page (e.g., "CAC Login", "Google")                                                                |
| `issuer`               | Yes      | OIDC issuer URL. Discovery via `{issuer}/.well-known/openid-configuration`                                                  |
| `client-id`            | Yes      | OIDC client ID registered with the IdP                                                                                      |
| `client-secret`        | Yes      | OIDC client secret. Supports `file:`/`cmd:` prefix for secret management                                                    |
| `scopes`               | No       | Space-separated scopes (default: `openid email profile`)                                                                    |
| `ca-cert`              | No       | Path to a CA certificate PEM file for IdPs with custom/private CAs                                                          |
| `token-validation-pem` | No       | Inline RSA/EC public key PEM for static token validation (skips JWKS discovery)                                             |
| `logout-redirect`      | No       | If `true`, logout redirects to the IdP's `end_session_endpoint` (RP-Initiated Logout). Default: `false` (local-only logout) |

## How It Works

- **Web**: Login page shows one button per provider. Clicking redirects to the IdP via Authorization Code flow with PKCE. After authentication, the IdP redirects back to Klangk which exchanges the code for tokens, validates the ID token, and issues a Klangk JWT.
- **CLI**: `klangkc login` detects OIDC from the server config, opens a browser for authentication, and receives the token via a temporary localhost callback server.
- **Login hook**: A single Python hook (`KLANGK_OIDC_LOGIN_HOOK`) handles both login validation and group mapping. See [OIDC Login Hook](#oidc-login-hook) below.
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

A single Python hook handles both login validation and group mapping on every OIDC login.

**Configuration:**

```bash
KLANGK_OIDC_LOGIN_HOOK=my_module.on_login
```

The value is a dotted Python path where the last component is the function name. If not set, all OIDC logins are accepted with no group sync.

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
    # Example: reject unverified emails
    if claims.get("email_verified") is not True:
        raise ValueError("Email not verified by identity provider")

    # Example: map IdP roles to groups
    groups = set()
    roles = claims.get("realm_access", {}).get("roles", [])
    if "klangk-admin" in roles:
        groups.add("admin")
    return groups or None
```

Async hooks are also supported (`async def`).

**Behavior:**

- Called after ID token validation, before user provisioning
- **Raise** an exception — login rejected (HTTP 403, exception message shown)
- **Return** `None` — login allowed, no group sync
- **Return** a `set[str]` — login allowed, memberships synced to those groups
- Groups returned by the hook are auto-created if they don't exist
- Memberships are tracked with `source='oidc_sync'` — only these are added/removed
- Manual group memberships (`source='manual'`) are never touched

**Security note:** The hook is entirely responsible for login validation and group mapping. There is no default hook — the hook is written by the deploying organization, and it is their responsibility to validate IdP claims as appropriate. Without a hook, all OIDC logins are accepted (including those with unverified emails).

**Built-in example hooks:**

```bash
# Reject logins with unverified emails
KLANGK_OIDC_LOGIN_HOOK=klangk_backend.oidc.example_require_verified_email

# Map Keycloak klangk-admin role to the admin group
KLANGK_OIDC_LOGIN_HOOK=klangk_backend.oidc.example_admin_hook
```
