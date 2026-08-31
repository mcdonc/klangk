# Customizing a Deployment

Most Klangk customization is done at **runtime** via environment variables and bind mounts — no image rebuild required. The stock [`klangk-host`](https://github.com/mcdonc/klangk/pkgs/container/klangk%2Fklangk-host) image supports branding, legal links, email templates, CA certificates, and OIDC login hooks out of the box.

**The only reason to build a custom host image is features** (Dart UI features require a Flutter web rebuild; TypeScript workspace features require a workspace image rebuild). See [Building a Custom Image (Features)](#building-a-custom-image-features) below.

The [`customize/`](https://github.com/mcdonc/klangk/tree/main/customize) directory in the Klangk repo provides a working example: a `docker-compose.yml` showcasing runtime configuration, and example runtime-customization files under `custom/`. Copy it and adapt to your needs.

## Runtime Customization

### Customization Directory

Set `KLANGKD_CUSTOMIZE_DIR` to a single directory containing all your customization files. Klangk looks for well-known subdirectories under this path:

```text
<KLANGKD_CUSTOMIZE_DIR>/
  certs/           ← CA .pem/.crt files (custom CA certificates)
  branding/        ← logos and other static assets served at /branding
  email-templates/ ← Jinja2 email template overrides
```

If a subdirectory doesn't exist, that subsystem simply isn't customized — no error, no special handling needed. Deployers only populate the subdirs they care about.

Default: `<config_dir>/custom` (→ `~/.config/klangkd/custom`; `KLANGKD_CONFIG_DIR`
relocates the whole tree). The published host container sets it to
`/home/klangk/custom`.

```bash
docker run -d \
  -v ./my-customization:/home/klangk/custom:ro \
  -e KLANGKD_CUSTOMIZE_DIR=/home/klangk/custom \
  ...
```

One env var and one `-v` mount replaces three. `KLANGKD_EMAIL_TEMPLATES_DIR` still works as an override but is **deprecated** — prefer the unified directory. `KLANGKD_BRANDING_DIR` and `KLANGKD_SSL_CERT_DIR` have been removed; branding resolves from `<KLANGKD_CUSTOMIZE_DIR>/branding/` (or `<KLANGKD_DATA_DIR>/branding/`) and custom CA certs from `<KLANGKD_CUSTOMIZE_DIR>/certs/`.

### Product Name

Set `KLANGKD_PRODUCT_NAME` to rename the product across the browser tab title, the app-bar logo wordmark, and all outgoing emails. Defaults to `Klangk`. Supports the `file:`/`cmd:` prefix.

```bash
docker run -d \
  -e KLANGKD_PRODUCT_NAME="Acme Labs" \
  ...
```

The value is published to the frontend through `GET /api/v1/config` (`product_name` field) and interpolated into email subjects and bodies server-side.

### Logo

Set `KLANGKD_LOGO_URL` to an absolute image URL. The value is published to the UI through the unauthenticated `/api/v1/config` endpoint (`logo_url`), so it renders on the login page before login. Supports `file:`/`cmd:` secret resolution.

To serve a local file without a CDN, drop your logo into `<KLANGKD_CUSTOMIZE_DIR>/branding/` and set `KLANGKD_LOGO_URL=/branding/logo.png`. Both steps are needed: placing the file makes it servable (Klangk mounts the branding directory at `/branding/`), while `KLANGKD_LOGO_URL` tells the frontend which image to render — this could equally be an external CDN URL like `https://cdn.example.com/logo.png`. When `<KLANGKD_CUSTOMIZE_DIR>/branding/` doesn't exist, branding falls back to `<KLANGKD_DATA_DIR>/branding/`.

When unset (or if the image fails to load), the default `KlangkLogo` widget is rendered. The logo also flows into email headers when emails are rendered through the templating system.

### Legal & Support Links

These env vars add links to the login/registration screens and email footers. All are plain values (no `file:`/`cmd:` resolution — they are public, shown pre-auth). Empty hides them.

| Variable                | Description                                                            |
| ----------------------- | ---------------------------------------------------------------------- |
| `KLANGKD_TERMS_URL`     | Terms of Service link                                                  |
| `KLANGKD_PRIVACY_URL`   | Privacy Policy link                                                    |
| `KLANGKD_AUP_URL`       | Acceptable Use Policy link                                             |
| `KLANGKD_SUPPORT_URL`   | Support/help link (app bar + auth screens)                             |
| `KLANGKD_SUPPORT_EMAIL` | Support email (`mailto:` fallback when `KLANGKD_SUPPORT_URL` is unset) |

### Email Templating

Outgoing auth emails (registration verification, password reset, invitation) are rendered from Jinja2 templates. Place your template overrides in `<KLANGKD_CUSTOMIZE_DIR>/email-templates/`.

Two approaches:

- **Copy the whole tree, then edit.** The built-in templates live at `src/klangk/klangk/email_templates/` in the source. Copy, delete `__init__.py`, edit, and mount.
- **Drop only the files you change.** Absent files fall through to the built-ins.

Overrides resolve per-file: a deployer file shadows the built-in at the same path, and `{% extends %}`/`{% include %}` resolve your overrides first. Override just `base.html` to re-brand all emails at once.

> **Keep the `.html` extension** on HTML templates (not `.html.j2`). Klangk enables Jinja autoescaping by filename.

#### Template Variables

**Global** (every email): `product_name` (`KLANGKD_PRODUCT_NAME`), `logo_url` (`KLANGKD_LOGO_URL`), `brand_color` (`KLANGKD_BRAND_COLOR`, default `#E65100`).

**Per-email**: `link` (the verification/reset/invite URL), `expiry_hours` (real token TTL), and `invited_by` (invitation only).

> **Tokens never appear in the subject line** — subjects receive only the global branding variables, never the link.

#### Other Email Knobs

- **`KLANGKD_SMTP_REPLY_TO`** — adds a `Reply-To` header to every outgoing message. Unset means no header.
- **Footer / legal line** — `base.html` exposes an empty `{% block legal %}` for a compliance footer.

#### Example

```bash
docker run -d \
  -e KLANGKD_PRODUCT_NAME="Acme Labs" \
  -e KLANGKD_LOGO_URL="/branding/logo.png" \
  -e KLANGKD_SMTP_REPLY_TO="support@acme.example.com" \
  -e KLANGKD_CUSTOMIZE_DIR=/home/klangk/custom \
  -v ./my-customization:/home/klangk/custom:ro \
  ...
```

> **Deprecated:** `KLANGKD_EMAIL_TEMPLATES_DIR` still works as an override but prefer using `<KLANGKD_CUSTOMIZE_DIR>/email-templates/` instead.

### Consent Banner

A login/consent banner lets you require acknowledgement of an acceptable-use
notice (or any policy text) before a user can access the app.

Set the banner text (and an optional title) via env vars or the
[klangkd config file](../reference/klangkd-config.md) branding keys:

```bash
docker run -d \
  -e KLANGKD_LOGIN_BANNER_TITLE="Notice" \
  -e KLANGKD_LOGIN_BANNER="You must accept the terms to continue." \
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
  -e KLANGKD_LOGIN_BANNER_EVERY_VISIT=true \
  -e KLANGKD_LOGIN_BANNER="..." \
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

Place your `.pem`/`.crt` CA certificate files in `<KLANGKD_CUSTOMIZE_DIR>/certs/` and **restart** workspaces (or the backend). Klangk makes those CAs trusted at startup without rebuilding any image:

- **Workspace containers** — the directory is bind-mounted read-only into each container, and the entrypoint builds a merged CA bundle (system CAs plus your custom certs) on the writable `/tmp` tmpfs. The toolchain trust env vars (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS`) are set to point at it, so OpenSSL, Python, `curl`, and Node all honor your CAs. The bundle is merged with system CAs so public-internet TLS keeps working.
- **Backend process** — at startup the backend concatenates your certs with its own system bundle into `<KLANGKD_STATE_DIR>/ssl/ca-bundle.crt` and sets the same trust env vars, so outbound TLS (OIDC discovery, SMTP relay, LLM proxy) trusts your private CAs too.

```bash
# Using KLANGKD_CUSTOMIZE_DIR (recommended):
docker run -d \
  -e KLANGKD_CUSTOMIZE_DIR=/home/klangk/custom \
  -v ./my-customization:/home/klangk/custom:ro \
  ...
# Place your .pem/.crt files in my-customization/certs/
```

Rotating a cert is just a file change plus a workspace/backend restart — no image rebuild.

> **Why a merged bundle?** `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` _replace_ the default trust store rather than add to it. Klangk therefore prepends the system CAs before your custom certs. (`NODE_EXTRA_CA_CERTS` is additive, but pointing it at the same merged bundle is harmless.)

### OIDC Login Hook

The `customize/custom/oidc/` directory includes a sample `login_hook.py` that restricts OIDC logins to invited users. Bind-mount it anywhere in the container and point the env var at it:

```bash
docker run -d \
  -v ./oidc/login_hook.py:/etc/klangk/login_hook.py:ro \
  -e KLANGKD_OIDC_LOGIN_HOOK=/etc/klangk/login_hook.py \
  ...
```

The file is loaded directly by path — it does not need to be on `PYTHONPATH`. No image rebuild is needed. To call a function other than the default `on_login`, append `:func_name` to the path.

The example hook's logic:

- Most recent invitation is **revoked** → login is **blocked** (even with an existing account)
- Most recent invitation is **pending** or **accepted** → login is **allowed**
- **No invitation** but has an **existing account** → login is **allowed**
- **No invitation and no account** → login is **blocked**

Re-inviting after a revocation creates a new pending invitation that overrides the revocation.

### Workspace-Created Hook

A deployment-local Python callback that runs after every workspace is created — on all three creation paths: a fresh `POST /workspaces`, an **import**, and a **duplicate** — after the workspace row, the owner ACE, and the four role groups are committed, and before the create response is returned. It can mutate the new workspace and rewrite its ACL, which is the supported way to change the seeded permission posture without forking the model layer.

The `customize/custom/hooks/` directory includes a sample `workspace_created.py`. Bind-mount it anywhere in the container and point the env var at it:

```bash
docker run -d \
  -v ./hooks/workspace_created.py:/etc/klangk/workspace_created.py:ro \
  -e KLANGKD_WORKSPACE_CREATED_HOOK=/etc/klangk/workspace_created.py \
  ...
```

Like the login hook, the file is loaded directly by path (no `PYTHONPATH` entry, no image rebuild), supports `:func_name` (default `on_workspace_created`), and is re-loaded on SIGHUP reconfigure.

The hook receives two arguments:

- `workspace` — a deep copy of the new workspace's row **dict** (assign keys to mutate attributes: `workspace["egress_mode"] = "static"`; `name`, `image`, `mounts`, `env`, `allowed_domains`, `rejected_domains`, `settings`, `per_handle_home`, … all work; nested edits like `workspace["env"]["K"] = "v"` are detected too, and deleting a key clears the column). Edits are persisted after the hook returns, with the **same validation the create API applies** — the settings-bag schema, the image allowlist, the mount-spec policy, the domain-list grammar, and the `egress_mode` / `setup_state` / `per_handle_home` enums. An invalid edit is logged and dropped, the create still succeeds. It also carries two async ACL helpers:
  - `entries = await workspace.acl_entries()` — the workspace's ACL, resolved like `GET /api/v1/workspaces/{id}/acl` (each entry's `principal` is the group name, user email, or `Everyone`/`Authenticated`);
  - `await workspace.rewrite_acl(entries)` — replaces the ACL wholesale; filter/edit/extend the list from `acl_entries()` and hand it back. The **list order becomes the ACL order**, so add/remove/reorder are all the same operation. Keep an entry granting the owner access, or every new workspace starts fully locked out.
- `actor` — the creating user's row dict (`actor["id"]`, `actor["email"]`, …).

Both sync and async hook functions are supported. ACL rewrites require the `async def` form (calling the awaitable helpers from a sync hook raises a clear error — it never silently no-ops).

**Failure semantics: log-and-continue.** The hook is a mutation extension point, not a gate — if it raises, the workspace still exists and the create response is returned normally. Errors are logged as a WARNING with the hook source, workspace id, and the exception, so partial effects stay visible. A missing hook file or a missing/uncallable `on_workspace_created` export _is_ a startup failure (same as a broken login hook).

The sample hook's example: force every new workspace to `egress_mode: static`, and keep the coders/collaborators role groups' `files` grant while dropping `files-download` and `files-write` for a browse-only posture (listings and metadata stay visible; no file body can be read or downloaded). Adapt it to your deployment's stance.

### OIDC Authentication

To enable OIDC login, create an `oidc.yaml` and mount it at runtime. The `customize/custom/oidc/oidc.yaml` template has the schema and placeholder values:

```bash
docker run -d \
  -v ./oidc/oidc.yaml:/home/klangk/oidc/oidc.yaml:ro \
  -e KLANGKD_OIDC_CONFIG=/home/klangk/oidc/oidc.yaml \
  -e KLANGKD_AUTH_MODES=both \
  ...
```

See the [OIDC documentation](../reference/oidc.md) for the config file format.

### Deployment profiles (auth modes)

`KLANGKD_AUTH_MODES` selects the deployment profile — same binary, different
config. The published host image uses **`password`** as its supported mode
(`oidc` and `both` are also supported). The `none` (no-login local-dev)
profile is **unsupported with the published Docker host image** — see the
note below.

See [Auth Modes](../features/auth-modes.md) for the full
local-dev / customer-locked / team mapping.

> **`none` mode is unsupported in the Docker host image.** `none` is
> loopback-only by design (it freely issues an admin token with no
> password, so its security model is "only the operator's loopback can
> reach it"), and a `docker run -p` published port isn't loopback. The
> bind-safety gate refuses to boot `none` on a non-loopback bind, and even
> with `KLANGKD_ALLOW_INSECURE_NO_AUTH=1` the proxy `/auth/local` ACL still
> denies the port-forwarded request with `403` (the request appears at the
> container as the Docker bridge IP, not `127.0.0.1`). The image therefore
> runs `KLANGKD_AUTH_MODES=password` (or `oidc`/`both`) — set
> `KLANGKD_DEFAULT_PASSWORD` accordingly. For a no-login single-user
> experience, run klangk locally (devenv, or the bare binary on your own
> machine), where `none` works out of the box. See
> [#1391](https://github.com/mcdonc/klangk/issues/1391).

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
# NOTE: KLANGKBUILD_VARIANT is captured at devenv-shell entry (generate-version.sh
# runs in enterShell), so set it *before* `devenv shell`:
KLANGKBUILD_VARIANT="Acme 1.0.0" devenv shell -- build-host-image

# Publish elsewhere than the default local tag (klangk-host):
KLANGKBUILD_HOST_IMAGE=ghcr.io/<your-org>/klangk-host devenv shell -- build-host-image
```

To pull upstream klangk improvements into your custom build, `git pull upstream main` (or `origin main`, depending on how you cloned) from the fork — the feature declaration and feature trees merge like any other source file.

### Features

Edit the checked-in `features.yaml` to add or remove features. The default build compiles in the built-in features declared there: celebrate, beep, bobdobbs, word-count, browser-fetch, boingball, git-credential (`word-count` and `soliplex` ship compiled-in but dormant — activate with `KLANGKD_FEATURES_ENABLE`).

To add an external feature:

```yaml
features:
  - name: my-feature
    git: https://github.com/myorg/my-klangk-feature.git
    ref: main
```

### How the Build Works

`scripts/build-host-image.sh` is a single source build: it embeds the Flutter web build, the workspace tarball, **and** the feature directories declared in `features.yaml` — so one build produces the final image with features baked in. There is no separate overlay, `Dockerfile`, or base-image pass. Run it via the devenv-wrapped `build-host-image` script (`devenv shell -- build-host-image`); `KLANGKBUILD_VARIANT` is captured by `scripts/generate-version.sh` in devenv's `enterShell` hook, so set it (and `KLANGKBUILD_HOST_IMAGE`) **before** entering the shell, not on the build command.

### Build Options

The build reads these from the environment (`KLANGKBUILD_HOST_IMAGE` / `KLANGKBUILD_PLATFORM` by `scripts/build-host-image.sh`; `KLANGKBUILD_VARIANT` by `scripts/generate-version.sh` during the build):

| Variable                 | Default       | Description                                                                               |
| ------------------------ | ------------- | ----------------------------------------------------------------------------------------- |
| `KLANGKBUILD_HOST_IMAGE` | `klangk-host` | Output image name — a local tag by default; override with a full registry path to publish |
| `KLANGKBUILD_VARIANT`    | _unset_       | Build identity string written to `version.json` (see [Build Variant](#build-variant))     |
| `KLANGKBUILD_PLATFORM`   | `linux/amd64` | Target platform                                                                           |

### Build Variant

`KLANGKBUILD_VARIANT` stamps a **product-identity string** into the built image's
`version.json`. It is surfaced in three places:

- **`GET /api/v1/version`** — a `variant` field (between `version` and `commit`)
- **The debug pane** — a "Variant" row (shown only when the field is present)
- **`version.json`** on disk — the source of truth, written by
  `scripts/generate-version.sh` at build time

It is **independent of the upstream klangk version** — `version` always reports
the klangk release (tag/branch/SHA), while `variant` names _this_ downstream
build. Set it to your product name and release, e.g. `"Acme 1.0.0"`:

```bash
# KLANGKBUILD_VARIANT is read at devenv-shell entry, so set it before `devenv shell`:
KLANGKBUILD_VARIANT="Acme 1.0.0" devenv shell -- build-host-image
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
  -p 8997:8997 \
  -v ./data:/home/klangk/data \
  -v ./mount:/home/klangk/mount \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  -e KLANGKD_EGRESS_PORT=8995 \
  -e KLANGKD_PORT=8997 \
  -e KLANGKD_DEFAULT_USER=admin@example.com \
  -e KLANGKD_DEFAULT_PASSWORD=changeme \
  -e KLANGKD_JWT_SECRET=change-this-to-a-random-secret \
  -e KLANGKD_PREVENT_INSECURE_JWT_SECRET=1 \
  -e KLANGKD_DATA_DIR=/home/klangk/data \
  -e KLANGKD_PRODUCT_NAME="Acme Labs" \
  -e KLANGKD_LOGO_URL="/branding/logo.png" \
  -e KLANGKD_LLM_MODELS="openai/gpt-4o::your-api-key" \
  ghcr.io/mcdonc/klangk/klangk-host:latest
```

Or use `docker-compose.yml` — see the example in `customize/docker-compose.yml`.
