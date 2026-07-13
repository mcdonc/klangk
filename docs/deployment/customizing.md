# Customizing a Deployment

Most Klangk customization is done at **runtime** via environment variables and bind mounts — no image rebuild required. The stock [`klangk-host`](https://github.com/mcdonc/klangk/pkgs/container/klangk%2Fklangk-host) image supports branding, legal links, email templates, CA certificates, and OIDC login hooks out of the box.

**The only reason to build a custom host image is plugins** (Dart UI plugins require a Flutter web rebuild; TypeScript workspace plugins require a workspace image rebuild). See [Building a Custom Image (Plugins)](#building-a-custom-image-plugins) below.

The [`customize/`](https://github.com/mcdonc/klangk/tree/main/customize) directory in the Klangk repo provides a working example: a `docker-compose.yml` showcasing runtime configuration, and build scripts for the plugins-only custom image path. Copy it and adapt to your needs.

## Runtime Customization

### Customization Directory

Set `KLANGK_CUSTOMIZE_DIR` to a single directory containing all your customization files. Klangk looks for well-known subdirectories under this path:

```text
<KLANGK_CUSTOMIZE_DIR>/
  certs/           ← CA .pem/.crt files (custom CA certificates)
  branding/        ← logos and other static assets served at /branding
  email-templates/ ← Jinja2 email template overrides
```

If a subdirectory doesn't exist, that subsystem simply isn't customized — no error, no special handling needed. Deployers only populate the subdirs they care about.

Default: `~/.klangk/custom` (or `/home/klangk/custom` in the container image).

```bash
docker run -d \
  -v ./my-customization:/home/klangk/custom:ro \
  -e KLANGK_CUSTOMIZE_DIR=/home/klangk/custom \
  ...
```

One env var and one `-v` mount replaces three. The per-feature env vars `KLANGK_SSL_CERT_DIR` and `KLANGK_EMAIL_TEMPLATES_DIR` still work as overrides but are **deprecated** — prefer the unified directory. `KLANGK_BRANDING_DIR` has been removed; branding assets are resolved from `<KLANGK_CUSTOMIZE_DIR>/branding/` or `<KLANGK_DATA_DIR>/branding/`.

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

To serve a local file without a CDN, drop your logo into `<KLANGK_CUSTOMIZE_DIR>/branding/` and set `KLANGK_LOGO_URL=/branding/logo.png`. Both steps are needed: placing the file makes it servable (Klangk mounts the branding directory at `/branding/`), while `KLANGK_LOGO_URL` tells the frontend which image to render — this could equally be an external CDN URL like `https://cdn.example.com/logo.png`. When `<KLANGK_CUSTOMIZE_DIR>/branding/` doesn't exist, branding falls back to `<KLANGK_DATA_DIR>/branding/`.

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

Outgoing auth emails (registration verification, password reset, invitation) are rendered from Jinja2 templates. Place your template overrides in `<KLANGK_CUSTOMIZE_DIR>/email-templates/`.

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
  -e KLANGK_CUSTOMIZE_DIR=/home/klangk/custom \
  -v ./my-customization:/home/klangk/custom:ro \
  ...
```

> **Deprecated:** `KLANGK_EMAIL_TEMPLATES_DIR` still works as an override but prefer using `<KLANGK_CUSTOMIZE_DIR>/email-templates/` instead.

### Custom CA Certificates

Place your `.pem`/`.crt` CA certificate files in `<KLANGK_CUSTOMIZE_DIR>/certs/` and **restart** workspaces (or the backend). Klangk makes those CAs trusted at startup without rebuilding any image:

- **Workspace containers** — the directory is bind-mounted read-only into each container, and the entrypoint builds a merged CA bundle (system CAs plus your custom certs) on the writable `/tmp` tmpfs. The toolchain trust env vars (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`) are set to point at it, so OpenSSL, Python, `curl`, and Node all honor your CAs. The bundle is merged with system CAs so public-internet TLS keeps working.
- **Backend process** — at startup the backend concatenates your certs with its own system bundle into `<KLANGK_STATE_DIR>/ssl/ca-bundle.crt` and sets the same trust env vars, so outbound TLS (OIDC discovery, SMTP relay, LLM proxy) trusts your private CAs too.

```bash
# Using KLANGK_CUSTOMIZE_DIR (recommended):
docker run -d \
  -e KLANGK_CUSTOMIZE_DIR=/home/klangk/custom \
  -v ./my-customization:/home/klangk/custom:ro \
  ...
# Place your .pem/.crt files in my-customization/certs/
```

Rotating a cert is just a file change plus a workspace/backend restart — no image rebuild.

> **Deprecated:** `KLANGK_SSL_CERT_DIR` still works as an override but prefer using `<KLANGK_CUSTOMIZE_DIR>/certs/` instead.
>
> **Why a merged bundle?** `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` _replace_ the default trust store rather than add to it. Klangk therefore prepends the system CAs before your custom certs. (`NODE_EXTRA_CA_CERTS` is additive, but pointing it at the same merged bundle is harmless.)

### OIDC Login Hook

The `customize/custom/oidc/` directory includes a sample `login_hook.py` that restricts OIDC logins to invited users. Bind-mount it anywhere in the container and point the env var at it:

```bash
docker run -d \
  -v ./oidc/login_hook.py:/etc/klangk/login_hook.py:ro \
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

To enable OIDC login, create an `oidc.yaml` and mount it at runtime. The `customize/custom/oidc/oidc.yaml` template has the schema and placeholder values:

```bash
docker run -d \
  -v ./oidc/oidc.yaml:/home/klangk/oidc/oidc.yaml:ro \
  -e KLANGK_OIDC_CONFIG=/home/klangk/oidc/oidc.yaml \
  -e KLANGK_AUTH_MODES=both \
  ...
```

See the [OIDC documentation](../reference/oidc.md) for the config file format.

### Deployment profiles (auth modes)

`KLANGK_AUTH_MODES` selects the deployment profile — same binary, different
config. For a **no-login local-dev** server (single user, your own browser),
use `none` — it auto-issues a token for the seeded default user and must bind
loopback:

```bash
docker run -d \
  -e KLANGK_AUTH_MODES=none \
  ...
```

See [Auth Modes](../features/auth-modes.md) for the full
local-dev / customer-locked / team mapping.

> **Note for Docker users:** `none` is loopback-only by design, and a
> `docker run -p` published port isn't loopback — so `none` mode does not
> yet work with the published host image (the nginx `/auth/local` ACL denies
> the port-forwarded request). For the Docker image, set
> `KLANGK_AUTH_MODES=password` (or `oidc`/`both`) until #1391 lands.
> Locally (devenv, or running the binary on your own machine) `none` works
> out of the box.

## Building a Custom Image (Plugins)

A custom image build is needed **only for plugins**. If you don't need plugins, use the stock `klangk-host` image with the runtime customization above.

### Prerequisites

- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/)
- Docker
- SSH key with access to the git repos listed in `plugins.yaml`

### Directory Layout

```text
customize/
  docker-compose.yml  # Example runtime configuration (all runtime knobs)
  build/
    build.sh          # Main build script (run this)
    plugins.yaml      # Plugin list for the build
  custom/             # Mounted as KLANGK_CUSTOMIZE_DIR at runtime
    oidc/
      oidc.yaml       # Example OIDC provider config (runtime-mounted)
      login_hook.py   # Example OIDC login hook (runtime-mounted, not baked)
    certs/
      cacert.pem      # Example custom CA certificate (runtime-mounted)
    branding/
      logo.png        # Example logo served at /branding (runtime-mounted)
    email-templates/  # Jinja2 email template overrides (runtime-mounted)
  data/               # Persistent state (bind-mounted, gitignored)
  mount/              # Workspace bind-mount root (bind-mounted, gitignored)
```

The `custom/` directory is bind-mounted as `KLANGK_CUSTOMIZE_DIR` by
`docker-compose.yml` — no image rebuild needed. Only `build/` is involved
in the image build.

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
./build/build.sh

# Or pin to a specific Klangk release:
KLANGK_REF=v1.0.1 ./build/build.sh

# Tag the build with a variant identity (surfaced in version.json + debug pane):
KLANGK_VARIANT="Acme 1.0.0" ./build/build.sh
```

The resulting image is tagged `ghcr.io/mcdonc/klangk/klangk-host-custom:latest` by default. Override with `KLANGK_HOST_IMAGE`.

### How the Build Works

The build is a single source build: `build/build.sh` clones klangk at the
pinned ref, stages `plugins.yaml`, then runs klangk's own
`scripts/build-host-image.sh` inside a devenv shell. That upstream script
already embeds the Flutter web build, the workspace tarball, **and** the
plugin directories — so one build produces the final image with plugins
baked in. There is no separate overlay, `Dockerfile`, or base-image pass.

The variant string (`KLANGK_VARIANT`, default `custom`) is exported into the
devenv shell so `generate-version.sh` writes it into the image's
`version.json` (see [Build Variant](#build-variant) below).

### Build Options

| Variable            | Default                                    | Description                                                                           |
| ------------------- | ------------------------------------------ | ------------------------------------------------------------------------------------- |
| `KLANGK_REF`        | `main`                                     | Klangk branch, tag, or commit SHA to build against                                    |
| `KLANGK_REPO`       | `https://github.com/mcdonc/klangk.git`     | Klangk repo URL                                                                       |
| `KLANGK_HOST_IMAGE` | `ghcr.io/mcdonc/klangk/klangk-host-custom` | Output image name                                                                     |
| `KLANGK_VARIANT`    | `custom`                                   | Build identity string written to `version.json` (see [Build Variant](#build-variant)) |
| `KLANGK_PLATFORM`   | `linux/amd64`                              | Target platform                                                                       |

### Build Variant

`KLANGK_VARIANT` stamps a **product-identity string** into the built image's
`version.json`. It is surfaced in three places:

- **`GET /api/v1/version`** — a `variant` field (between `version` and `commit`)
- **The debug pane** — a "Variant" row (shown only when the field is present)
- **`version.json`** on disk — the source of truth, written by
  `scripts/generate-version.sh` at build time

It is **independent of the upstream klangk version** — `version` always reports
the klangk release (tag/branch/SHA), while `variant` names _this_ downstream
build. Set it to your product name and release, e.g. `"Acme 1.0.0"`:

```bash
KLANGK_VARIANT="Acme 1.0.0" ./build/build.sh
```

Or edit the `VARIANT` default at the top of `customize/build/build.sh`.

When empty (or unset), the `variant` field is **omitted entirely** from
`version.json` and the API/debug output — stock klangk builds are byte-identical
whether the feature exists or not. The `customize/build.sh` template defaults
it to `"custom"` so a copied template never impersonates upstream klangk; clear
that default only if you want stock output.

> The variant is a single free-form string (e.g. `"Acme 1.0.0"`). A split into
> separate name + version fields is a non-goal for now — keep them together in
> one human-readable string.

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
