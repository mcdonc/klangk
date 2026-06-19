# Klangk

Multi-User AI Collaboration and Coding Platform

![Klangk Web Coding Agent](assets/screenshot.png)

Klangk is a container orchestration system powered by Podman that
specializes in sandboxing AI coding tasks. AI agents like Pi and
Claude Code are powerful but intentionally given wide permissions —
they read, write, and execute code on your behalf. Klangk keeps
these agents safely isolated: each workspace is its own container
where an agent can work freely without risking your host system or
other projects.

Use [`klangkc sandbox`](features/sandbox.md) to spin up a sandboxed
environment from a project config file — mount your source code,
copy your dotfiles, install tools, and drop into a shell, all with
one command.

## What You Can Do

1. **Sandbox a project** — [`klangkc sandbox`](features/sandbox.md) creates an isolated workspace from a `.klangk/sandbox.yaml` config, mounts your source code, and runs a setup script
2. **Run AI agents safely** — Pi and Claude Code run inside containers, isolated from your host
3. **Use the terminal** for direct shell access to the container (bash with tab completion and colors)
4. **View files** in the file viewer panel, drag-and-drop files or folders to upload, right-click to download, rename, or delete. Preview markdown, images, code (with syntax highlighting and editing), PDFs, video, and spreadsheets
5. **Chat with other workspace users** in shared workspaces
6. **Share workspaces** with other users or groups, controlling access per-permission (terminal, files, chat, etc.)
7. **Monitor activity** in the debug panel
8. **Manage users and groups** (admin only) — add users, create groups, manage membership

## Architecture

![Architecture Overview](assets/architecture-overview.svg)

## Key Technologies

- **Pi Coding Agent**: Minimal terminal coding harness (pi.dev) running in interactive terminal mode with native session persistence and extension tools
- **LLM Provider**: Any OpenAI-compatible LLM provider (Ollama Cloud, self-hosted Ollama, etc.), configurable via env vars (`KLANGK_LLM_BASE_URL`, `KLANGK_LLM_MODEL`, `KLANGK_LLM_API_KEY`). The model must support tool/function calling — Pi uses tools (bash, edit, write, read) to interact with the workspace.
- **Pydantic Logfire**: AI observability — FastAPI auto-instrumentation via Logfire Python SDK (`LOGFIRE_TOKEN`). Pi agent tracing via [pi-otel-telemetry](https://github.com/mprokopov/pi-otel-telemetry) extension (OTLP export to Logfire) — requires `LOGFIRE_TOKEN` as a workspace env var and sourcing `. /opt/klangk/otel.sh` in the container shell (or `.bashrc`) to set the standard `OTEL_*` env vars.
- **devenv**: Nix-based development environment with auto-setup, conditional build tasks (`execIfModified`), auto-reload disabled

## Components

- **Backend** (`src/backend/`): Python/FastAPI — single-port server for API, WebSocket, and frontend static files
- **CLI** (`src/cli/klangkc/`): `klangkc` command — typer-based thin client that talks to the backend over HTTP + WebSocket for terminal access to containers
- **Frontend** (`src/frontend/`): Flutter Web — chat (markdown rendering, syntax-highlighted code blocks, @mentions, message types, pagination, history recall), file viewer, debug panel, workspace presence
- **Containers** (`src/containers/`): Custom Dockerfile for Pi agent containers with Python3, Node.js, build-essential, SQLite, vim, emacs, network tools, Pi extensions (built and run via podman)
