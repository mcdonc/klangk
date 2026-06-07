# Klangk

![Klangk Web Coding Agent](docs/screenshot.png)

A container orchestration system powered by Docker, which specializes in sandboxing AI tasks using [Pi](https://pi.dev) and any OpenAI-compatible LLM provider.

Klangk gives its users isolated coding environments (aka "workspaces") using
Docker containers. Within each workspace, any task can be run, but special
consideration is given to LLM-focused tasks. Coding harnesses like `pi` and
`claude` are made available in each workspace.

## Quick Start

### Prerequisites

- Docker daemon running
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

# Start the app (builds Docker image and Flutter web on first run)
# Make sure Docker is running before this step
devenv processes up --no-tui
```

Open [http://localhost:8995](http://localhost:8995) and log in with `admin@example.com` (or whatever you set `KLANGK_DEFAULT_USER` to). If you set `KLANGK_DEFAULT_PASSWORD` in `.env`, use that password. Otherwise, check the server log output for the generated password. The default user is in the `admin` group and can manage other users and groups via the Admin page.

### Alternative: Docker Compose (no Nix)

If you'd rather not install Nix, the daemon ships as a self-contained Docker
Compose stack (nginx + a backend that runs rootless Podman):

```bash
cp -n .env.example .env && $EDITOR .env   # set KLANGK_LLM_API_KEY, KLANGK_JWT_SECRET, ...
docker compose up --build                 # log in at http://localhost:8995
```

Workspaces need the `klangk` image seeded into the stack first
(`scripts/dockerbuild.sh` then `scripts/seed-workspace-image.sh`). See the
[Docker Compose section in HACKING.md](HACKING.md#running-with-docker-compose-without-nix)
for host prerequisites and details.

### What You Can Do

1. **Create a workspace** — each workspace is an isolated coding environment
2. **Chat with the AI agent** — execute "pi" in the terminal, then ask it to write code, create projects, fix bugs
3. **Use the terminal** for direct shell access to the container (bash with tab completion and colors)
4. **View files** in the file viewer panel, drag-and-drop files or folders to upload, right-click to download, rename, or delete
5. **Share workspaces** with other users or groups, controlling access per-permission (terminal, files, chat, etc.)
6. **Monitor activity** in the debug panel
7. **Manage users and groups** (admin only) — add users, create groups, manage membership

### Security

Klangk uses an ACL (Access Control List) authorization system with fine-grained, per-resource permissions. Permissions are defined on a resource tree hierarchy and support allow/deny rules for individual users, groups, and system principals. See the [Authorization](HACKING.md#authorization-acl-system) section in HACKING.md for details.

## Environment Variables

See the [Environment Variables](HACKING.md#environment-variables) section in HACKING.md for the full list of configuration options.

## For More Information

See [HACKING.md](HACKING.md) for development setup, and [ARCHITECTURE.md](ARCHITECTURE.md) for detailed architecture documentation.
