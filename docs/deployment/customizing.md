# Customizing a Deployment

Klangk is deployed as a Docker container (the "[host container](https://github.com/mcdonc/klangk/pkgs/container/klangk%2Fklangk-host)") that packages the backend, nginx, Flutter web UI, and workspace image into a single image. The [`customize/`](https://github.com/mcdonc/klangk/tree/main/customize) directory in the Klangk repo is an example of how to build a custom version of this deployment container with plugins, custom CA certificates, and OIDC login hooks baked in. It is meant to be copied out of the repo and adapted to your own needs — edit the scripts, swap out the plugins, add your own login hooks, replace the placeholder logo, etc. Nothing in `customize/` is required to run Klangk; it's a starting point for organizations that want a tailored deployment.

## What Gets Customized

This example custom build layers your changes on top of the standard Klangk host image:

- **Plugins** — TypeScript extensions and Dart UI plugins are compiled into the workspace image and Flutter web frontend
- **CA certificates** — Private CA certs are installed into the system trust store of both the host and workspace images
- **Login hooks** — Custom Python hooks for OIDC login validation (e.g., require invitation)
- **Logo** — A placeholder `logo.png` demonstrates branding customization

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

| Variable              | Default                                    | Description                                                            |
| --------------------- | ------------------------------------------ | ---------------------------------------------------------------------- |
| `KLANGK_REF`          | `main`                                     | Klangk branch, tag, or commit SHA to build against                     |
| `KLANGK_REPO`         | `https://github.com/mcdonc/klangk.git`     | Klangk repo URL                                                        |
| `KLANGK_HOST_IMAGE`   | `ghcr.io/mcdonc/klangk/klangk-host-custom` | Output image name                                                      |
| `KLANGK_PLATFORM`     | `linux/amd64`                              | Target platform                                                        |
| `KLANGK_SSL_CERT_DIR` | `./ssl`                                    | Directory containing `.pem`/`.crt` CA certs to inject into both images |

## Plugins

Edit `plugins.yaml` to add or remove plugins. The default set includes the built-in plugins: celebrate, beep, pig-latin, word-count, browser-fetch, bobdobbs.

To add an external plugin:

```yaml
plugins:
  - name: my-plugin
    git: git@github.com:myorg/my-klangk-plugin.git
    ref: main
```

See the [Creating Plugins](../development/creating-plugins.md) reference for plugin structure details.

## Custom CA Certificates

Place `.pem` or `.crt` files in the `ssl/` directory (or set `KLANGK_SSL_CERT_DIR`). They are installed into the system CA store of both the host and workspace images. This is needed when services (e.g., Logfire, OIDC providers) use certificates signed by a private CA. The `ssl/` directory is gitignored — certs must be provided at build time.

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
