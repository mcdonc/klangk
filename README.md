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

# Start the app (builds Docker image on first run)
# Make sure Docker is running before this step
devenv processes up
```

Open [http://localhost:8997](http://localhost:8997) and log in with `admin`/`admin`.

### What You Can Do

1. **Create a workspace** — each workspace is an isolated coding environment
2. **Chat with the AI agent** — ask it to write code, create projects, fix bugs
3. **The agent writes files directly** — no copy-paste needed
4. **The agent runs and tests code** — it has shell access inside the container
5. **View files** in the file viewer panel, drag-and-drop to upload
6. **Monitor activity** in the debug panel

### Ports

| Port | Service |
|------|---------|
| 8997 | Web UI + API (single FastAPI server) |
| 9000+ | User app ports (5 per workspace) |

### Rebuilding

After code changes:

```bash
devenv shell -- rebuild
devenv processes restart
```

This rebuilds both the Docker image and the Flutter web app, then restarts the services.

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

- **Frontend**: Flutter Web with markdown rendering, syntax-highlighted code blocks, file viewer, debug panel
- **Backend**: FastAPI serving both API and frontend static files on a single port
- **Agent**: Pi coding agent in RPC mode with Ollama (cloud or self-hosted, configurable model)
- **Protocol**: [AG-UI](https://docs.ag-ui.com/) for standardized agent-user communication

Each workspace gets its own Docker container with a bind-mounted directory. Pi sessions persist across container restarts, and conversation history is stored in SQLite.

See [PLAN.md](PLAN.md) for detailed architecture and feature documentation.

## License

TBD
