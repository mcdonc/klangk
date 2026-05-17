# Bark

![Bark Web Coding Agent](docs/screenshot.png)

A multi-user web coding agent powered by [Pi](https://pi.dev) and [Ollama](https://ollama.com) (cloud or self-hosted).

Bark gives each user their own isolated coding environment with an AI agent that can write, run, and test code directly. Each workspace runs in a Docker container with Python, Node.js, Dart, Flutter, Rust, and C/C++ available.

## Quick Start

### Prerequisites

- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/) installed (run `./bootstrap` to install both)
- Docker daemon running
- [Ollama](https://ollama.com) — either a Cloud account with API key, or a self-hosted instance

### Setup

```bash
git clone <repo-url> bark
cd bark

# Create .env with your Ollama API key
cat > .env << 'EOF'
# Ollama configuration
OLLAMA_API_KEY=your-api-key-here
OLLAMA_BASE_URL=https://ollama.com/v1       # or http://localhost:11434/v1 for self-hosted
OLLAMA_MODEL=gemma4:31b                     # any model available on your Ollama instance

# Bark configuration
BARK_JWT_SECRET=change-this-to-a-random-secret
BARK_DEFAULT_USER=admin
BARK_DEFAULT_PASSWORD=admin

EOF

# Install Nix and devenv (if not already installed)
./bootstrap

# Start the app (builds Docker image and Flutter web on first run)
# Make sure Docker is running before this step
devenv processes up
```

Open [http://localhost:8997](http://localhost:8997) and log in with `admin`/`admin`.

### What You Can Do

1. **Create a workspace** — each workspace is an isolated coding environment
2. **Chat with the AI agent** — ask it to write code, create projects, fix bugs
3. **The agent writes files directly** — no copy-paste needed
4. **The agent runs and tests code** — it has shell access inside the container
5. **View files** in the file viewer panel, drag-and-drop files or folders to upload, right-click to download, rename, or delete
6. **Use the terminal** for direct shell access to the container (bash with tab completion and colors)
7. **Monitor activity** in the debug panel

### Environment Variables

All settings can be overridden in `.env`. Defaults are provided in `devenv.nix` at low priority so `.env` values take precedence.

| Variable                | Default           | Description                                                |
| ----------------------- | ----------------- | ---------------------------------------------------------- |
| `BARK_PORT`             | `8997`            | Backend (FastAPI/uvicorn) port                             |
| `BARK_NGINX_PORT`       | `8995`            | nginx reverse proxy port                                   |
| `BARK_SOLIPLEX_PORT`    | `8555`            | Soliplex backend port (for nginx proxy)                    |
| `BARK_DATA_DIR`         | `~/.bark/data`    | Database, workspaces, Pi sessions                          |
| `BARK_PLUGINS_DIR`      | `~/.bark/plugins` | Fetched plugins (outside repo for `execIfModified`)        |
| `SOLIPLEX_URL`          | (empty)           | Soliplex base URL as seen by browser (empty = same origin) |
| `OLLAMA_API_KEY`        |                   | Ollama Cloud API key                                       |
| `OLLAMA_BASE_URL`       |                   | Ollama API URL (cloud or self-hosted)                      |
| `OLLAMA_MODEL`          |                   | LLM model name                                             |
| `BARK_JWT_SECRET`       |                   | JWT signing secret                                         |
| `BARK_DEFAULT_USER`     |                   | Auto-seeded user on startup                                |
| `BARK_DEFAULT_PASSWORD` |                   | Auto-seeded password on startup                            |

### Ports

- `BARK_PORT` (default `8997`): Web UI + API (single FastAPI/uvicorn server)
- `BARK_NGINX_PORT` (default `8995`): nginx reverse proxy
- `9000+`: User app ports (5 per workspace, mapped to container ports 8000-8004)

### Rebuilding

The devenv environment rebuilds necessary components at `devenv processes up` time.

To force-rebuild the Docker image and Flutter web app:

```bash
devenv shell -- rebuild
```

Then restart the processes.

## Architecture

```
Browser (Flutter Web)
    ↕ WebSocket (AG-UI protocol)
Python/FastAPI backend
    ↕ docker attach (JSON-RPC)
Pi coding agent (Docker container)
    ↕ bind mount
Workspace files on disk
```

- **Frontend**: Flutter Web with markdown rendering, syntax-highlighted code blocks, file viewer, container terminal, debug panel
- **Backend**: FastAPI serving both API and frontend static files on a single port
- **Agent**: Pi coding agent in RPC mode with Ollama (cloud or self-hosted, configurable model)
- **Protocol**: [AG-UI](https://docs.ag-ui.com/) for standardized agent-user communication

Each workspace gets its own Docker container with a bind-mounted directory. Pi sessions persist across container restarts (resumed automatically via `--session` flag), and conversation history is stored in SQLite. API keys are delivered via FIFO (named pipe) so they never persist on disk inside the container.

### Plugins

Plugins are fetched from git repos into `~/.bark/plugins` at development time. Run `update-plugins` to set up:

```bash
devenv shell -- update-plugins           # creates ~/.bark/plugins/plugins.yaml on first run
# edit ~/.bark/plugins/plugins.yaml to add/remove plugins
devenv shell -- update-plugins           # fetches all plugins
devenv shell -- update-plugins soliplex  # fetch/update a single plugin
devenv up                                # builds and starts
```

Sample plugins (celebrate, beep, pig-latin, word-count) are included in the generated template. Sample plugin source lives in `plugins/` in this repo.

Each plugin directory can contain:

| File           | Purpose                                                           |
| -------------- | ----------------------------------------------------------------- |
| `extension.ts` | Pi extension (TypeScript) — registered as an LLM-callable tool    |
| `plugin.dart`  | Dart plugin class — handles client-side execution and optional UI |
| `*.dart`       | Supporting Dart files (widgets, utilities)                        |
| `tools/`       | Server-side scripts copied into the Docker image                  |

**Client-side plugins** use Pi's Extension UI Sub-Protocol to delegate execution to the browser. This enables tools that need browser authentication (e.g., Soliplex cookies) or browser-native capabilities (audio, animations).

See [docs/PLAN.md](docs/PLAN.md) for detailed architecture and feature documentation.

## License

TBD
