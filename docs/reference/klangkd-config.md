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
| `klangkd`                               | Requires `/etc/klangkd.conf` to exist. Missing → startup error. |
| `klangkd --config /path/to/config.yaml` | Uses the specified file. Missing → startup error.               |
| `klangkd --config=none`                 | No config file — env vars and built-in defaults only.           |

There is no silent fallback. The only way to run without a config file is `--config=none`.

## Key mapping

Config-file keys map directly to `KLANGK_*` environment variable names with the prefix stripped and lowercased. For example:

| Env var             | Config-file key |
| ------------------- | --------------- |
| `KLANGK_JWT_SECRET` | `jwt_secret`    |
| `KLANGK_NGINX_PORT` | `nginx_port`    |
| `KLANGK_AUTH_MODES` | `auth_modes`    |

## `file:` and `cmd:` resolution

Any value — whether from the config file or an env var — can use `file:` or `cmd:` prefixes to resolve secrets at runtime:

```yaml
jwt_secret: "file:/run/secrets/jwt"
smtp_password: "cmd:aws secretsmanager get-secret-value --secret-id klangk/smtp | jq -r .SecretString"
```

The resolver is source-agnostic: it sees only the value, not where it came from.

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
# /etc/klangkd.conf

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
nginx_port: "8995"
hosting_hostname: klangk.example.com
hosting_proto: https
trusted_proxy_cidrs: "127.0.0.1,::1,10.0.0.0/8"

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
```

## All configuration keys

Every key below corresponds to a `KLANGK_*` environment variable (uppercased, with `KLANGK_` prefix). See [Environment Variables](environment.md) for detailed descriptions of each.

### Deployment UI mode

`KLANGK_UI_MODE` (config key `ui_mode`) is the single deployment-shape selector — selects one of four corners of the (UI × auth-gate) space, with values named for the operator _experience_ (do I get a UI?) rather than the transport:

| `ui_mode`    | UI             | transport | auth gate |
| ------------ | -------------- | --------- | --------- |
| `cli-noauth` | headless (CLI) | UDS       | off       |
| `cli-auth`   | headless (CLI) | UDS       | on        |
| `web-noauth` | browser        | TCP       | off       |
| `web-auth`   | browser        | TCP       | on        |

Everything else is _derived_ from the ui_mode and is **not** individually configurable: the auth gate is the `-auth`/`-noauth` suffix; UI presence is the `cli-`/`web-` prefix (a browser can't ingress over a UDS, so `cli-*` is headless and `web-*` is browser-facing); container egress paths are a fixed per-ui_mode default. The one thing the operator still chooses separately is the auth **backend** (password vs OIDC vs both) via the existing `KLANGK_AUTH_MODES` — that decision can't be made for them.

`KLANGK_UI_MODE` and `KLANGK_AUTH_MODES` are cross-validated at startup: a `*-noauth` ui_mode requires `KLANGK_AUTH_MODES=none`; a `*-auth` ui_mode requires a gated backend (`password`/`oidc`/`both`). A conflicting config fails fast with a `ConfigurationError`. When `KLANGK_AUTH_MODES` is **unset**, it self-defaults to match the ui_mode — `password` for `*-auth`, `none` for `*-noauth` — so a ui_mode alone boots cleanly without an explicit backend. (OIDC is then opt-in by setting `KLANGK_AUTH_MODES=oidc` or `both`.)

| Key       | Default | Env var          |
| --------- | ------- | ---------------- |
| `ui_mode` |         | `KLANGK_UI_MODE` |

```yaml
# --- Deployment UI mode (#1397) ---
# One of: cli-noauth | cli-auth | web-noauth | web-auth
# The auth backend is chosen separately via KLANGK_AUTH_MODES:
ui_mode: web-auth # + KLANGK_AUTH_MODES=oidc (a ui_mode with a gate needs a backend)
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

| Key                      | Default          | Env var                         |
| ------------------------ | ---------------- | ------------------------------- |
| `listen`                 | `127.0.0.1`      | `KLANGK_LISTEN`                 |
| `nginx_port`             | `8995`           | `KLANGK_NGINX_PORT`             |
| `port_range_start`       | `9000`           | `KLANGK_PORT_RANGE_START`       |
| `cors_origins`           |                  | `KLANGK_CORS_ORIGINS`           |
| `dns_servers`            |                  | `KLANGK_DNS_SERVERS`            |
| `hosting_hostname`       | _(auto-derived)_ | `KLANGK_HOSTING_HOSTNAME`       |
| `hosting_proto`          | _(auto-derived)_ | `KLANGK_HOSTING_PROTO`          |
| `hosting_base_path`      | _(auto-derived)_ | `KLANGK_HOSTING_BASE_PATH`      |
| `bridge_timeout_seconds` |                  | `KLANGK_BRIDGE_TIMEOUT_SECONDS` |
| `idle_timeout_seconds`   | `1800`           | `KLANGK_IDLE_TIMEOUT_SECONDS`   |

### Container / workspace

| Key                          | Default            | Env var                             |
| ---------------------------- | ------------------ | ----------------------------------- |
| `data_dir`                   |                    | `KLANGK_DATA_DIR`                   |
| `customize_dir`              |                    | `KLANGK_CUSTOMIZE_DIR`              |
| `plugins_dir`                |                    | `KLANGK_PLUGINS_DIR`                |
| `image_name`                 | `klangk-workspace` | `KLANGK_IMAGE_NAME`                 |
| `image_pull_policy`          | `never`            | `KLANGK_IMAGE_PULL_POLICY`          |
| `allowed_images`             |                    | `KLANGK_ALLOWED_IMAGES`             |
| `allowed_mount_roots`        |                    | `KLANGK_ALLOWED_MOUNT_ROOTS`        |
| `allow_autostart`            |                    | `KLANGK_ALLOW_AUTOSTART`            |
| `allow_sudo`                 |                    | `KLANGK_ALLOW_SUDO`                 |
| `container_subnets`          | _(auto-derived)_   | `KLANGK_CONTAINER_SUBNETS`          |
| `userns`                     |                    | `KLANGK_USERNS`                     |
| `podman_bin`                 | `podman`           | `KLANGK_PODMAN_BIN`                 |
| `disable_tmux`               |                    | `KLANGK_DISABLE_TMUX`               |
| `health_check_interval`      |                    | `KLANGK_HEALTH_CHECK_INTERVAL`      |
| `health_check_startup_grace` |                    | `KLANGK_HEALTH_CHECK_STARTUP_GRACE` |
| `health_check_timeout`       |                    | `KLANGK_HEALTH_CHECK_TIMEOUT`       |
| `hosted_ports_per_workspace` | `5`                | `KLANGK_HOSTED_PORTS_PER_WORKSPACE` |
| `test_mode`                  |                    | `KLANGK_TEST_MODE`                  |
| `version_file`               |                    | `KLANGK_VERSION_FILE`               |

### LLM

| Key           | Default | Env var              |
| ------------- | ------- | -------------------- |
| `llm_api_key` |         | `KLANGK_LLM_API_KEY` |
| `llm_model`   |         | `KLANGK_LLM_MODEL`   |

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

| Key                  | Default   | Env var                     |
| -------------------- | --------- | --------------------------- |
| `product_name`       | `Klangk`  | `KLANGK_PRODUCT_NAME`       |
| `logo_url`           |           | `KLANGK_LOGO_URL`           |
| `brand_color`        | `#E65100` | `KLANGK_BRAND_COLOR`        |
| `login_banner`       |           | `KLANGK_LOGIN_BANNER`       |
| `login_banner_title` |           | `KLANGK_LOGIN_BANNER_TITLE` |
| `terminal_banner`    |           | `KLANGK_TERMINAL_BANNER`    |

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

| Key                    | Default | Env var                       |
| ---------------------- | ------- | ----------------------------- |
| `file_upload_size_max` |         | `KLANGK_FILE_UPLOAD_SIZE_MAX` |
