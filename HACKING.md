# Hacking on Klangk

## Prerequisites

- Podman (rootless) available
- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/) installed (or run `./bootstrap`)

## Getting Started

```bash
git clone git@github.com:mcdonc/klangk.git
cd klangk

# Create .env from the example
cp -n .env.example .env

# Edit .env with your credentials
cat > .env << 'EOF'
KLANGK_LLM_API_KEY=your-api-key-here
# Any OpenAI-compatible provider (or http://localhost:11434/v1 for self-hosted)
KLANGK_LLM_BASE_URL=https://ollama.com/v1
# Any model available on your provider
KLANGK_LLM_MODEL=gemma4:31b
KLANGK_JWT_SECRET=change-this-to-a-random-secret
KLANGK_DEFAULT_USER=admin@example.com
# Omit to generate a random password on first run
# KLANGK_DEFAULT_PASSWORD=admin
EOF

# Install Nix and devenv (if not already installed)
./bootstrap
```

## Entering the Dev Shell

All commands below assume you're inside the devenv shell:

```bash
devenv shell
```

This puts all project tools (Python, Flutter, Dart, Node, podman, etc.) on your PATH.

## Starting the Dev Environment

```bash
devenv processes up --no-tui
```

This builds the workspace image and Flutter web app on first run (via `execIfModified`), starts nginx, the FastAPI backend, and watches for file changes. Open http://localhost:8995.

## Running Tests

```bash
# Backend unit tests (Python, pytest, parallel)
test-backend

# Frontend unit tests (Dart, flutter test, 100% coverage required)
test-frontend

# CLI E2E tests (starts real server + podman containers)
test-cli-e2e

# Flutter E2E tests (Playwright, needs flutter build + podman build)
test-frontend-e2e

# Run a specific E2E test
cd src/frontend/e2e-tests && npx playwright test --project=chromium --no-deps --grep "test name"
```

Backend tests for Python changes, frontend tests for Dart changes. Run E2E tests before committing cross-cutting changes.

## Environment Variables

`$DEVENV_STATE` refers to `<project root>/.devenv/state` — this is where devenv stores runtime data.

All settings can be overridden in `.env`. Defaults (where appropriate) are provided in `devenv.nix` at low priority so `.env` values take precedence.

**`file:` prefix:** Any env var can be prefixed with `file:` to read the value from a file at runtime (e.g. `KLANGK_JWT_SECRET=file:/run/secrets/jwt`). The file contents are stripped of leading/trailing whitespace. This works with secret management tools like agenix/sops that write decrypted secrets to files. If the file cannot be read, an error is logged and the value is treated as unset.

| Variable                             | Default                              | Description                                                                                                                                                                                |
| ------------------------------------ | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `KLANGK_NGINX_PORT`                  | `8995`                               | **Primary access point** — nginx reverse proxy port (UI, API, WebSocket, hosted apps)                                                                                                      |
| `KLANGK_PORT`                        | `8997`                               | Backend (FastAPI/uvicorn) port — proxied through nginx, not accessed directly                                                                                                              |
| `KLANGK_DATA_DIR`                    | `$DEVENV_STATE/klangk/data`          | Database, workspaces, Pi sessions                                                                                                                                                          |
| `KLANGK_PLUGINS_DIR`                 | `$DEVENV_STATE/klangk/plugins`       | Fetched plugins (outside repo for `execIfModified`)                                                                                                                                        |
| `KLANGK_HOST_IMAGE`                  | `klangk-host`                        | Docker image name for `run-host-container`                                                                                                                                                 |
| `KLANGK_IMAGE_NAME`                  | `klangk-workspace`                   | Podman image name for workspace containers                                                                                                                                                 |
| `KLANGK_IMAGE_PULL_POLICY`           | `never`                              | Podman `--pull` policy for workspace containers (`never`, `missing`, `always`, `newer`). Default `never` requires the image to exist locally; `missing` pulls from a registry if not found |
| `KLANGK_PODMAN_STORAGE`              |                                      | Custom path for podman image storage (graphroot). Set to a path on ext4 (not ZFS) for `--userns=keep-id` support. ZFS lacks idmapped mounts, causing slow container startup.               |
| `KLANGK_ALLOWED_MOUNT_ROOTS`         |                                      | Comma-separated list of allowed host path prefixes for bind mounts (e.g., `/home,/data`). If unset, all bind mount paths are allowed. Protected paths are always blocked (see below).      |
| `KLANGK_INSTANCE_ID`                 | `default`                            | Instance identifier for multi-instance deployments on the same host — isolates containers, names, and cleanup                                                                              |
| `KLANGK_DNS_SERVERS`                 |                                      | Comma-separated DNS server IPs for containers (e.g., `100.100.100.100,8.8.8.8` for Tailscale MagicDNS). If unset, containers use podman's default DNS.                                     |
| `KLANGK_HOSTING_HOSTNAME`            | (auto-derived)                       | Hostname for hosted app URLs. Behind a reverse proxy: uses `X-Forwarded-Host` as-is. Direct access: uses `Host` header with `KLANGK_NGINX_PORT` substituted                                |
| `KLANGK_HOSTING_PROTO`               | (from `X-Forwarded-Proto` or `http`) | Protocol for user-facing app URLs. Auto-derived from request headers if not set                                                                                                            |
| `KLANGK_HOSTING_BASE_PATH`           | (from `X-Forwarded-Prefix` or empty) | Base path prefix for user-facing app URLs (e.g., `/klangk`). Auto-derived from nginx `X-Forwarded-Prefix` header if not set                                                                |
| `KLANGK_IDLE_TIMEOUT_SECONDS`        | `1800`                               | Container idle timeout in seconds (check interval auto-computed as timeout/3, clamped 10–60s)                                                                                              |
| `KLANGK_LOGIN_LOCKOUT_WINDOW`        | `300`                                | Time window in seconds for counting failed login attempts.                                                                                                                                 |
| `KLANGK_LOGIN_LOCKOUT_FAILURES`      | `0`                                  | Number of failed login attempts before a lockout. Default `0` (disabled).                                                                                                                  |
| `KLANGK_LOGIN_LOCKOUT_DURATION`      | `900`                                | Duration of lockout in seconds (only relevant when `KLANGK_LOGIN_LOCKOUT_FAILURES` > 0).                                                                                                   |
| `KLANGK_LLM_API_KEY`                 |                                      | LLM provider API key                                                                                                                                                                       |
| `KLANGK_LLM_BASE_URL`                |                                      | LLM API URL — must use IP or public FQDN, not bare hostnames (see [Tailscale note](#tailscale-and-llm-proxy))                                                                              |
| `KLANGK_LLM_MODEL`                   |                                      | LLM model name                                                                                                                                                                             |
| `KLANGK_JWT_SECRET`                  |                                      | JWT signing secret. A warning is logged at startup if unset or left as the insecure dev default.                                                                                           |
| `KLANGK_PREVENT_INSECURE_JWT_SECRET` |                                      | Set to `1` to fail at startup if `KLANGK_JWT_SECRET` is unset or insecure. Recommended for production.                                                                                     |
| `KLANGK_DEFAULT_USER`                |                                      | Auto-seeded admin email on startup                                                                                                                                                         |
| `KLANGK_DEFAULT_PASSWORD`            |                                      | Auto-seeded password on startup (omit to generate random; supports `file:` prefix)                                                                                                         |
| `KLANGK_MIN_PASSWORD_LENGTH`         | `4`                                  | Minimum password length                                                                                                                                                                    |
| `KLANGK_IMPORT_MAX_SIZE`             | `524288000`                          | Maximum upload size in bytes for workspace import (default 500 MB)                                                                                                                         |
| `KLANGK_WS_MSG_SIZE_MAX`             | `16777216`                           | Maximum WebSocket message size in bytes (default 16 MB). Applies to both the uvicorn server and the CLI client. Increase if syncing very large files via rsync over WebSocket.             |
| `KLANGK_DISABLE_REGISTRATION`        |                                      | Set to any non-empty value to block new user signups and hide the registration link in the UI                                                                                              |
| `KLANGK_OIDC_CONFIG`                 |                                      | Path to OIDC provider config file (YAML or JSON). Enables OIDC authentication when set. See [OIDC Configuration](#oidc-configuration).                                                     |
| `KLANGK_AUTH_MODES`                  | `both` (if OIDC configured)          | Auth modes: `password`, `oidc`, or `both`. Defaults to `password` when no OIDC config.                                                                                                     |
| `KLANGK_SMTP_HOST`                   |                                      | SMTP server hostname (if set, uses SMTP; otherwise uses sendmail)                                                                                                                          |
| `KLANGK_SMTP_PORT`                   | `587`                                | SMTP server port                                                                                                                                                                           |
| `KLANGK_SMTP_USER`                   |                                      | SMTP auth username                                                                                                                                                                         |
| `KLANGK_SMTP_PASSWORD`               |                                      | SMTP auth password                                                                                                                                                                         |
| `KLANGK_SMTP_FROM`                   |                                      | Email sender address (falls back to SMTP_USER, then noreply@localhost)                                                                                                                     |
| `KLANGK_SMTP_USE_TLS`                | `true`                               | Use STARTTLS for SMTP                                                                                                                                                                      |
| `KLANGK_SENDMAIL_PATH`               | `sendmail`                           | Path to sendmail binary (used when KLANGK_SMTP_HOST is not set)                                                                                                                            |
| `KLANGK_LOGIN_BANNER_TITLE`          |                                      | Title shown on the consent banner page (e.g., company name). If empty, no title is displayed.                                                                                              |
| `KLANGK_LOGIN_BANNER`                |                                      | Consent banner text shown before login. Blocks all access until accepted. Supports `file:` prefix. If empty, no banner is shown.                                                           |
| `LOGFIRE_TOKEN`                      |                                      | Pydantic Logfire write token (opt-in)                                                                                                                                                      |
| `LOGFIRE_BASE_URL`                   | `https://logfire-api.pydantic.dev`   | Logfire API base URL (for self-hosted instances)                                                                                                                                           |
| `LOGFIRE_ENVIRONMENT`                |                                      | Logfire environment tag (e.g., `production`, `staging`) — filters traces in the dashboard.                                                                                                 |

### Ports

- `KLANGK_NGINX_PORT` (default `8995`): **Primary access point** — nginx serves UI, API, WebSocket, and proxies hosted app URLs directly to container ports
- `KLANGK_PORT` (default `8997`): Backend (FastAPI/uvicorn)
- `9000+`: User app ports (5 per workspace, mapped to container ports 8000-8004)

### Tailscale and LLM Proxy

If the LLM provider is on a Tailscale host (e.g., a self-hosted Ollama on another machine in the tailnet), `KLANGK_LLM_BASE_URL` **must use the Tailscale IP address**, not a hostname.

The nginx LLM proxy uses lazy DNS resolution (so nginx can start even if the LLM host is temporarily unreachable). This means nginx sends raw DNS queries to the resolvers from `/etc/resolv.conf`. On a Tailscale host, those resolvers include MagicDNS (`100.100.100.100`), but MagicDNS only resolves tailnet names through the system resolver stack — raw UDP DNS queries from nginx don't go through Tailscale's networking, so both bare hostnames (`bizon`) and FQDNs (`bizon.tail33f8f4.ts.net`) fail to resolve.

Meanwhile, `KLANGK_DNS_SERVERS=100.100.100.100,8.8.8.8` is still needed for workspace containers, because podman configures container DNS with search domains that make MagicDNS work correctly inside containers.

```bash
# In .env on a Tailscale host:
KLANGK_LLM_BASE_URL=http://100.122.115.33:11434/v1   # Tailscale IP, not hostname
KLANGK_DNS_SERVERS=100.100.100.100,8.8.8.8            # for containers (works fine)
```

Tailscale IPs are stable and don't change, so using the IP directly is safe.

### Mount Security

Workspace bind mounts are validated at create and edit time. Two protections apply regardless of `KLANGK_ALLOWED_MOUNT_ROOTS`:

**Protected paths** — the following host paths are always blocked, even if they fall under an allowed root:

- `/var/run/docker.sock`, `/run/docker.sock`, `/run/podman/podman.sock` — mounting a container engine socket grants full host control
- `KLANGK_DATA_DIR` (and anything beneath it) — contains every user's workspace home and the database

**Volume isolation** — named volumes (e.g., `nix-store:/nix`) are labelled with `klangk.instance` and `klangk.user-id` at creation time. A workspace cannot mount a volume created by a different `KLANGK_INSTANCE_ID` or a different user. This prevents both cross-tenant and cross-user data access on shared hosts.

## Branch Protection

`main` requires a PR with 4 passing checks before merge:

- `test-backend`
- `test-frontend`
- `test-backend-e2e`
- `test-frontend-e2e`

All 4 run automatically on PRs. You can bypass as repo admin.

## Host Container

The host container is a self-contained deployment image. It packages the backend, nginx proxy, Flutter web UI, and the workspace image into a single Docker image based on `python:3.13-slim`. Workspace containers are launched inside the host container via rootless podman (pasta networking).

The published image is available from GHCR:

```bash
docker pull ghcr.io/mcdonc/klangk/klangk-host:latest
```

### Running

```bash
docker run -d \
  -p 8995:8995 -p 8997:8997 \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  -e KLANGK_DEFAULT_USER=admin@example.com \
  -e KLANGK_DEFAULT_PASSWORD=admin \
  -e KLANGK_JWT_SECRET=change-me \
  ghcr.io/mcdonc/klangk/klangk-host
```

Open http://localhost:8995. On first startup the embedded workspace image is automatically loaded into podman.

The five Docker flags are required for rootless podman to create workspace containers inside the host container. They grant mount capabilities (`SYS_ADMIN`), FUSE filesystem access (`/dev/fuse`), pasta networking (`/dev/net/tun`), and remove default restrictions on syscalls and `/proc` that block nested container creation.

Data is stored in `/home/klangk/data` inside the container. To persist across restarts, mount a volume:

```bash
docker run -d -v klangk-data:/home/klangk/data ...
```

### Building locally

```bash
build-host-image
```

This builds everything from source: Flutter web, workspace image (podman), then the host image (Docker). Tagged with `latest` and a CalVer version (e.g., `2026.06.09-abc1234`). The version is baked into `/home/klangk/version.json` and served at `GET /version`.

### Custom image with plugins

To build a host image with plugins baked in, see [klangk-host-with-plugins](https://github.com/mcdonc/klangk-host-with-plugins) for an example. It clones klangk at a given ref, fetches plugins, rebuilds the Flutter web frontend and workspace image with plugin support, then layers the results on top of the released host image.

### Scanning

```bash
trivy-host                        # full vulnerability scan
trivy-host --severity CRITICAL    # critical only
```

### CI

The `image-host.yml` workflow builds and pushes the host image to GHCR. It is triggered manually via `workflow_dispatch` (building is too expensive for automatic push triggers). The `image-workspace.yml` workflow builds and pushes the workspace image independently on push to `main`.

### Releasing

Push a CalVer tag to trigger the `release.yml` workflow:

```bash
git tag v2026.06.10
git push origin v2026.06.10
```

This builds the host image (including workspace and Flutter web), pushes both `klangk-host` and `klangk-workspace` to GHCR tagged with `latest` and the version, and creates a GitHub Release with auto-generated notes. If you need a second release on the same day, append a suffix: `v2026.06.10.1`.

## Build Architecture (amd64 / arm64)

All workspace image builds (`build-workspace-image`, `build-base-image`) use podman and build for `$KLANGK_PLATFORM`, which `devenv.nix` defaults to the host architecture (`linux/arm64` on Apple Silicon, `linux/amd64` elsewhere). This means images build and run natively instead of under QEMU emulation. The host container (`build-host-image`) still uses Docker. Override per-shell via `.env`:

```bash
KLANGK_PLATFORM=linux/amd64   # force amd64 even on an arm64 host
```

Building the **workspace** image natively requires a **base** image with a matching variant. The base (`ghcr.io/mcdonc/klangk/klangk-workspace-base`) is published as a multi-arch manifest (amd64 + arm64) by `push-base-image`, so `pull-base-image` automatically gets the right variant for the host. If the published base lacks your arch, build it locally first:

```bash
build-base-image            # local single-arch build for $KLANGK_PLATFORM
```

`push-base-image` builds and pushes both arches in one step via `docker buildx` (a multi-arch manifest cannot be loaded into the local daemon). Override the published set with `KLANGK_BASE_PLATFORMS` (default `linux/amd64,linux/arm64`).

## Project Layout

```text
src/
  backend/             # FastAPI app
    tests/             # Backend unit tests
    e2e-tests/         # CLI E2E tests
  frontend/            # Flutter web app
    test/              # Frontend unit tests
    e2e-tests/         # Playwright E2E tests
  containers/
    host/              # Host container (Dockerfile, entrypoint)
    workspace/         # Workspace container (Dockerfile, base, entrypoint)
  bridge/              # @klangk/bridge npm package
plugins/               # Built-in plugins (celebrate, beep, etc.)
scripts/               # Build and utility scripts
devenv.nix             # devenv configuration
```

## All Shell Commands

Inside `devenv shell`, these commands are available:

| Command                 | Description                           |
| ----------------------- | ------------------------------------- |
| `test-backend`          | Run backend unit tests                |
| `test-frontend`         | Run frontend unit tests with coverage |
| `test-cli-e2e`          | Run CLI E2E tests                     |
| `test-frontend-e2e`     | Run Flutter E2E tests (all browsers)  |
| `flutterbuildweb`       | Rebuild Flutter web only              |
| `build-workspace-image` | Rebuild workspace image (podman)      |
| `build-base-image`      | Rebuild workspace base image          |
| `build-host-image`      | Build host container image            |
| `run-host-container`    | Run host container locally            |
| `trivy-host`            | Scan host image for vulnerabilities   |
| `update-plugins`        | Fetch plugins from plugins.yaml       |

## Plugin System

All plugins live in `$KLANGK_PLUGINS_DIR/<name>/` directories (defaults to `.devenv/state/klangk/plugins/`). A plugin can contain:

- `extension.ts` — Pi extension with `pi.registerTool()`. Copied to `src/containers/extensions/` at build time.
- `klangk/` — Optional Dart package for client-side browser actions:
  - `klangk/pubspec.yaml` — Package definition, depends on `klangk_plugin_api` (git)
  - `klangk/lib/plugin.dart` — Class extending `ToolPlugin` with action handlers
  - `klangk/lib/*.dart` — Supporting Dart files (widgets, utilities)
- `tools/` — Server-side scripts. Everything in this subdirectory is copied to `/opt/klangk/plugin-tools/<name>/` in the workspace image.

A plugin needs at minimum an `extension.ts`. The `klangk/` subdirectory is only needed for client-side browser actions (e.g., celebrate, beep, authenticated fetch) that are dispatched via the browser bridge.

### Build integration

- `scripts/import_dart_plugins.py` scans `$KLANGK_PLUGINS_DIR/*/klangk/` for plugin Dart packages and generates `$KLANGK_PLUGINS_DIR/.dart/` (the `klangk_plugins` package with path deps and `createAllPlugins()`)
- `build-workspace-image` stages `extension.ts` and `tools/` files from all plugins into `$KLANGK_PLUGINS_DIR/.docker/` and passes them via named build contexts (`plugin-extensions`, `plugin-tools`)
- `flutterbuildweb` runs the codegen before compiling
- `stub_dart_plugins.sh` creates a minimal stub at `$KLANGK_PLUGINS_DIR/.dart/` so `flutter pub get` works before plugins are fetched (runs automatically at devenv shell startup via `enterShell`; skips if `pubspec_overrides.yaml` already exists)
- Both build tasks are triggered automatically by `devenv up` via `execIfModified`

### Adding a plugin

For local development, create files directly in `$KLANGK_PLUGINS_DIR`:

1. Create `$KLANGK_PLUGINS_DIR/<name>/extension.ts` with `pi.registerTool()`
2. For client-side browser actions, add `klangk/pubspec.yaml` (depends on `klangk_plugin_api`) and `klangk/lib/plugin.dart` extending `ToolPlugin`
3. For server-side scripts, add files in `$KLANGK_PLUGINS_DIR/<name>/tools/`
4. `devenv up` rebuilds automatically when `$KLANGK_PLUGINS_DIR` changes

For remote plugins, add an entry to `$KLANGK_PLUGINS_DIR/plugins.yaml` and run `update-plugins` to fetch it.

### Plugin management

Run `update-plugins` to fetch plugins. On first run it creates a `plugins.yaml` template with the default plugins. Plugins are declared in `$KLANGK_PLUGINS_DIR/plugins.yaml`. Each entry requires `name` and `git`; `path` and `ref` are optional:

```yaml
plugins:
  - name: celebrate
    git: git@github.com:mcdonc/klangk.git
    path: plugins/celebrate
    ref: main
  - name: beep
    git: git@github.com:mcdonc/klangk.git
    path: plugins/beep
    ref: main
```

- `update-plugins` — fetches all plugins listed in `plugins.yaml`, resolves git refs to commit SHAs, writes `plugins.lock`
- `update-plugins <name>` — fetch/update a single plugin by name
- `plugins.lock` — records resolved commit SHAs for reproducible builds
- Local plugin development: drop a directory into `$KLANGK_PLUGINS_DIR` directly — the build system treats it the same as a fetched plugin
- `execIfModified` watches `$KLANGK_PLUGINS_DIR` to trigger rebuilds when plugin content or the lockfile changes

## Data

- All data stored in `$KLANGK_DATA_DIR` (defaults to `$DEVENV_STATE/klangk/data`)
- SQLite database: `klangk.db` (users, workspaces, port allocations, token blocklist, login attempts)
- Workspace files: `workspaces/<user-id>/home/<workspace-id>/work/` (inside the `/home/klangk` bind mount)
- Persistent home: `workspaces/<user-id>/home/<workspace-id>/` (mounted as `/home/klangk` — dotfiles, bash history, Pi sessions)
- Database persists across restarts and rebuilds

## OIDC Configuration

Klangk supports OIDC authentication via one or more external Identity Providers (e.g., Keycloak). This is used for CAC card login and other SSO scenarios. Klangk is a standard OIDC relying party — the IdP handles all certificate/credential complexity.

### Setup

1. Create a YAML config file with your OIDC providers:

```yaml
- id: cac
  display_name: CAC Login
  issuer: https://keycloak.example.com/realms/company
  client_id: klangk
  client_secret: "file:/run/secrets/cac-secret"
  scopes: openid email profile
  ca_cert: /etc/pki/tls/certs/company-ca-bundle.pem
  logout_redirect: true

- id: internal
  display_name: Internal SSO
  issuer: https://keycloak.example.com/realms/corp
  client_id: klangk
  client_secret: "file:/run/secrets/corp-secret"
```

1. Set `KLANGK_OIDC_CONFIG` in `.env`:

```bash
KLANGK_OIDC_CONFIG=/path/to/oidc.yaml
```

1. Optionally set `KLANGK_AUTH_MODES` to control which login methods are available:
   - `both` (default when OIDC configured) — SSO buttons + email/password form
   - `oidc` — SSO buttons only, email/password disabled
   - `password` — email/password only (same as no OIDC config)

### Provider Config Fields

| Field                  | Required | Description                                                                                                                 |
| ---------------------- | -------- | --------------------------------------------------------------------------------------------------------------------------- |
| `id`                   | Yes      | URL-safe slug, used in endpoint paths (`/auth/oidc/{id}/login`) and stored as `provider` on users                           |
| `display_name`         | Yes      | Button label on the login page (e.g., "CAC Login", "Google")                                                                |
| `issuer`               | Yes      | OIDC issuer URL. Discovery via `{issuer}/.well-known/openid-configuration`                                                  |
| `client_id`            | Yes      | OIDC client ID registered with the IdP                                                                                      |
| `client_secret`        | Yes      | OIDC client secret. Supports `file:` prefix for secret management                                                           |
| `scopes`               | No       | Space-separated scopes (default: `openid email profile`)                                                                    |
| `ca_cert`              | No       | Path to a CA certificate PEM file for IdPs with custom/private CAs                                                          |
| `token_validation_pem` | No       | Inline RSA/EC public key PEM for static token validation (skips JWKS discovery)                                             |
| `logout_redirect`      | No       | If `true`, logout redirects to the IdP's `end_session_endpoint` (RP-Initiated Logout). Default: `false` (local-only logout) |

### How It Works

- **Web**: Login page shows one button per provider. Clicking redirects to the IdP via Authorization Code flow with PKCE. After authentication, the IdP redirects back to Klangk which exchanges the code for tokens, validates the ID token, and issues a Klangk JWT.
- **CLI**: `klangk login` detects OIDC from the server config, opens a browser for authentication, and receives the token via a temporary localhost callback server.
- **Login hook**: A single Python hook (`KLANGK_OIDC_LOGIN_HOOK`) handles both login validation and group mapping. See [OIDC Login Hook](#oidc-login-hook) below.
- **User provisioning**: On first OIDC login, a user is created automatically (verified, no password). If a local user with the same email already exists, the OIDC identity is linked to it.
- **OIDC users** cannot use forgot-password, change-password, or change-email.
- **Logout**: By default, logout only kills the Klangk session. With `logout_redirect: true`, the user is also redirected to the IdP's logout endpoint to end the SSO session (requires full re-authentication on next login).

### IdP Setup (Keycloak Example)

1. Create a client in your Keycloak realm:
   - Client ID: `klangk`
   - Client authentication: On (confidential)
   - Valid redirect URIs: `https://your-klangk-host/auth/oidc/cac/callback` (one per provider ID)
   - Web origins: `https://your-klangk-host`
2. Copy the client secret to a file or set it directly in the OIDC config
3. For CAC: configure the X.509 client certificate authenticator in the Keycloak authentication flow

### OIDC Login Hook

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
- **Raise** an exception → login rejected (HTTP 403, exception message shown)
- **Return** `None` → login allowed, no group sync
- **Return** a `set[str]` → login allowed, memberships synced to those groups
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

## Authorization (ACL System)

Klangk uses an Access Control List (ACL) system to manage permissions. Instead of simple admin/non-admin roles, permissions are defined as ACL entries (ACEs) attached to resources in a tree hierarchy. This allows fine-grained control over who can do what, without code changes.

### Core Concepts

- **Resources**: paths in a tree that mirror the URL structure (`/`, `/workspaces`, `/workspaces/{id}`, `/admin`, `/admin/users`, etc.)
- **Principals**: who the ACE applies to — a specific user, a group, or a system principal (`Everyone` or `Authenticated`)
- **Permissions**: what action is allowed or denied (e.g., `view`, `create`, `edit`, `delete`, `terminal`, `files`, `chat`, `share`, `*`)
- **ACEs**: `(Allow/Deny, principal, permission)` entries ordered by position on a resource
- **ACL walk**: when checking permission, the system walks from the target resource up to `/`, checking each node's ACEs in order. First match wins. If no match after reaching root, access is denied.

### Resource Tree

```text
/                              (root)
├── /workspaces                (workspace collection)
│   └── /workspaces/{id}       (specific workspace)
├── /admin
│   ├── /admin/users
│   ├── /admin/invitations
│   └── /admin/groups
└── /auth                      (public — no ACL checks)
```

### Default ACEs (seeded on first startup)

| Resource      | Action | Principal     | Permission |
| ------------- | ------ | ------------- | ---------- |
| `/`           | Allow  | Authenticated | `view`     |
| `/`           | Deny   | Everyone      | `*`        |
| `/workspaces` | Allow  | Authenticated | `create`   |
| `/admin`      | Allow  | group:admin   | `*`        |
| `/admin`      | Deny   | Everyone      | `*`        |

These defaults mean: any logged-in user can view pages and create workspaces; only members of the `admin` group can access admin functions; unauthenticated users are denied everything.

### Groups

Groups replace the old role system. A group is a named collection of users. The built-in `admin` group is created automatically on first startup and the default admin user is added to it.

**Admin UI**: Admin > Groups tab — create/delete groups, add/remove members.

**API endpoints**:

- `GET /admin/groups` — list all groups
- `POST /admin/groups` — create group `{"name": "...", "description": "..."}`
- `DELETE /admin/groups/{id}` — delete group (cascades: removes all ACEs referencing it)
- `POST /admin/groups/{id}/members` — add user `{"user_id": "..."}`
- `DELETE /admin/groups/{id}/members/{user_id}` — remove user

### Workspace Permissions

When a workspace is created, the owner gets a `(Allow, user:{id}, *)` ACE on `/workspaces/{id}`. This grants full access: view, edit, delete, share, terminal, files, chat.

**Sharing**: the owner can share a workspace with users or groups. The simple sharing UI (Sharing tab) grants `view`, `terminal`, `files`, and `chat`. For finer control, the Advanced ACL editor lets you add/remove/reorder individual ACEs.

**Tab visibility**: workspace tabs (Terminal, Files, Chat, Sharing, Settings) are gated by per-resource permissions. A shared user without `chat` permission won't see the Chat tab.

**Permissions checked on workspace resources**:

| Permission | Controls                                                          |
| ---------- | ----------------------------------------------------------------- |
| `view`     | Can see the workspace exists                                      |
| `terminal` | Can open a terminal / exec commands                               |
| `files`    | Can browse/upload/download files                                  |
| `chat`     | Can see the Chat tab                                              |
| `edit`     | Can change workspace settings (name, image, command, mounts, env) |
| `share`    | Can manage who has access (Sharing tab)                           |
| `delete`   | Can delete the workspace                                          |
| `*`        | All of the above                                                  |

### Checking Your Permissions

**Web UI**: the UI automatically shows/hides elements based on your permissions (admin button, workspace tabs, create button, etc.).

**API**: `GET /api/my-permissions` returns your effective permissions on all static resources. Add `?resource=/workspaces/{id}` to check a specific resource.

**CLI**: `klangk list --shared` shows workspaces shared with you.

### Troubleshooting: "Why can't I access this workspace?"

1. **Check your permissions**: `GET /api/my-permissions?resource=/workspaces/{id}` — does it include the permission you need?
2. **Check the workspace ACL**: in the Sharing tab, expand "Advanced: Access Control" to see the ACE list.
3. **Check group membership**: are you in the right group? Admin > Groups tab shows group members.
4. **Check the ACL walk**: permissions are inherited from parent resources. An ACE on `/` applies to everything below it unless overridden. A `Deny` ACE at a higher level blocks access even if a lower-level `Allow` exists, if the `Deny` has a lower position number.
5. **Position matters**: ACEs are checked in position order (lowest first). If a `Deny` on position 0 matches before an `Allow` on position 1, access is denied. Use the ACL editor to reorder entries.

## CLI Access

Klangk provides a CLI for terminal-based access to the same containers:

```bash
klangk login admin@example.com        # authenticate (prompts for password)
klangk list                             # list workspaces
klangk create my-project                # create a workspace
klangk create my-project --mount ~/src:/home/klangk/work/src          # with bind mount
klangk create my-project --mount nix-store:/nix           # with named volume
klangk create my-project --env KLANGK_SKILLS=stats,rdkit    # with env vars
klangk edit my-project                  # interactive edit (name, image, command, mounts, env)
klangk edit my-project --env FOO=bar    # set env var via flag
klangk dup my-project my-copy           # duplicate a workspace
klangk shell my-project                 # drop into bash inside the container
klangk exec my-project ls /home/klangk/work         # run a command in the container
klangk sync ~/src my-project:/home/klangk/work      # sync files to/from the container
klangk rm my-project                # delete a workspace
klangk export my-project            # export workspace to my-project.tar.gz (admin only)
klangk export my-project -o bak.tar.gz  # export to specific file
klangk import bak.tar.gz            # import workspace from archive
klangk import bak.tar.gz --name new-name  # import with a different name
klangk volumes ls                   # list your podman volumes
klangk volumes create nix-store     # create a named volume (owned by you)
klangk volumes rm nix-store         # delete a volume (must be yours)
```

The CLI connects to the running Klangk backend over HTTP + WebSocket — it works locally and against remote servers.

## Workspace Export/Import

Workspaces can be exported as `.tar.gz` archives and imported to create new workspaces. The archive contains:

- `workspace.json` — metadata (name, image, default command, mounts, env vars, num_ports)
- `home/` — the workspace's home directory tree (files, dotfiles, virtualenvs, Pi sessions, bash history)

**Export** (admin only): `klangk export <workspace>` downloads the archive via `GET /workspaces/{id}/export`. The tarball is built on the server using a temp file to avoid memory pressure on large workspaces.

**Import**: `klangk import <archive>` uploads the archive via `POST /workspaces/import`. The server streams the upload to a temp file, extracts metadata, creates the workspace, and extracts the home directory. Invalid images or mounts from the archive are silently dropped. Use `--name` to override the workspace name from the archive.

System-level packages (apt installs, etc.) are not included — those belong in custom workspace images.

## Pre-commit Hooks

Pre-commit hooks run automatically on `git commit` via [git-hooks.nix](https://github.com/cachix/git-hooks.nix):

- **ruff check --fix** — Python linting with auto-fix
- **ruff format** — Python formatting
- **dart format** — Dart formatting
- **nixfmt** — Nix formatting
- **prettier** — TypeScript, JavaScript, and YAML formatting
- **yamllint** — YAML linting

Hooks are installed automatically when entering the devenv shell.
