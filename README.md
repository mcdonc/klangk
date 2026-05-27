# Bark

![Bark Web Coding Agent](docs/screenshot.png)

A multi-user web coding agent powered by [Pi](https://pi.dev) and any OpenAI-compatible LLM provider.

Bark gives each user their own isolated coding environment (a "workspace") using a Docker container. `pi` and other tools can be run within a workspace.

## Quick Start

### Prerequisites

- Docker daemon running
- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/) installed (or run `./bootstrap` to install both)
- An OpenAI-compatible LLM provider (e.g., [Ollama Cloud](https://ollama.com) or self-hosted Ollama)

### Setup

```bash
git clone git@github.com:mcdonc/bark.git
cd bark

# Create .env with your LLM provider credentials
cat > .env << 'EOF'
# LLM configuration (any OpenAI-compatible provider)
LLM_API_KEY=your-api-key-here
LLM_BASE_URL=https://ollama.com/v1          # or http://localhost:11434/v1 for self-hosted
LLM_MODEL=gemma4:31b                        # any model available on your provider

# Bark configuration
BARK_JWT_SECRET=change-this-to-a-random-secret
BARK_DEFAULT_USER=admin@example.com
# BARK_DEFAULT_PASSWORD=admin  # omit to generate a random password on first run

EOF

# Install Nix and devenv (if not already installed)
./bootstrap

# Start the app (builds Docker image and Flutter web on first run)
# Make sure Docker is running before this step
devenv processes up
```

Open [http://localhost:8995](http://localhost:8995) and log in with `admin@example.com` (or whatever you set `BARK_DEFAULT_USER` to). If you set `BARK_DEFAULT_PASSWORD` in `.env`, use that password. Otherwise, check the server log output for the generated password. The default user has the admin role and can manage other users at `/admin/users`.

### What You Can Do

1. **Create a workspace** — each workspace is an isolated coding environment
2. **Chat with the AI agent** — execute "pi" in the terminal, then ask it to write code, create projects, fix bugs
3. **Use the terminal** for direct shell access to the container (bash with tab completion and colors)
4. **View files** in the file viewer panel, drag-and-drop files or folders to upload, right-click to download, rename, or delete
5. **Monitor activity** in the debug panel
6. **Manage users** (admin only) — add, edit, delete users and toggle admin roles

### CLI Access

Bark also provides a CLI for terminal-based access to the same containers:

```bash
bark login admin@example.com        # authenticate (prompts for password)
bark ws list                         # list workspaces
bark ws create my-project            # create a workspace
bark ws shell my-project             # drop into bash inside the container
bark ws exec my-project ls /work     # run a command in the container
bark ws sync ~/src my-project:/work  # sync files to/from the container
bark ws delete my-project            # delete a workspace
```

The CLI connects to the running Bark backend over HTTP + WebSocket — it works locally and against remote servers. See [CLI.md](CLI.md) for the full CLI reference and roadmap.

### Environment Variables

All settings can be overridden in `.env`. Defaults are provided in `devenv.nix` at low priority so `.env` values take precedence.

| Variable                   | Default                      | Description                                                        |
| -------------------------- | ---------------------------- | ------------------------------------------------------------------ |
| `BARK_NGINX_PORT`          | `8995`                       | **Primary access point** — nginx (UI, API, WebSocket, hosted apps) |
| `BARK_PORT`                | `8997`                       | Backend (FastAPI/uvicorn) — proxied through nginx                  |
| `BARK_DATA_DIR`            | `$DEVENV_STATE/bark/data`    | Database, workspaces, Pi sessions                                  |
| `BARK_PLUGINS_DIR`         | `$DEVENV_STATE/bark/plugins` | Fetched plugins (outside repo for `execIfModified`)                |
| `SOLIPLEX_URL`             | (empty)                      | Soliplex base URL as seen by browser (empty = same origin)         |
| `LLM_API_KEY`              |                              | LLM provider API key                                               |
| `LLM_BASE_URL`             |                              | LLM API URL (any OpenAI-compatible provider)                       |
| `LLM_MODEL`                |                              | LLM model name                                                     |
| `BARK_JWT_SECRET`          |                              | JWT signing secret                                                 |
| `BARK_DEFAULT_USER`        |                              | Auto-seeded admin email on startup                                 |
| `BARK_DEFAULT_PASSWORD`    |                              | Auto-seeded password on startup (omit to generate random)          |
| `BARK_MIN_PASSWORD_LENGTH` | `4`                          | Minimum password length                                            |

### Ports

- `BARK_NGINX_PORT` (default `8995`): **Primary access point** — nginx serves UI, API, WebSocket, and proxies hosted app URLs directly to container ports
- `BARK_PORT` (default `8997`): Backend (FastAPI/uvicorn)
- `9000+`: User app ports (5 per workspace, mapped to container ports 8000-8004)

### Rebuilding

The devenv environment rebuilds necessary components at `devenv processes up` time.

To force-rebuild the Docker image and Flutter web app:

```bash
devenv shell -- rebuild
```

Then restart the processes.

## Architecture

```text
Browser (Flutter Web)
    ↕ WebSocket (terminal I/O, exec, browser bridge, lifecycle events)
nginx reverse proxy (port 8995)
    ├── /hosted/ → container ports (direct proxy)
    └── /        → FastAPI backend (port 8997)
                     ↕ docker exec
                 Pi coding agent (Docker container)
                     ↕ bind mount
                 Workspace files on disk
```

- **Frontend**: Flutter Web with terminal, file viewer, browser delegate for plugin actions, debug panel, admin user management
- **Backend**: nginx reverse proxy + FastAPI serving API, WebSocket, and frontend static files. Role-based access control with JWT roles claim
- **Agent**: Pi coding agent in interactive terminal mode with any OpenAI-compatible LLM provider

### Plugins

Plugins are fetched from git repos into `$BARK_PLUGINS_DIR` at development
time. Run `update-plugins` to set up:

```bash
devenv shell -- update-plugins           # creates $BARK_PLUGINS_DIR/plugins.yaml on first run
# edit $BARK_PLUGINS_DIR/plugins.yaml to add/remove plugins
devenv shell -- update-plugins           # fetches all plugins
devenv shell -- update-plugins soliplex  # fetch/update a single plugin
devenv up                                # builds and starts
```

Sample plugins (celebrate, beep, pig-latin, word-count) are included in the generated template. Sample plugin source lives in `plugins/` in this repo.

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed architecture and feature documentation.

## License

TBD
