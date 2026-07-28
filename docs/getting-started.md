# Getting Started

## Run Using Docker

The fastest way to evaluate or deploy Klangk. No build tools needed.
The published image may lag behind the latest development on main —
use devenv if you want the most up-to-date version.

You need Docker (or Podman) and an OpenAI-compatible LLM API key.

```bash
docker run -d \
  --name klangk \
  -p 8995:8995 \
  -v klangk-data:/home/klangk/data \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  -e KLANGKD_DEFAULT_USER=you@example.com \
  -e KLANGKD_DEFAULT_PASSWORD=changeme \
  -e KLANGKD_AUTH_MODES=password \
  -e KLANGKD_JWT_SECRET=$(openssl rand -hex 32) \
  -e KLANGKD_LLM_BASE_URL=https://ollama.com/v1 \
  -e KLANGKD_LLM_API_KEY=your-api-key \
  -e KLANGKD_LLM_MODEL=gemma4:31b \
  ghcr.io/mcdonc/klangk/klangk-host:v1.0
```

Open <http://localhost:8995> and log in with the email and password
you set above.

> A Docker container publishes its port (`-p 8995:8995`), making it
> network-reachable, so these examples set `KLANGKD_AUTH_MODES=password`
> explicitly. The default mode is `none` (no-login, loopback-only), which is
> meant for local dev on your own machine — a published port is not that.
> See [Auth Modes](features/auth-modes.md).

## Run Using Nix

Install and run Klangk on bare metal (no Docker) with **every**
dependency — the klangk Python package itself, its Python deps,
and the system binaries it shells out to — provided by Nix. Works
on Linux and macOS.

You need [Nix](https://nixos.org/download/) (with flakes enabled) and
an OpenAI-compatible LLM API key.

```bash
nix run github:mcdonc/klangk
```

The flake builds klangk from source via Nix's Python tooling and
resolves all Python dependencies (fastapi, sqlalchemy, textual, …)
from nixpkgs — there is no virtualenv and no `pip install`. Nix also
provides every system-level dependency (podman, caddy, tmux, git,
GNU tar, etc.). Once built, `nix run` needs no network access.

!!! note "No bundled UI"
The flake build skips the compiled Flutter web UI (the release
PyPI wheel ships it; the flake source doesn't carry the gitignored
build artifact). To serve the UI, build the frontend separately and
point `KLANGKD_FRONTEND_DIR` at it, or `pip install klangk` from
PyPI for the all-in-one wheel.

To run the client:

```bash
nix run github:mcdonc/klangk#klangk
```

To drop into a shell with all deps on `PATH` (useful for an editable
`pip install -e src/klangk` or running `klangkd` directly):

```bash
nix develop github:mcdonc/klangk
```

!!! note "Podman setup"
On **macOS**, podman runs inside a VM. You need to run
`podman machine init && podman machine start` once before starting
klangkd. On **Linux**, rootless podman requires subuid/subgid
mappings for your user — see [Podman](reference/podman.md) for
details.

## Run Using devenv

For developing or modifying Klangk itself.

You need Linux or macOS,
[Nix](https://nixos.org/download/) with
[devenv](https://devenv.sh/) (run `./bootstrap` to install both),
and an OpenAI-compatible LLM API key.

### Setup

```bash
git clone git@github.com:mcdonc/klangk.git
cd klangk

# Create .env from the example
cp -n .env.example .env

# Edit .env with your credentials
cat > .env << 'EOF'
KLANGKD_LLM_API_KEY=your-api-key-here
KLANGKD_LLM_BASE_URL=https://ollama.com/v1
KLANGKD_LLM_MODEL=gemma4:31b
KLANGKD_JWT_SECRET=change-this-to-a-random-secret
KLANGKD_DEFAULT_USER=admin@example.com
# The default auth mode is `none` (no password, loopback-only) — you're
# logged in automatically as the default user above. To require a real
# password instead, uncomment the next two lines:
# KLANGKD_AUTH_MODES=password
# KLANGKD_DEFAULT_PASSWORD=admin
EOF

# Install Nix and devenv (if not already installed)
./bootstrap
```

### Starting the Dev Environment

```bash
devenv processes up --no-tui
```

This sets up the dev shell (Python, Flutter, Dart, Node, podman,
etc.), builds the workspace image and Flutter web app on first run,
starts the proxy and the FastAPI backend, and watches for file changes.
Open <http://localhost:8995>.

To run project commands like `test-backend` or
`build-workspace-image` in a separate terminal, use `devenv shell`
to enter the same environment.

!!! note "Podman policy errors"
If you see errors about missing container signatures or policies,
you may need to create a policy file. See
[Container Policy](reference/podman.md#container-policy) for
instructions.

## Logging in

Out of the box (the default `none` auth mode) there is **nothing to log in
with** — open <http://localhost:8995> and you're already in, as the default
user (`KLANGKD_DEFAULT_USER`). The CLI likewise needs no `klangk login`.

If you switched to a real auth mode (`password`, `oidc`, or `both` — e.g. the
Docker examples above set `KLANGKD_AUTH_MODES=password`), log in with the
email you configured. If you set `KLANGKD_DEFAULT_PASSWORD`, use that
password; otherwise check the server log for the generated one. The default
user is in the `admin` group and can manage other users and groups via the
Admin page.

See [Auth Modes](features/auth-modes.md) for the full picture, including how
to switch modes.

> **Configuration file:** For production deployments, a YAML config file is
> recommended over env vars. See
> [Configuration File](reference/klangkd-config.md) for the full reference.
