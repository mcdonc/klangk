# Customizing a Deployment

Most Klangk customization is done at **runtime** via environment variables and bind mounts — no image rebuild required. The stock [`klangk-host`](https://github.com/mcdonc/klangk/pkgs/container/klangk%2Fklangk-host) image supports branding, legal links, email templates, CA certificates, and OIDC login hooks out of the box.

**The only reason to build a custom host image is features** (Dart UI features require a Flutter web rebuild; TypeScript workspace features require a workspace image rebuild). See [Building a Custom Image (Features)](#building-a-custom-image-features) below.

The [`customize/`](https://github.com/mcdonc/klangk/tree/main/customize) directory in the Klangk repo provides a working example: a `docker-compose.yml` showcasing runtime configuration, and example runtime-customization files under `custom/`. Copy it and adapt to your needs.

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

- **Copy the whole tree, then edit.** The built-in templates live at `src/klangk/klangk/email_templates/` in the source. Copy, delete `__init__.py`, edit, and mount.
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

### Consent Banner

A login/consent banner lets you require acknowledgement of an acceptable-use
notice (or any policy text) before a user can access the app.

Set the banner text (and an optional title) via env vars or the
[klangkd config file](../reference/klangkd-config.md) branding keys:

```bash
docker run -d \
  -e KLANGK_LOGIN_BANNER_TITLE="Notice" \
  -e KLANGK_LOGIN_BANNER="You must accept the terms to continue." \
  ...
```

When set, the banner blocks all access until the user clicks **I Accept**.

By default the acceptance is **cached permanently** against the banner text
hash — once accepted, the same banner text won't re-prompt the user (even on
later visits). To change the wording, operators edit the banner text so the
hash flips.

**Require acceptance on every visit.** For regulated deployments that need a
per-session acknowledgement (e.g. a legal notice that must be re-accepted on
each fresh app load / login), set:

```bash
docker run -d \
  -e KLANGK_LOGIN_BANNER_EVERY_VISIT=true \
  -e KLANGK_LOGIN_BANNER="..." \
  ...
```

or in the config file:

```yaml
login_banner_every_visit: true
```

When `true`, acceptance is held **for the session only** (in-memory) — the
banner re-appears on every app restart / re-login until **I Accept** is
clicked that session. It does **not** re-appear on in-app route changes within
the same session. When `false` (default), behavior is unchanged (permanent
hash-based acceptance). (#1544)

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
> yet work with the published host image (the proxy `/auth/local` ACL denies
> the port-forwarded request). For the Docker image, set
> `KLANGK_AUTH_MODES=password` (or `oidc`/`both`) until #1391 lands.
> Locally (devenv, or running the binary on your own machine) `none` works
> out of the box.

## Building a Custom Image (Features)

A custom image build is needed **only for features**. If you don't need features, use the stock `klangk-host` image with the runtime customization above.

The feature declaration list is the checked-in [`features.yaml`](https://github.com/mcdonc/klangk/blob/main/features.yaml) at the repository root — the build-time source of truth. To ship a different feature set than stock, **fork the repo and edit that file directly** (the same model `package.json`/`Cargo.toml`/`go.mod` use). There is no separate overlay or build script to maintain.

### Prerequisites

- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/)
- Docker
- SSH key with access to the git repos listed in `features.yaml`

### Fork-and-build workflow

```bash
# 1. Fork klangk on GitHub, clone your fork.
git clone https://github.com/<your-org>/klangk.git
cd klangk

# 2. Add feature source trees under features/<name>/ (for local features) and
#    declare every feature you want compiled in via the checked-in features.yaml
#    at the repo root — same format (local `path:` or remote `git:`/`ref:`
#    entries).
$EDITOR features.yaml

# 3. Build the host image from source (inside the devenv shell — the
#    wrapped `build-host-image` script is on PATH there).
devenv shell -- build-host-image

# Tag the build with a variant identity (surfaced in version.json + debug pane).
# NOTE: KLANGK_VARIANT is captured at devenv-shell entry (generate-version.sh
# runs in enterShell), so set it *before* `devenv shell`:
KLANGK_VARIANT="Acme 1.0.0" devenv shell -- build-host-image

# Publish elsewhere than the default local tag (klangk-host):
KLANGK_HOST_IMAGE=ghcr.io/<your-org>/klangk-host devenv shell -- build-host-image
```

To pull upstream klangk improvements into your custom build, `git pull upstream main` (or `origin main`, depending on how you cloned) from the fork — the feature declaration and feature trees merge like any other source file.

### Features

Edit the checked-in `features.yaml` to add or remove features. The default build compiles in the built-in features declared there: celebrate, beep, bobdobbs, word-count, browser-fetch, boingball, git-credential (`word-count` and `soliplex` ship compiled-in but dormant — activate with `KLANGK_FEATURES_ENABLE`).

To add an external feature:

```yaml
features:
  - name: my-feature
    git: https://github.com/myorg/my-klangk-feature.git
    ref: main
```

### How the Build Works

`scripts/build-host-image.sh` is a single source build: it embeds the Flutter web build, the workspace tarball, **and** the feature directories declared in `features.yaml` — so one build produces the final image with features baked in. There is no separate overlay, `Dockerfile`, or base-image pass. Run it via the devenv-wrapped `build-host-image` script (`devenv shell -- build-host-image`); `KLANGK_VARIANT` is captured by `scripts/generate-version.sh` in devenv's `enterShell` hook, so set it (and `KLANGK_HOST_IMAGE`) **before** entering the shell, not on the build command.

### Build Options

The build reads these from the environment (`KLANGK_HOST_IMAGE` / `KLANGK_PLATFORM` by `scripts/build-host-image.sh`; `KLANGK_VARIANT` by `scripts/generate-version.sh` during the build):

| Variable            | Default       | Description                                                                               |
| ------------------- | ------------- | ----------------------------------------------------------------------------------------- |
| `KLANGK_HOST_IMAGE` | `klangk-host` | Output image name — a local tag by default; override with a full registry path to publish |
| `KLANGK_VARIANT`    | _unset_       | Build identity string written to `version.json` (see [Build Variant](#build-variant))     |
| `KLANGK_PLATFORM`   | `linux/amd64` | Target platform                                                                           |

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
# KLANGK_VARIANT is read at devenv-shell entry, so set it before `devenv shell`:
KLANGK_VARIANT="Acme 1.0.0" devenv shell -- build-host-image
```

When empty (or unset), the `variant` field is **omitted entirely** from
`version.json` and the API/debug output — so a fork that doesn't set it is
byte-identical to upstream. Set it only if you want to distinguish your build.

> The variant is a single free-form string (e.g. `"Acme 1.0.0"`). A split into
> separate name + version fields is a non-goal for now — keep them together in
> one human-readable string.

## Running

Use the stock image with runtime customization (no features):

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
  -e KLANGK_EGRESS_PORT=8995 \
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
