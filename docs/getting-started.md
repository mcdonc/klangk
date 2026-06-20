# Getting Started

This guide covers setting up Klangk for local, single-user development
on your own machine. For multi-user or team deployments, see
[Deployment](deployment/index.md).

## Prerequisites

- Linux or macOS
- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/) installed (run `./bootstrap` to install both)
- An OpenAI-compatible LLM provider (e.g., [Ollama Cloud](https://ollama.com) or self-hosted Ollama or LiteLLM instance)

## Setup

```bash
git clone git@github.com:mcdonc/klangk.git
cd klangk

# Create .env from the example
cp -n .env.example .env

# Edit .env with your credentials
cat > .env << 'EOF'
KLANGK_LLM_API_KEY=your-api-key-here
# Any OpenAI-compatible provider (or http://localhost:11434/v1 for self-hosted)
KLANGK_LLM_BASE_URL=https://ollama.com/v1
# Any model available on your provider
KLANGK_LLM_MODEL=gemma4:31b
KLANGK_JWT_SECRET=change-this-to-a-random-secret
KLANGK_DEFAULT_USER=admin@example.com
# Omit to generate a random password on first run
# KLANGK_DEFAULT_PASSWORD=admin
EOF

# Install Nix and devenv (if not already installed)
./bootstrap
```

## Starting the Dev Environment

```bash
devenv processes up --no-tui
```

This sets up the dev shell (Python, Flutter, Dart, Node, podman, etc.),
builds the workspace image and Flutter web app on first run, starts
nginx and the FastAPI backend, and watches for file changes. Open
[http://localhost:8995](http://localhost:8995).

To run project commands like `test-backend` or `build-workspace-image`
in a separate terminal, use `devenv shell` to enter the same
environment.

## Logging In

Log in with `admin@example.com` (or whatever you set
`KLANGK_DEFAULT_USER` to). If you set `KLANGK_DEFAULT_PASSWORD` in
`.env`, use that password. Otherwise, check the server log output for
the generated password.

The default user is in the `admin` group and can manage other users
and groups via the Admin page.
