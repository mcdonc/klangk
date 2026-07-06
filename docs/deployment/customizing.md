# Customizing a Deployment

Most Klangk customization is done at **runtime** via environment variables and bind mounts — no image rebuild required. The stock [`klangk-host`](https://github.com/mcdonc/klangk/pkgs/container/klangk%2Fklangk-host) image supports branding, legal links, email templates, CA certificates, and OIDC login hooks out of the box.

**The only reason to build a custom host image is plugins** (Dart UI plugins require a Flutter web rebuild; TypeScript workspace plugins require a workspace image rebuild). See [Building a Custom Image (Plugins)](#building-a-custom-image-plugins) below.

The [`customize/`](https://github.com/mcdonc/klangk/tree/main/customize) directory in the Klangk repo provides a working example: a `docker-compose.yml` showcasing runtime configuration, and build scripts for the plugins-only custom image path. Copy it and adapt to your needs.

## Runtime Customization

### Product Name

Set `KLANGK_PRODUCT_NAME` to rename the product across the browser tab title, the app-bar logo wordmark, and all outgoing emails. Defaults to `Klangk`. Supports the `file:`/`cmd:` prefix.

```bash
docker run -d \
  -e KLANGK_PRODUCT_NAME="Acme Labs" \
  ...
```

The value is published to the frontend through `GET /api/v1/config` (`product_name` field) and interpolated into email subjects and bodies server-side.

### Logo

Set `KLANGK_LOGO_URL` to an absolute image URL. The value is published to the UI through the unauthenticated `/api/v1/config` endpoint (`logo_url`), so it renders on the login page before login. Supports `file:`/`cmd:` secret resolution.

To serve a local file without a CDN, drop your logo into `$KLANGK_DATA_DIR/branding/` (created at startup) and set `KLANGK_LOGO_URL=/branding/logo.png`. Klangk serves that directory at `/branding/`.

When unset (or if the image fails to load), the default `KlangkLogo` widget is rendered. The logo also flows into email headers when emails are rendered through the templating system.

### Legal & Support Links

These env vars add links to the login/registration screens and email footers. All are plain values (no `file:`/`cmd:` resolution — they are public, shown pre-auth). Empty hides them.

| Variable               | Description                                                           |
| ---------------------- | --------------------------------------------------------------------- |
| `KLANGK_TERMS_URL`     | Terms of Service link                                                 |
| `KLANGK_PRIVACY_URL`   | Privacy Policy link                                                   |
| `KLANGK_AUP_URL`       | Acceptable Use Policy link                                            |
| `KLANGK_SUPPORT_URL`   | Support/help link (app bar + auth screens)                            |
| `KLANGK_SUPPORT_EMAIL` | Support email (`mailto:` fallback when `KLANGK_SUPPORT_URL` is unset) |

### Email Templating

Outgoing auth emails (registration verification, password reset, invitation) are rendered from Jinja2 templates. Customize them by pointing `KLANGK_EMAIL_TEMPLATES_DIR` at a directory of your own templates, bind-mounted into the container.

Two approaches:

- **Copy the whole tree, then edit.** The built-in templates live at `src/backend/klangk_backend/email_templates/` in the source. Copy, delete `__init__.py`, edit, and mount.
- **Drop only the files you change.** Absent files fall through to the built-ins.

Overrides resolve per-file: a deployer file shadows the built-in at the same path, and `{% extends %}`/`{% include %}` resolve your overrides first. Override just `base.html` to re-brand all emails at once.

> **Keep the `.html` extension** on HTML templates (not `.html.j2`). Klangk enables Jinja autoescaping by filename.

#### Template Variables

**Global** (every email): `product_name` (`KLANGK_PRODUCT_NAME`), `logo_url` (`KLANGK_LOGO_URL`), `brand_color` (`KLANGK_BRAND_COLOR`, default `#E65100`).

**Per-email**: `link` (the verification/reset/invite URL), `expiry_hours` (real token TTL), and `invited_by` (invitation only).

> **Tokens never appear in the subject line** — subjects receive only the global branding variables, never the link.

#### Other Email Knobs

- **`KLANGK_SMTP_REPLY_TO`** — adds a `Reply-To` header to every outgoing message. Unset means no header.
- **Footer / legal line** — `base.html` exposes an empty `{% block legal %}` for a compliance footer.

#### Example

```bash
docker run -d \
  -e KLANGK_PRODUCT_NAME="Acme Labs" \
  -e KLANGK_LOGO_URL="/branding/logo.png" \
  -e KLANGK_SMTP_REPLY_TO="support@acme.example.com" \
  -e KLANGK_EMAIL_TEMPLATES_DIR=/etc/klangk/email-templates \
  -v /etc/klangk/email-templates:/etc/klangk/email-templates:ro \
  ...
```

### Custom CA Certificates

Point `KLANGK_SSL_CERT_DIR` at a directory of `.pem`/`.crt` files on the host and **restart** workspaces (or the backend). Klangk makes those CAs trusted at startup without rebuilding any image:

- **Workspace containers** — the directory is bind-mounted read-only into each container, and the entrypoint builds a merged CA bundle (system CAs plus your custom certs) on the writable `/tmp` tmpfs. The toolchain trust env vars (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`) are set to point at it, so OpenSSL, Python, `curl`, and Node all honor your CAs. The bundle is merged with system CAs so public-internet TLS keeps working.
- **Backend process** — at startup the backend concatenates your certs with its own system bundle and sets the same trust env vars, so outbound TLS (OIDC discovery, SMTP relay, LLM proxy) trusts your private CAs too.

```bash
docker run -d \
  -e KLANGK_SSL_CERT_DIR=/certs \
  -v ./my-corporate-cas:/certs:ro \
  ...
```

Rotating a cert is just a file change plus a workspace/backend restart — no image rebuild.

> **Why a merged bundle?** `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` _replace_ the default trust store rather than add to it. Klangk therefore prepends the system CAs before your custom certs. (`NODE_EXTRA_CA_CERTS` is additive, but pointing it at the same merged bundle is harmless.)

### OIDC Login Hook

The `customize/` directory includes a sample `login_hook.py` that restricts OIDC logins to invited users. Bind-mount it anywhere in the container and point the env var at it:

```bash
docker run -d \
  -v ./login_hook.py:/etc/klangk/login_hook.py:ro \
  -e KLANGK_OIDC_LOGIN_HOOK=/etc/klangk/login_hook.py \
  ...
```

The file is loaded directly by path — it does not need to be on `PYTHONPATH`. No image rebuild is needed. To call a function other than the default `on_login`, append `:func_name` to the path.

The example hook's logic:

- Most recent invitation is **revoked** → login is **blocked** (even with an existing account)
- Most recent invitation is **pending** or **accepted** → login is **allowed**
- **No invitation** but has an **existing account** → login is **allowed**
- **No invitation and no account** → login is **blocked**

Re-inviting after a revocation creates a new pending invitation that overrides the revocation.

### OIDC Authentication

To enable OIDC login, create an `oidc.yaml` and mount it at runtime:

```bash
docker run -d \
  -v ./oidc.yaml:/home/klangk/oidc.yaml:ro \
  -e KLANGK_OIDC_CONFIG=/home/klangk/oidc.yaml \
  -e KLANGK_AUTH_MODES=both \
  ...
```

See the [OIDC documentation](../reference/oidc.md) for the config file format.

## Building a Custom Image (Plugins)

A custom image build is needed **only for plugins**. If you don't need plugins, use the stock `klangk-host` image with the runtime customization above.

### Prerequisites

- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/)
- Docker
- SSH key with access to the git repos listed in `plugins.yaml`

### Directory Layout

```text
customize/
  build.sh            # Main build script (run this)
  build-inner.sh      # Devenv-side build (called by build.sh)
  Dockerfile          # Custom image layer on top of klangk-host
  docker-compose.yml  # Example runtime configuration
  plugins.yaml        # Plugin list for the build
  login_hook.py       # Example OIDC login hook (bind-mounted, not baked)
  mount/              # Mount directory for workspace bind mounts
```

### Plugins

Edit `plugins.yaml` to add or remove plugins. The default set includes the built-in plugins: celebrate, beep, pig-latin, word-count, browser-fetch, bobdobbs.

To add an external plugin:

```yaml
plugins:
  - name: my-plugin
    git: https://github.com/myorg/my-klangk-plugin.git
    ref: main
```

See the [Creating Plugins](../development/creating-plugins.md) reference for plugin structure details.

### Build

```bash
cd customize
./build.sh

# Or pin to a specific Klangk release:
KLANGK_REF=v1.0 ./build.sh
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
1. Runs `docker build` with the `Dockerfile`, which layers the rebuilt web frontend and workspace image tarball on top of `klangk-host:latest`
1. Cleans up temporary build artifacts

**`build-inner.sh`** (runs inside devenv shell):

1. Fetches plugins listed in `plugins.yaml` via `update_plugins.py`
1. Rebuilds the Flutter web frontend with Dart plugin UI via `flutterbuildweb.sh`
1. Rebuilds the workspace container image with plugin extensions and tools via `build-workspace-image.sh`
1. Exports the final workspace image as `workspace.tar` via `podman save`

**`Dockerfile`** (custom image layer):

Extends `klangk-host:latest` and replaces two things:

- The Flutter web build with the plugin-enabled version
- The workspace image tarball with the plugin-enabled version

### Build Options

| Variable            | Default                                    | Description                                        |
| ------------------- | ------------------------------------------ | -------------------------------------------------- |
| `KLANGK_REF`        | `main`                                     | Klangk branch, tag, or commit SHA to build against |
| `KLANGK_REPO`       | `https://github.com/mcdonc/klangk.git`     | Klangk repo URL                                    |
| `KLANGK_HOST_IMAGE` | `ghcr.io/mcdonc/klangk/klangk-host-custom` | Output image name                                  |
| `KLANGK_PLATFORM`   | `linux/amd64`                              | Target platform                                    |

## Running

Use the stock image with runtime customization (no plugins):

```bash
docker run -d \
  -p 8995:8995 \
  -v ./data:/home/klangk/data \
  -v ./mount:/home/klangk/mount \
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
  -e KLANGK_PRODUCT_NAME="Acme Labs" \
  -e KLANGK_LOGO_URL="/branding/logo.png" \
  -e KLANGK_LLM_BASE_URL=https://ollama.com/v1 \
  -e KLANGK_LLM_API_KEY=your-api-key \
  -e KLANGK_LLM_MODEL=gemma4:31b \
  ghcr.io/mcdonc/klangk/klangk-host:latest
```

Or use `docker-compose.yml` — see the example in `customize/docker-compose.yml`.
