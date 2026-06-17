# Klangk

![Klangk Web Coding Agent](docs/screenshot.png)

A container orchestration system powered by Podman, which specializes in sandboxing AI tasks.

Klangk gives its users isolated coding environments (aka "workspaces") using
containers. Within each workspace, any task can be run, but special
consideration is given to LLM-focused tasks. Coding harnesses like `pi` and
`claude` are made available in each workspace.

## Documentation

See the [full documentation](https://mcdonc.github.io/klangk/) for architecture, development, and deployment details.

## Quick Start

### Prerequisites

- macOS or Linux
- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/) installed (or run `./bootstrap` to install both)
- An OpenAI-compatible LLM provider (e.g., [Ollama Cloud](https://ollama.com) or self-hosted Ollama or LiteLLM instance)

### Setup

```bash
git clone git@github.com:mcdonc/klangk.git
cd klangk

# Create .env from the example (edit with your credentials)
# -n: don't overwrite if .env already exists
cp -n .env.example .env
$EDITOR .env
# set KLANGK_LLM_API_KEY, KLANGK_JWT_SECRET, etc.

# Install Nix and devenv (if not already installed)
./bootstrap

# Start the app
devenv processes up --no-tui
```

Open [http://localhost:8995](http://localhost:8995) and log in with `admin@example.com` (or whatever you set `KLANGK_DEFAULT_USER` to). If you set `KLANGK_DEFAULT_PASSWORD` in `.env`, use that password. Otherwise, check the server log output for the generated password. The default user is in the `admin` group and can manage other users and groups via the Admin page.

### What You Can Do

1. **Create a workspace** — each workspace is an isolated coding environment
2. **Chat with the AI agent** — execute `claude` or `pi` in the terminal, then ask it to write code, create projects, fix bugs
3. **Use the terminal** for direct shell access to the container (bash with tab completion and colors)
4. **View files** in the file viewer panel, drag-and-drop files or folders to upload, right-click to download, rename, or delete. Preview markdown, images, code (with syntax highlighting and editing), PDFs, video, and spreadsheets
5. **Chat with other workspace users** in shared workspaces
6. **Share workspaces** with other users or groups, controlling access per-permission (terminal, files, chat, etc.)
7. **Monitor activity** in the debug panel
8. **Manage users and groups** (admin only) — add users, create groups, manage membership

## Architecture

![Architecture Overview](docs/architecture-overview.svg)
foo
foo
foo
foo
