# Getting Started

## Prerequisites

- macOS or Linux
- Podman (rootless) available
- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/) installed (or run `./bootstrap`)
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

## Entering the Dev Shell

All commands below assume you're inside the devenv shell:

```bash
devenv shell
```

This puts all project tools (Python, Flutter, Dart, Node, podman, etc.) on your PATH.

## Starting the Dev Environment

```bash
devenv processes up --no-tui
```

This builds the workspace image and Flutter web app on first run (via `execIfModified`), starts nginx, the FastAPI backend, and watches for file changes. Open [http://localhost:8995](http://localhost:8995).

Log in with `admin@example.com` (or whatever you set `KLANGK_DEFAULT_USER` to). If you set `KLANGK_DEFAULT_PASSWORD` in `.env`, use that password. Otherwise, check the server log output for the generated password. The default user is in the `admin` group and can manage other users and groups via the Admin page.
