# Getting Started

## Run Using Docker

The fastest way to evaluate or deploy Klangk. No build tools needed.
The published image may lag behind the latest development on main —
use devenv if you want the most up-to-date version.

You need Docker (or Podman) and an OpenAI-compatible LLM API key.

```bash
docker run -d \
  --name klangk \
  -p 8997:8997 \
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
  -e KLANGKD_LLM_MODELS="openai/gemma4:31b:https://ollama.com/v1:" \
  ghcr.io/mcdonc/klangk/klangk-host:v1.0
```

Open <http://localhost:8997> and log in with the email and password
you set above.

> A Docker container publishes its port (`-p 8997:8997`), making it
> network-reachable, so the published host image uses
> `KLANGKD_AUTH_MODES=password` — that is the supported configuration for
> the image. The default mode is `none` (no-login, loopback-only), which is
> **unsupported with the published Docker host image**: it is meant for
> local dev on your own machine, where the port is not published, and it
> freely issues an admin token with no password. For the no-login
> single-user experience, run klangk locally via devenv (below) instead.
> See [Auth Modes](features/auth-modes.md) and
> [#1391](https://github.com/mcdonc/klangk/issues/1391).

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

# Install Nix and devenv (if not already installed)
./bootstrap

# Enter the shell — the first entry seeds klangkd.yaml (gitignored)
# from klangkd.yaml.devenv: the dev config for the backend
devenv shell
```

Then edit `klangkd.yaml` to add your LLM provider (required for the AI
features — see [LLM Proxy](architecture/llm-proxy.md) for all forms):

```yaml
# klangkd.yaml (seeded on first shell entry)
llm-models:
  - model_name: "*" # single provider, all its models
    params:
      api-base: https://api.openai.com/v1 # or http://localhost:11434 (Ollama)
      api-key: your-key-here
```

The seeded dev config runs `auth_modes: password` with
`admin@example.com` / `admin123abc` — change them in `klangkd.yaml`
before first boot, or set a real password afterwards with
`klangk admin users set-password`.

### Starting the Dev Environment

```bash
devenv processes up --no-tui
```

This sets up the dev shell (Python, Flutter, Dart, Node, podman,
etc.), builds the workspace image and Flutter web app on first run,
starts the proxy and the FastAPI backend, and watches for file changes.
Open <http://localhost:8997>.

To run project commands like `test-backend` or
`build-workspace-image` in a separate terminal, use `devenv shell`
to enter the same environment.

!!! note "Podman policy errors"
If you see errors about missing container signatures or policies,
you may need to create a policy file. See
[Container Policy](reference/podman.md#container-policy) for
instructions.

## Logging in

With the Docker examples above (`KLANGKD_AUTH_MODES=password`) and the
seeded devenv config (also `password` mode), log in with the email you
configured (`admin@example.com` / `admin123abc` in dev) and the password
you set. The default user is in the `admin` group and can manage other
users and groups via the Admin page.

A bare `klangkd` (e.g. a `pip install klangk` install with no config) uses
the default `none` auth mode — there is **nothing to log in with**: open
the page and you're already in, as the default user
(`KLANGKD_DEFAULT_USER`). The CLI likewise needs no `klangk login`.

See [Auth Modes](features/auth-modes.md) for the full picture, including how
to switch modes.

> **Configuration file:** For production deployments, a YAML config file is
> recommended over env vars. See
> [Configuration File](reference/klangkd-config.md) for the full reference.
