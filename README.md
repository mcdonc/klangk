# Bark

![Bark Web Coding Agent](docs/screenshot.png)

A multi-user web coding agent powered by [Pi](https://pi.dev) and [Ollama](https://ollama.com).

Bark gives each user their own isolated coding environment with an AI agent that can write, run, and test code directly. Each workspace runs in a Docker container with Python, Node.js, Dart, Flutter, Rust, and C/C++ available.

## Quick Start

### Prerequisites

- [Nix](https://nixos.org/download/) with [devenv](https://devenv.sh/) installed (run `./bootstrap` to install both)
- Docker daemon running
- [Ollama Cloud](https://ollama.com) account with an API key

### Setup

```bash
git clone <repo-url> bark
cd bark

# Create .env with your Ollama API key
cat > .env << 'EOF'
OLLAMA_API_KEY=your-api-key-here
BARK_JWT_SECRET=change-this-to-a-random-secret
BARK_DEFAULT_USER=admin
BARK_DEFAULT_PASSWORD=admin
EOF

# Install Nix and devenv (if not already installed)
./bootstrap

# Enter dev environment (installs deps, builds Docker image)
devenv shell

# Start the app
devenv up
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
| 8996 | Backend API |
| 8997 | Web UI |
| 9000+ | User app ports (5 per workspace) |

### Rebuilding

After code changes:

```bash
rebuild
```

This rebuilds both the Docker image and the Flutter web app.

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
- **Backend**: FastAPI with WebSocket, JWT auth, SQLite, Docker container management
- **Agent**: Pi coding agent in RPC mode with Ollama cloud LLM (Gemma 4 31B)
- **Protocol**: [AG-UI](https://docs.ag-ui.com/) for standardized agent-user communication

Each workspace gets its own Docker container with a bind-mounted directory. Pi sessions persist across container restarts, and conversation history is stored in SQLite.

See [PLAN.md](PLAN.md) for detailed architecture and feature documentation.

## License

TBD
