# Customizing a Deployment

Klangk is deployed as a Docker container (the "[host container](https://github.com/mcdonc/klangk/pkgs/container/klangk%2Fklangk-host)") that packages the backend, nginx, Flutter web UI, and workspace image into a single image. The [`customize/`](https://github.com/mcdonc/klangk/tree/main/customize) directory in the Klangk repo is an example of how to build a custom version of this deployment container with plugins, custom CA certificates, and OIDC login hooks baked in. It is meant to be copied out of the repo and adapted to your own needs — edit the scripts, swap out the plugins, add your own login hooks, replace the placeholder logo, etc. Nothing in `customize/` is required to run Klangk; it's a starting point for organizations that want a tailored deployment.

## Product Name

The simplest white-labeling is renaming the product. Set `KLANGK_PRODUCT_NAME` and the browser tab title, the app-bar logo wordmark, and all outgoing emails (registration verification, password reset, invitation) use the configured name — with **no image rebuild required**. Defaults to `Klangk`. Like other `KLANGK_*` vars it supports the `file:`/`cmd:` prefix.

```bash
docker run -d \
  -e KLANGK_PRODUCT_NAME="Acme Labs" \
  ...
```

The value is published to the frontend through the `GET /api/v1/config` endpoint (`product_name` field) and interpolated into email subjects and bodies server-side. The frontend reads it at startup (via the `/config` fetch), so the tab title and logo update without a rebuild.

> The logo _icon_ (the robot glyph) is a separate, build-time customization — see the `logo.png` placeholder below. `KLANGK_PRODUCT_NAME` only controls the textual wordmark.

## What Gets Customized

This example custom build layers your changes on top of the standard Klangk host image:

- **Plugins** — TypeScript extensions and Dart UI plugins are compiled into the workspace image and Flutter web frontend
- **CA certificates** — Private CA certs are installed into the system trust store of both the host and workspace images
- **Login hooks** — Custom Python hooks for OIDC login validation (e.g., require invitation)
- **Logo** — A placeholder `logo.png` demonstrates branding customization

## Runtime logo override (no rebuild)

The build-time `customize/logo.png` path requires rebuilding the host image.
For a no-rebuild rebrand, klangk also overrides the logo at runtime via the
`KLANGK_LOGO_URL` env var:

- **Set `KLANGK_LOGO_URL`** to an absolute image URL (e.g.
  `https://example.com/logo.png`). The value is published to the UI through the
  unauthenticated `/api/v1/config` endpoint (`logo_url`), so it renders on the
  login page before login. It supports `file:`/`cmd:` secret resolution like
  other env vars (e.g. `file:/run/secrets/logo_url` whose contents is the URL).
- **Serve a local file without a CDN:** drop your logo into
  `$KLANGK_DATA_DIR/branding/` (created at startup) and set
  `KLANGK_LOGO_URL=/branding/logo.png`. klangk serves that directory at
  `/branding/` — no rebuild, no external host.
- When unset — or if the image fails to load — the default `KlangkLogo` widget
  is rendered, so existing deployments are unchanged.

This overrides only the **image** in the UI. It also flows into email headers
when emails are rendered through the templating system (see below).

## Email templating (`KLANGK_EMAIL_TEMPLATES_DIR`)

Outgoing auth emails — registration verification, password reset, and
invitation — are rendered from Jinja2 templates shipped inside the backend
package at `klangk_backend/email_templates/`. There is no separate hardcoded
fallback: the built-in templates _are_ the defaults. Each email is three
files under a per-event folder (`verify/`, `reset/`, `invite/`): a
`subject.txt`, a plain-text `body.txt`, and an HTML `body.html` that
`{% extends "base.html" %}`. Editing `base.html` alone re-brands every
email at once.

### Customizing emails

Point `KLANGK_EMAIL_TEMPLATES_DIR` at a directory of your own templates.
Two equivalent approaches (they produce identical output):

- **Copy the whole `email_templates/` tree, then edit what you want.** This
  is the usual path. The built-in templates live at
  `src/backend/klangk_backend/email_templates/` in the Klangk source
  checkout; copy that directory out, delete the copied `__init__.py`
  (packaging only), edit, and point the env var at it. By copying the whole
  tree you **own it**: on upgrade, re-diff against the current built-ins and
  bring forward whatever you want — normal maintenance for a forked config.
- **Drop only the files you change.** Anything absent from your directory
  falls through to the built-ins, so unchanged files keep inheriting upstream
  fixes automatically. Use this for a few surgical changes when you don't
  want to maintain a full copy.

Overrides resolve per-file: a deployer file shadows the built-in of the same
path, and `{% extends %}`/`{% include %}` resolve your overrides first. So
you can re-brand all emails by overriding just `base.html`, change a single
subject by overriding just `invite/subject.txt`, or wholesale replace one
email by overriding its three files.

> **Keep the `.html` extension** on HTML templates (not `.html.j2`). klangk
> enables Jinja autoescaping by filename, and a `.j2` suffix silently disables
> it — the worst place to lose escaping is the shared `base.html`.

### Variables

**Global** (every email): `product_name` (`KLANGK_PRODUCT_NAME`), `logo_url`
(`KLANGK_LOGO_URL` — when set, the email header shows your logo instead of the
default badge), `brand_color` (`KLANGK_BRAND_COLOR`, default `#E65100`).

**Per-email**: `link` (the verification / reset / invite URL), `expiry_hours`
(the real token TTL — interpolated, not hardcoded, so it always matches your
`KLANGK_INVITE_EXPIRE_HOURS` / token settings), and `invited_by` (invitation
only).

> **Tokens never appear in the subject line** — subjects receive only the
> global branding variables, never the link, so tokens can't leak into
> mail-server subject logs.

### Other email knobs

- **`KLANGK_SMTP_REPLY_TO`** — when set, every outgoing message carries a
  `Reply-To` header pointing at a monitored address (compliance /
  deliverability). Unset → no header.
- **Footer / legal line** — the base template exposes an empty
  `{% block legal %}` you can fill in your override to add a compliance
  footer in one place.

### Example: rebrand + monitored reply address

```bash
docker run -d \
  -e KLANGK_PRODUCT_NAME="Acme Labs" \
  -e KLANGK_LOGO_URL="/branding/logo.png" \
  -e KLANGK_SMTP_REPLY_TO="support@acme.example.com" \
  -e KLANGK_EMAIL_TEMPLATES_DIR=/etc/klangk/email-templates \
  -v /etc/klangk/email-templates:/etc/klangk/email-templates:ro \
  ...
```

## Prerequisites

- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/)
- Docker
- SSH key with access to the git repos listed in `plugins.yaml`

## Directory Layout

```text
customize/
  build.sh            # Main build script (run this)
  build-inner.sh      # Devenv-side build (called by build.sh)
  Dockerfile          # Custom image layer on top of klangk-host
  docker-compose.yml  # Example runtime configuration
  plugins.yaml        # Plugin list for the build
  login_hook.py       # Example OIDC login hook
  logo.png            # Placeholder logo (replace with your own)
  ssl/                # Place .pem/.crt CA certs here (gitignored)
  data/               # Persistent data directory (gitignored)
```

## Build

```bash
cd customize
./build.sh

# Or pin to a specific Klangk host container release:
KLANGK_REF=v2026.06.10-1 ./build.sh
```

The resulting image is tagged `ghcr.io/mcdonc/klangk/klangk-host-custom:latest` by default. Override with `KLANGK_HOST_IMAGE`.

### How the Build Works

The build is split into two scripts: `build.sh` handles orchestration (git, Docker), and `build-inner.sh` runs inside the devenv shell where Flutter, podman, and Python are available.

**`build.sh`** (outer script):

1. Clones (or updates) the Klangk repo at `KLANGK_REF` into `.klangk/`
1. Copies `plugins.yaml` into a staging directory (`.plugins/`)
1. Enters the devenv shell and runs `build-inner.sh`
1. Builds the standard `klangk-host` image from source via `scripts/build-host-image.sh`
1. Copies the rebuilt Flutter web output into the Docker build context
1. Runs `docker build` with the `Dockerfile`, which layers custom CA certs, the login hook, the rebuilt web frontend, and the rebuilt workspace image tarball on top of `klangk-host:latest`
1. Cleans up temporary build artifacts

**`build-inner.sh`** (runs inside devenv shell):

1. Fetches plugins listed in `plugins.yaml` via `update_plugins.py`
1. Rebuilds the Flutter web frontend with Dart plugin UI via `flutterbuildweb.sh`
1. Rebuilds the workspace container image with plugin extensions and tools via `build-workspace-image.sh`
1. If custom CA certs are present in `ssl/`, builds a temporary Dockerfile that layers them onto the workspace image (installs into `/usr/local/share/ca-certificates/` and runs `update-ca-certificates`)
1. Exports the final workspace image as `workspace.tar` via `podman save`

**`Dockerfile`** (custom image layer):

Extends `klangk-host:latest` and replaces four things:

- Installs CA certs from `ssl/` into the host's system trust store
- Copies `login_hook.py` into the backend's Python path
- Replaces the Flutter web build with the plugin-enabled version
- Replaces the workspace image tarball with the plugin-enabled version

## Build Options

| Variable              | Default                                    | Description                                                                                                                                                                                                                          |
| --------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `KLANGK_REF`          | `main`                                     | Klangk branch, tag, or commit SHA to build against                                                                                                                                                                                   |
| `KLANGK_REPO`         | `https://github.com/mcdonc/klangk.git`     | Klangk repo URL                                                                                                                                                                                                                      |
| `KLANGK_HOST_IMAGE`   | `ghcr.io/mcdonc/klangk/klangk-host-custom` | Output image name                                                                                                                                                                                                                    |
| `KLANGK_PLATFORM`     | `linux/amd64`                              | Target platform                                                                                                                                                                                                                      |
| `KLANGK_SSL_CERT_DIR` | `./ssl`                                    | Directory containing `.pem`/`.crt` CA certs to inject into both images (build-time). The same variable is also honored **at runtime** to trust private CAs without a rebuild — see [Custom CA Certificates](#custom-ca-certificates) |

## Plugins

Edit `plugins.yaml` to add or remove plugins. The default set includes the built-in plugins: celebrate, beep, pig-latin, word-count, browser-fetch, bobdobbs.

To add an external plugin:

```yaml
plugins:
  - name: my-plugin
    git: https://github.com/myorg/my-klangk-plugin.git
    ref: main
```

See the [Creating Plugins](../development/creating-plugins.md) reference for plugin structure details.

## Custom CA Certificates

Klangk supports custom CA certificates in two complementary ways: a **build-time** path that bakes the certs into the images (requires a rebuild to rotate), and a **runtime** path that mounts the certs and points each toolchain at them (rotate by restarting containers, no rebuild).

Both honor the same directory: place `.pem` or `.crt` files there. This is needed when services (e.g., Logfire, OIDC providers, internal package mirrors, SMTP relays) use certificates signed by a private CA — common behind corporate MITM proxies or in air-gapped/offline deployments.

### Build-time (baked into the images)

Place `.pem` or `.crt` files in the `ssl/` directory (or set `KLANGK_SSL_CERT_DIR`). They are installed into the system CA store of both the host and workspace images. The `ssl/` directory is gitignored — certs must be provided at build time.

### Runtime (mounted, no rebuild) — `KLANGK_SSL_CERT_DIR`

Point `KLANGK_SSL_CERT_DIR` at a directory of `.pem`/`.crt` files on the host and **restart** workspaces (or the backend). Klangk makes those CAs trusted at startup **without rebuilding any image**, in **two scopes**:

- **Workspace containers** — the directory is bind-mounted read-only into each container, and the container entrypoint builds a merged CA bundle (the container's system CAs **plus** your custom certs) on the writable `/tmp` tmpfs. The toolchain trust env vars (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`) are set to point at it, so OpenSSL, Python (`requests`/`certifi`), `curl`, and Node all honor your CAs. Because the bundle is **merged with the system CAs**, public-internet TLS (e.g. `npm install`, `pip install`, `git clone https://...`) keeps working. This applies to shells, `podman exec`, and the agent subprocess alike.
- **Backend process** — at startup the backend concatenates your certs with its own system bundle and sets the same trust env vars, so its own outbound TLS (OIDC discovery, SMTP relay, the LLM-proxy upstream) trusts your private CAs too.

```bash
docker run -d \
  ...
  -e KLANGK_SSL_CERT_DIR=/certs \
  -v ./my-corporate-cas:/certs:ro \
  ghcr.io/mcdonc/klangk/klangk-host-custom
```

Rotating a cert is just a file change plus a workspace/backend restart — no image rebuild. The build-time path remains available; the runtime path is additive.

> **Why a merged bundle?** The `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` variables _replace_ the default trust store rather than add to it, so a custom-only bundle would break public-internet TLS. Klangk therefore prepends the system CAs before your custom certs. (`NODE_EXTRA_CA_CERTS` is additive, but pointing it at the same merged bundle is harmless.)

## OIDC Authentication

To enable OIDC login, create an `oidc.yaml` in the `customize/` directory (gitignored), then mount it at runtime:

```bash
docker run -d \
  ...
  -v ./oidc.yaml:/home/klangk/oidc.yaml:ro \
  -e KLANGK_OIDC_CONFIG=/home/klangk/oidc.yaml \
  ghcr.io/mcdonc/klangk/klangk-host-custom
```

If your OIDC provider requires a custom CA certificate (e.g. `ca_cert: cacert.pem` in `oidc.yaml`), place the PEM file at `cacert.pem` in the `customize/` directory (gitignored). The `docker-compose.yml` mounts it into the container at `/home/klangk/cacert.pem`.

See the [OIDC documentation](../reference/oidc.md) for the config file format.

## OIDC Login Hook

The `customize/` directory includes a sample login hook (`login_hook.py`) that restricts OIDC logins to invited users. Set the environment variable to activate it:

```bash
KLANGK_OIDC_LOGIN_HOOK=login_hook.require_invitation
```

The hook works because the Dockerfile copies `login_hook.py` into the backend's Python path (`/home/klangk/src/backend/`), making it importable by the OIDC login system.

The example hook's logic:

- If the user's most recent invitation is **revoked**, login is **blocked** (even if they have an existing account)
- If the user's most recent invitation is **pending** or **accepted**, login is **allowed**
- If the user has **no invitation** but has an **existing account**, login is **allowed**
- If the user has **no invitation and no account**, login is **blocked**

Re-inviting someone after a revocation creates a new pending invitation that overrides the revocation.

## Running

```bash
docker run -d \
  -p 8995:8995 \
  -v ./data:/home/klangk/data \
  -v ./mount:/home/klangk/mount \
  -v ./oidc.yaml:/home/klangk/oidc.yaml:ro \
  -v ./cacert.pem:/home/klangk/cacert.pem:ro \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  -e KLANGK_NGINX_PORT=8995 \
  -e KLANGK_PORT=8997 \
  -e KLANGK_DEFAULT_USER=admin@example.com \
  -e KLANGK_DEFAULT_PASSWORD=changeme \
  -e KLANGK_JWT_SECRET=change-this-to-a-random-secret \
  -e KLANGK_PREVENT_INSECURE_JWT_SECRET=1 \
  -e KLANGK_DATA_DIR=/home/klangk/data \
  -e KLANGK_LLM_BASE_URL=https://ollama.com/v1 \
  -e KLANGK_LLM_API_KEY=your-api-key \
  -e KLANGK_LLM_MODEL=gemma4:31b \
  -e KLANGK_INSTANCE_ID=default \
  -e KLANGK_OIDC_CONFIG=/home/klangk/oidc.yaml \
  -e KLANGK_AUTH_MODES=both \
  -e KLANGK_OIDC_LOGIN_HOOK=login_hook.require_invitation \
  -e KLANGK_DISABLE_REGISTRATION=1 \
  -e KLANGK_DNS_SERVERS=100.100.100.100,8.8.8.8 \
  -e KLANGK_ALLOWED_MOUNT_ROOTS=/home/klangk/mount \
  -e KLANGK_SMTP_HOST=smtp.example.com \
  -e KLANGK_SMTP_USER=you@example.com \
  -e KLANGK_SMTP_PASSWORD=your-smtp-password \
  -e KLANGK_SMTP_FROM=noreply@example.com \
  -e LOGFIRE_BASE_URL=https://logfire-api.pydantic.dev \
  -e LOGFIRE_TOKEN=your-logfire-token \
  -e LOGFIRE_ENVIRONMENT=production \
  ghcr.io/mcdonc/klangk/klangk-host-custom
```

Or use `docker-compose.yml` with a `.env` file:

```yaml
services:
  klangk:
    image: ghcr.io/mcdonc/klangk/klangk-host-custom
    ports:
      - "8995:8995"
    volumes:
      - ./data:/home/klangk/data
      - ./mount:/home/klangk/mount
      - ./oidc.yaml:/home/klangk/oidc.yaml:ro
      - ./cacert.pem:/home/klangk/cacert.pem:ro
    cap_add:
      - SYS_ADMIN
    devices:
      - /dev/fuse
      - /dev/net/tun
    security_opt:
      - seccomp=unconfined
      - systempaths=unconfined
    environment:
      KLANGK_NGINX_PORT: "8995"
      KLANGK_PORT: "8997"
      KLANGK_DEFAULT_USER: admin@example.com
      KLANGK_DEFAULT_PASSWORD: changeme
      KLANGK_JWT_SECRET: change-this-to-a-random-secret
      KLANGK_PREVENT_INSECURE_JWT_SECRET: "1"
      KLANGK_DATA_DIR: /home/klangk/data
      KLANGK_LLM_BASE_URL: https://ollama.com/v1
      KLANGK_LLM_API_KEY: your-api-key
      KLANGK_LLM_MODEL: gemma4:31b
      KLANGK_INSTANCE_ID: default
      KLANGK_OIDC_CONFIG: /home/klangk/oidc.yaml
      KLANGK_AUTH_MODES: both
      KLANGK_OIDC_LOGIN_HOOK: login_hook.require_invitation
      KLANGK_DISABLE_REGISTRATION: "1"
      KLANGK_DNS_SERVERS: 100.100.100.100,8.8.8.8
      KLANGK_ALLOWED_MOUNT_ROOTS: /home/klangk/mount
      KLANGK_SMTP_HOST: smtp.example.com
      KLANGK_SMTP_USER: you@example.com
      KLANGK_SMTP_PASSWORD: your-smtp-password
      KLANGK_SMTP_FROM: noreply@example.com
      LOGFIRE_BASE_URL: https://logfire-api.pydantic.dev
      LOGFIRE_TOKEN: your-logfire-token
      LOGFIRE_ENVIRONMENT: production
```
