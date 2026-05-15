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
5. **View files** in the file viewer panel, drag-and-drop to upload
6. **Monitor activity** in the debug panel

### Ports

| Port | Service |
|------|---------|
| 8997 | Web UI + API (single FastAPI server) |
| 9000+ | User app ports (5 per workspace) |

### Rebuilding

To force rebuild the Docker image and Flutter web app:

```bash
devenv shell -- rebuild
```

Then restart the processes. On normal startup, Flutter and Docker builds run automatically when their source files have changed (via devenv `execIfModified` content hashing).

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
- **Soliplex**: Optional RAG knowledge base integration via client-side tools

Each workspace gets its own Docker container with a bind-mounted directory. Pi sessions persist across container restarts, and conversation history is stored in SQLite.

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

By defaul, plugins will be placed in `~/.bark/plugins`.  To change the plugins directory using devenv, create `devenv.local.nix`
(gitignored):

```nix
{ lib, ... }: {
  env.BARK_PLUGINS_DIR = lib.mkForce "/path/to/my/plugins";
}
```

If you aren't using devenv, just set the environment variable before running `update-plugins`.

Each plugin directory can contain:

| File | Purpose |
|------|---------|
| `extension.ts` | Pi extension (TypeScript) — registered as an LLM-callable tool |
| `plugin.dart` | Dart plugin class — handles client-side execution and optional UI |
| `*.dart` | Supporting Dart files (widgets, utilities) |
| `tools/` | Server-side scripts copied into the Docker image |

**Client-side plugins** use Pi's Extension UI Sub-Protocol to delegate execution to the browser. This enables tools that need browser authentication (e.g., Soliplex cookies) or browser-native capabilities (audio, animations).

See [PLAN.md](PLAN.md) for detailed architecture and feature documentation.

## License

TBD
