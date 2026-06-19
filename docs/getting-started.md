# Getting Started

## What is Klangk?

Klangk is a general-purpose container orchestration system with a
web-based multi-user interface. Each user gets isolated workspace
containers with a full terminal, file browser, and real-time chat.

AI coding agents like Pi and Claude Code are powerful but
intentionally given wide permissions — they can read, write, and
execute code on your behalf. Klangk's containerization keeps these
agents sandboxed: each workspace is an isolated container where an
agent can do its work without risking your host system, other
projects, or other users' environments.

The [`klangkc sandbox`](features/sandbox.md) command makes this
easy: check a config file into your repo that describes what the
workspace needs (mounts, tools, dotfiles), and spin up an isolated
environment.

- **Sandboxed AI agents** — Pi and Claude Code run inside
  containers, isolated from your host system
- **Project-level config** — `.klangk/sandbox.yaml` defines mounts,
  setup scripts, and dotfiles for reproducible environments
- **SSH agent forwarding** — use your local SSH keys inside
  containers without copying them
- **GitHub HTTPS authentication** — browser-based credential flow
  for git operations
- **Plugin system** — extend the AI agent and browser with
  TypeScript and Dart extensions
- **Collaborate with other users** — share workspaces, terminals,
  and chat in real time
- **ACL authorization** — fine-grained access control for
  multi-tenant deployments

## Prerequisites

- macOS or Linux
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
