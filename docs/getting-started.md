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
  -e KLANGK_DEFAULT_USER=you@example.com \
  -e KLANGK_DEFAULT_PASSWORD=changeme \
  -e KLANGK_JWT_SECRET=$(openssl rand -hex 32) \
  -e KLANGK_LLM_BASE_URL=https://ollama.com/v1 \
  -e KLANGK_LLM_API_KEY=your-api-key \
  -e KLANGK_LLM_MODEL=gemma4:31b \
  ghcr.io/mcdonc/klangk/klangk-host:v2026.06.10
```

Open <http://localhost:8995> and log in with the email and password
you set above.

See [Running with Docker](deployment/docker.md) for details on the
Docker flags, docker-compose, persistence, and updating.

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
KLANGK_LLM_API_KEY=your-api-key-here
KLANGK_LLM_BASE_URL=https://ollama.com/v1
KLANGK_LLM_MODEL=gemma4:31b
KLANGK_JWT_SECRET=change-this-to-a-random-secret
KLANGK_DEFAULT_USER=admin@example.com
# Omit to generate a random password on first run
# KLANGK_DEFAULT_PASSWORD=admin
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
starts nginx and the FastAPI backend, and watches for file changes.
Open <http://localhost:8995>.

To run project commands like `test-backend` or
`build-workspace-image` in a separate terminal, use `devenv shell`
to enter the same environment.

!!! note "Podman policy errors"
If you see errors about missing container signatures or policies,
you may need to create a policy file. See
[Container Policy](reference/podman.md#container-policy) for
instructions.

## Logging In

Log in with the email you configured (`KLANGK_DEFAULT_USER`). If you
set `KLANGK_DEFAULT_PASSWORD`, use that password. Otherwise, check
the server log output for the generated password.

The default user is in the `admin` group and can manage other users
and groups via the Admin page.
