# Getting Started

## Run Using Docker

The fastest way to evaluate or deploy Klangk. No build tools needed.
The published image may lag behind the latest development on main —
use devenv if you want the most up-to-date version.

You need Docker (or Podman) and an OpenAI-compatible LLM API key.

klangkd reads its settings from a `klangkd.yaml` the container keeps
at `/home/klangk/etc/klangkd.yaml`. Save this file next to your
terminal, replacing the secret, the credentials, and the LLM provider
values:

```yaml
# klangkd.yaml
# --- Deployment settings ---
auth_modes: password
# Generate your own: openssl rand -hex 32
jwt_secret: paste-the-openssl-output-here
default_user: you@example.com # first-boot admin identity
default_password: changeme # that identity's password
# LLM provider — see LLM Proxy for all forms
llm-models:
  - model_name: "*" # single provider, all its models
    params:
      api-base: https://api.openai.com/v1 # or http://localhost:11434 (Ollama)
      api-key: your-key-here

# --- Structural settings ---
# The container mount below replaces the config file baked into the
# image, so keep these values: they point klangkd at the image's data
# volume, the embedded workspace image, and its version file.
port: 8997
listen: 0.0.0.0
egress_port: 8995
data_dir: /home/klangk/data
customize_dir: /home/klangk/custom
image_name: klangk-workspace
version_file: /home/klangk/version.json
state_dir: /tmp/klangk-state
```

Then run the container, mounting your config over the image's copy:

```bash
docker run -d \
  --name klangk \
  -p 8997:8997 \
  -v klangk-data:/home/klangk/data \
  -v ./klangkd.yaml:/home/klangk/etc/klangkd.yaml:ro \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  ghcr.io/mcdonc/klangk/klangk-host:v1.0
```

Open <http://localhost:8997> and log in with the `default_user` /
`default_password` you set above.

> A Docker container publishes its port (`-p 8997:8997`), making it
> network-reachable, so the quick-start config sets
> `auth_modes: password` — that is the supported configuration for
> the image. The default mode is `none` (no-login, loopback-only), which is
> **unsupported with the published Docker host image**: it is meant for
> local dev on your own machine, where the port is not published, and it
> freely issues an admin token with no password. For the no-login
> single-user experience, run klangk locally via devenv (below) instead.
> See [Auth Modes](features/auth-modes.md).

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

The seeded dev config runs `auth_modes: password`. Two keys set the
first-boot admin identity: `default_user` (the admin's email) and
`default_password` (that identity's password) — the seed sets
`admin@example.com` / `admin123abc`. Change both in `klangkd.yaml`
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

With the Docker examples above (`auth_modes: password` in your
`klangkd.yaml`) and the seeded devenv config (also `password` mode),
log in with the email you configured (`admin@example.com` /
`admin123abc` in dev) and the password you set. The default user is in
the `admins` group and can manage other users and groups via the Admin
page.

A bare `klangkd` (e.g. a `pip install klangk` install with no config) uses
the default `none` auth mode — there is **nothing to log in with**: open
the page and you're already in, as the default user (`default_user`,
which defaults to `<unixuser>@example.com`). The CLI likewise needs no
`klangk login`.

See [Auth Modes](features/auth-modes.md) for the full picture, including how
to switch modes.

> **Configuration file:** Every `klangkd.yaml` key also has a
> `KLANGKD_*` environment-variable form, and an env var overrides the
> file — handy for one-off tweaks without rewriting the config. See
> [Configuration File](reference/klangkd-config.md) for the full
> reference.
