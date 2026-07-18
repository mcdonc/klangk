# `klangkd` Configuration File

The `klangkd` launcher reads configuration from a YAML file, environment variables, and built-in defaults. The config file is the primary substrate for deployment settings — environment variables remain supported as overrides.

## Precedence

Values are resolved in this order (highest priority first):

1. **Environment variables** (`KLANGK_*`) — override everything
2. **Config file** — the YAML file passed to `--config`
3. **Built-in defaults** — hardcoded in the settings model

This means an operator can set the bulk of a deployment's config in the YAML file and still tweak individual values via env vars (e.g. in CI, ansible, or `--env-file`) without rewriting the file.

## `--config` flag

`klangkd` requires a config file by default. There are three modes:

| Invocation                              | Behavior                                                        |
| --------------------------------------- | --------------------------------------------------------------- |
| `klangkd`                               | Requires `/etc/klangkd.yaml` to exist. Missing → startup error. |
| `klangkd --config /path/to/config.yaml` | Uses the specified file. Missing → startup error.               |
| `klangkd --config=none`                 | No config file — env vars and built-in defaults only.           |

There is no silent fallback. The only way to run without a config file is `--config=none`.

## Key mapping

Config-file keys map directly to `KLANGK_*` environment variable names with the prefix stripped and lowercased. For example:

| Env var              | Config-file key |
| -------------------- | --------------- |
| `KLANGK_JWT_SECRET`  | `jwt_secret`    |
| `KLANGK_EGRESS_PORT` | `egress_port`   |
| `KLANGK_AUTH_MODES`  | `auth_modes`    |

### `snake_case` or `kebab-case`

Every config-file key accepts **either** `snake_case` or `kebab-case` — `jwt_secret` and `jwt-secret` resolve to the same setting, as do `egress_port` / `egress-port`, `auth_modes` / `auth-modes`, and so on ([#1538](https://github.com/mcdonc/klangk/issues/1538)). This matches the dual-form lookup the OIDC provider dicts already had and is forgiving of either style, but **`snake_case` is the preferred/documented form** — all examples in this chapter (and the field names they map to) use `snake_case`.

## `file:` and `cmd:` resolution

Any value — whether from the config file or an env var — can use `file:` or `cmd:` prefixes to resolve secrets at **construction time** (not per-call at use time):

```yaml
jwt_secret: "file:/run/secrets/jwt"
smtp_password: "cmd:aws secretsmanager get-secret-value --secret-id klangk/smtp | jq -r .SecretString"
```

The resolver is source-agnostic: it sees only the value, not where it came from. `KlangkSettings` runs every string field through the resolver once, in a `model_validator(mode="after")`, before the object leaves `__init__` ([#1461](https://github.com/mcdonc/klangk/issues/1461)). A bad reference (`file:/nonexistent`, `cmd:false`) fails fast at boot with a `ValidationError` — not silently at use time. Thereafter every `settings.field` read returns the already-resolved value; no caller wraps in a resolver call.

## Inline OIDC providers

OIDC providers can be specified directly in the config file under the `oidc_providers` key, removing the need for a separate OIDC config file:

```yaml
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
    trust-email: true
```

If `KLANGK_OIDC_CONFIG` is also set (as an env var), the separate file wins — consistent with the global precedence rule (env vars override config-file values). When `KLANGK_OIDC_CONFIG` is unset, the inline `oidc_providers` list is used. See [OIDC Configuration](oidc.md) for provider field details.

## Complete example

A production-ready config file covering all common settings:

```yaml
# /etc/klangkd.yaml

# --- Auth / identity ---
auth_modes: both
jwt_secret: "file:/run/secrets/jwt"
prevent_insecure_jwt_secret: "1"
default_user: admin@example.com
default_password: "file:/run/secrets/admin-pw"
access_token_hours: "24"
min_password_length: "12"

# --- Server / network ---
listen: "127.0.0.1"
port: "8997"
egress_port: "8995"
hosting_hostname: klangk.example.com
hosting_proto: https
trusted_proxy_cidrs: "127.0.0.1,::1,10.0.0.0/8"

# --- Storage (required, no defaults) ---
state_dir: /var/lib/klangk/state
data_dir: /var/lib/klangk/data

# --- Container / workspace ---
image_name: klangk-workspace
image_pull_policy: missing
data_dir: /var/lib/klangk/data
port_range_start: "9000"
allow_sudo: "true"

# --- OIDC (inline) ---
oidc_providers:
  - id: company-sso
    display-name: Company SSO
    issuer: https://sso.example.com/realms/main
    client-id: klangk
    client-secret: "file:/run/secrets/oidc-secret"
    trust-email: true

# --- SMTP ---
smtp_host: smtp.example.com
smtp_port: "587"
smtp_user: klangk@example.com
smtp_password: "file:/run/secrets/smtp-pw"
smtp_from: noreply@example.com
smtp_use_tls: "true"

# --- Branding ---
product_name: "My Platform"
brand_color: "#1565C0"
logo_url: https://example.com/logo.png
# Require consent banner acknowledgement on every fresh app load / login
login_banner_every_visit: true
```

## All configuration keys

Every key below corresponds to a `KLANGK_*` environment variable (uppercased, with `KLANGK_` prefix). See [Environment Variables](environment.md) for detailed descriptions of each.

### Deployment shape (derived from `KLANGK_PORT` + `KLANGK_AUTH_MODE`)

The deployment shape is **derived** from two knobs: the browser port
(`KLANGK_PORT`) and the auth gate (`KLANGK_AUTH_MODE`). klangk picks the
proxy template and enforces its one safety rule from their combination —
there is no separate "mode" to set.

**`KLANGK_PORT`** is the browser/proxy port. **Unset ⇒ headless** (no browser
listener is rendered; the proxy serves only the container-egress listener on
`KLANGK_EGRESS_PORT`). **Set ⇒ full/browser mode** — the proxy serves the browser
UI, API, WebSocket, and hosted apps on `listen {KLANGK_LISTEN}:{KLANGK_PORT};`
plus a separate container-egress listener on `KLANGK_EGRESS_PORT`.

**`KLANGK_AUTH_MODE`** is the sole authority on the auth gate (`none` /
`password` / `oidc` / `both`). When unset it defaults to `none`; OIDC settings
never promote it.

For each combination, klangk renders the **maximum-feature proxy template
the combination can service** (the proxy is Caddy by default since #1634;
nginx remains selectable as a deprecated fallback):

| `KLANGK_PORT`    | `KLANGK_AUTH_MODE`     | proxy template | browser?    | status                                               |
| ---------------- | ---------------------- | -------------- | ----------- | ---------------------------------------------------- |
| unset (headless) | none                   | headless       | no          | ✅ (most secure)                                     |
| unset (headless) | password / oidc / both | headless       | no          | ✅ (most secure, less convenient)                    |
| loopback TCP     | none                   | full           | yes (local) | ✅ (local-dev "just works")                          |
| loopback TCP     | password / oidc / both | full           | yes (local) | ✅                                                   |
| non-loopback TCP | none                   | —              | —           | ⚠️ rejected unless `KLANGK_ALLOW_INSECURE_NO_AUTH=1` |
| non-loopback TCP | password / oidc / both | full           | yes (net)   | ✅ (least secure; operator opted in)                 |

- **Template selection keys off `KLANGK_PORT` only**: unset ⇒ headless
  (container-egress only — no browser UI); set ⇒ full (browser UI + API +
  hosted apps). `KLANGK_AUTH_MODE` does **not** change which template renders.
- **The one gate** (`none` on non-loopback `KLANGK_LISTEN`) is enforced by
  `enforce_no_auth_bind_safety()` at boot — refused unless
  `KLANGK_ALLOW_INSECURE_NO_AUTH=1` is set (e.g. a throwaway VM on an
  isolated network). No-auth mode freely issues an admin token, so exposing
  it off-loopback is opt-in, not silent.

**Security ordering** (most → least secure): headless+password > headless+none

> loopback TCP (local) > non-loopback TCP+gate. Headless is the most-secure
> posture: the backend binds only a UDS (same-uid socket access), and the proxy
> serves only the container-egress listener — no browser/TCP surface at all.
> (The default proxy engine is Caddy since #1634; nginx remains selectable as
> a deprecated fallback.)

| Key      | Default     | Env var         |
| -------- | ----------- | --------------- |
| `listen` | `127.0.0.1` | `KLANGK_LISTEN` |

```yaml
# --- Deployment shape ---
# port is the browser/proxy port. Unset ⇒ headless (no browser listener).
# Set ⇒ full/browser mode (UI + API + hosted apps).
port: "8997"
# listen is the browser interface address (rendered only when port is set).
# listen: "127.0.0.1"  # browser interface address (default loopback; set 0.0.0.0 for all interfaces)
# egress_listen is the egress interface address. Defaults to 0.0.0.0 (all
# interfaces) — the only portable default across podman network modes. The
# three served egress locations are double-gated (CONTAINER_ACL deny-all →
# 403 outside the container subnet, plus auth_request workspace-token → 401
# without a valid JWT), so the all-interfaces bind is not a security hole.
# Pin to a specific host IP if your container-facing interface is stable.
# egress_listen: "0.0.0.0"
# auth_modes is the sole auth authority; unset defaults to none.
# auth_modes: password  # or oidc / both / none
```

### Auth / identity

| Key                           | Default                  | Env var                              |
| ----------------------------- | ------------------------ | ------------------------------------ |
| `auth_modes`                  | `none`                   | `KLANGK_AUTH_MODES`                  |
| `jwt_secret`                  | _(insecure dev default)_ | `KLANGK_JWT_SECRET`                  |
| `prevent_insecure_jwt_secret` |                          | `KLANGK_PREVENT_INSECURE_JWT_SECRET` |
| `default_user`                | `admin@example.com`      | `KLANGK_DEFAULT_USER`                |
| `default_password`            |                          | `KLANGK_DEFAULT_PASSWORD`            |
| `access_token_hours`          | `24`                     | `KLANGK_ACCESS_TOKEN_HOURS`          |
| `workspace_token_hours`       | `24`                     | `KLANGK_WORKSPACE_TOKEN_HOURS`       |
| `min_password_length`         | `8`                      | `KLANGK_MIN_PASSWORD_LENGTH`         |
| `login_lockout_failures`      | `5`                      | `KLANGK_LOGIN_LOCKOUT_FAILURES`      |
| `login_lockout_duration`      | `900`                    | `KLANGK_LOGIN_LOCKOUT_DURATION`      |
| `login_lockout_window`        | `300`                    | `KLANGK_LOGIN_LOCKOUT_WINDOW`        |
| `disable_registration`        |                          | `KLANGK_DISABLE_REGISTRATION`        |
| `disable_invites`             |                          | `KLANGK_DISABLE_INVITES`             |
| `invite_expire_hours`         | `72`                     | `KLANGK_INVITE_EXPIRE_HOURS`         |
| `allow_insecure_no_auth`      |                          | `KLANGK_ALLOW_INSECURE_NO_AUTH`      |
| `reject_proxy_headers`        |                          | `KLANGK_REJECT_PROXY_HEADERS`        |
| `trusted_proxy_cidrs`         | `127.0.0.1,::1`          | `KLANGK_TRUSTED_PROXY_CIDRS`         |

### Server / network

| Key                      | Default                          | Env var                         |
| ------------------------ | -------------------------------- | ------------------------------- |
| `listen`                 | `127.0.0.1`                      | `KLANGK_LISTEN`                 |
| `port`                   | _(unset)_                        | `KLANGK_PORT`                   |
| `egress_port`            | `8995`                           | `KLANGK_EGRESS_PORT`            |
| `egress_listen`          | `0.0.0.0`                        | `KLANGK_EGRESS_LISTEN`          |
| `proxy_port`             | _(deprecated)_                   | `KLANGK_PROXY_PORT`             |
| `socket`                 | `<state_dir>/klangk.sock`        | `KLANGK_SOCKET`                 |
| `caddy_admin_socket`     | `<state_dir>/caddy-admin.sock`   | `KLANGK_CADDY_ADMIN_SOCKET`     |
| `port_range_start`       | `9000`                           | `KLANGK_PORT_RANGE_START`       |
| `cors_origins`           |                                  | `KLANGK_CORS_ORIGINS`           |
| `frontend_dir`           | _(in-package `klangk/frontend`)_ | `KLANGK_FRONTEND_DIR`           |
| `dns_servers`            |                                  | `KLANGK_DNS_SERVERS`            |
| `hosting_hostname`       | _(auto-derived)_                 | `KLANGK_HOSTING_HOSTNAME`       |
| `hosting_proto`          | _(auto-derived)_                 | `KLANGK_HOSTING_PROTO`          |
| `hosting_base_path`      | _(auto-derived)_                 | `KLANGK_HOSTING_BASE_PATH`      |
| `bridge_timeout_seconds` |                                  | `KLANGK_BRIDGE_TIMEOUT_SECONDS` |
| `idle_timeout_seconds`   | `1800`                           | `KLANGK_IDLE_TIMEOUT_SECONDS`   |

### Container / workspace

| Key                          | Default               | Env var                             |
| ---------------------------- | --------------------- | ----------------------------------- |
| `data_dir`                   | **required**          | `KLANGK_DATA_DIR`                   |
| `state_dir`                  | **required**          | `KLANGK_STATE_DIR`                  |
| `customize_dir`              |                       | `KLANGK_CUSTOMIZE_DIR`              |
| `plugins_dir`                | `<state_dir>/plugins` | `KLANGK_PLUGINS_DIR`                |
| `image_name`                 | `klangk-workspace`    | `KLANGK_IMAGE_NAME`                 |
| `image_pull_policy`          | `never`               | `KLANGK_IMAGE_PULL_POLICY`          |
| `allowed_images`             |                       | `KLANGK_ALLOWED_IMAGES`             |
| `allowed_mount_roots`        |                       | `KLANGK_ALLOWED_MOUNT_ROOTS`        |
| `allow_autostart`            |                       | `KLANGK_ALLOW_AUTOSTART`            |
| `allow_sudo`                 |                       | `KLANGK_ALLOW_SUDO`                 |
| `container_subnets`          | _(auto-derived)_      | `KLANGK_CONTAINER_SUBNETS`          |
| `userns`                     |                       | `KLANGK_USERNS`                     |
| `podman_bin`                 | `podman`              | `KLANGK_PODMAN_BIN`                 |
| `disable_tmux`               |                       | `KLANGK_DISABLE_TMUX`               |
| `health_check_interval`      |                       | `KLANGK_HEALTH_CHECK_INTERVAL`      |
| `health_check_startup_grace` |                       | `KLANGK_HEALTH_CHECK_STARTUP_GRACE` |
| `health_check_timeout`       |                       | `KLANGK_HEALTH_CHECK_TIMEOUT`       |
| `hosted_ports_per_workspace` | `5`                   | `KLANGK_HOSTED_PORTS_PER_WORKSPACE` |
| `test_mode`                  |                       | `KLANGK_TEST_MODE`                  |
| `version_file`               |                       | `KLANGK_VERSION_FILE`               |

### LLM

| Key            | Default | Env var               |
| -------------- | ------- | --------------------- |
| `llm_base_url` |         | `KLANGK_LLM_BASE_URL` |
| `llm_api_key`  |         | `KLANGK_LLM_API_KEY`  |
| `llm_model`    |         | `KLANGK_LLM_MODEL`    |

### OIDC

| Key               | Default | Env var                                    |
| ----------------- | ------- | ------------------------------------------ |
| `oidc_config`     |         | `KLANGK_OIDC_CONFIG`                       |
| `oidc_login_hook` |         | `KLANGK_OIDC_LOGIN_HOOK`                   |
| `oidc_providers`  |         | _(inline only — not settable via env var)_ |

### SMTP / email

| Key                   | Default    | Env var                      |
| --------------------- | ---------- | ---------------------------- |
| `smtp_host`           |            | `KLANGK_SMTP_HOST`           |
| `smtp_port`           | `587`      | `KLANGK_SMTP_PORT`           |
| `smtp_user`           |            | `KLANGK_SMTP_USER`           |
| `smtp_password`       |            | `KLANGK_SMTP_PASSWORD`       |
| `smtp_from`           |            | `KLANGK_SMTP_FROM`           |
| `smtp_reply_to`       |            | `KLANGK_SMTP_REPLY_TO`       |
| `smtp_use_tls`        | `true`     | `KLANGK_SMTP_USE_TLS`        |
| `sendmail_path`       | `sendmail` | `KLANGK_SENDMAIL_PATH`       |
| `email_templates_dir` |            | `KLANGK_EMAIL_TEMPLATES_DIR` |

### Legal / support links

| Key             | Default | Env var                |
| --------------- | ------- | ---------------------- |
| `terms_url`     |         | `KLANGK_TERMS_URL`     |
| `privacy_url`   |         | `KLANGK_PRIVACY_URL`   |
| `aup_url`       |         | `KLANGK_AUP_URL`       |
| `support_url`   |         | `KLANGK_SUPPORT_URL`   |
| `support_email` |         | `KLANGK_SUPPORT_EMAIL` |

### Branding / UI

| Key                        | Default   | Env var                           |
| -------------------------- | --------- | --------------------------------- |
| `product_name`             | `Klangk`  | `KLANGK_PRODUCT_NAME`             |
| `logo_url`                 |           | `KLANGK_LOGO_URL`                 |
| `brand_color`              | `#E65100` | `KLANGK_BRAND_COLOR`              |
| `login_banner`             |           | `KLANGK_LOGIN_BANNER`             |
| `login_banner_title`       |           | `KLANGK_LOGIN_BANNER_TITLE`       |
| `login_banner_every_visit` | `false`   | `KLANGK_LOGIN_BANNER_EVERY_VISIT` |
| `terminal_banner`          |           | `KLANGK_TERMINAL_BANNER`          |

### Agent

| Key              | Default               | Env var                 |
| ---------------- | --------------------- | ----------------------- |
| `agent_email`    | `clanker@example.com` | `KLANGK_AGENT_EMAIL`    |
| `agent_handle`   | `clanker`             | `KLANGK_AGENT_HANDLE`   |
| `agent_disabled` |                       | `KLANGK_AGENT_DISABLED` |

### SSL / certs

| Key            | Default | Env var               |
| -------------- | ------- | --------------------- |
| `ssl_cert_dir` |         | `KLANGK_SSL_CERT_DIR` |

### File upload

| Key                    | Default     | Env var                       |
| ---------------------- | ----------- | ----------------------------- |
| `file_upload_size_max` | `524288000` | `KLANGK_FILE_UPLOAD_SIZE_MAX` |
