# Klangk

Multi-User AI Sandboxing, Collaboration and Coding Platform

![Klangk Web Coding Agent](assets/screenshot.png)

Klangk is a container orchestration system that specializes in
sandboxing AI coding tasks.

**For solo developers:** AI agents like Pi and Claude Code are powerful
but intentionally given wide permissions — they read, write, and
execute code on your behalf. Klangk keeps them safely isolated: each
workspace is its own container where an agent can work freely without
risking your host system or other projects.

**For teams:** Klangk adds multi-user collaboration on top of
sandboxing. Share workspaces with teammates, pair-program through
shared terminals, chat alongside your AI agent, and control access
with per-user roles and permissions — all within the same isolated
containers.

## What You Can Do

1. **Sandbox a project** — [`klangkc sandbox`](features/sandbox.md) creates an isolated workspace from a `.klangk/sandbox.yaml` config, mounts your source code, and runs a setup script
2. **Run AI agents safely** — Pi and Claude Code run inside containers, isolated from your host
3. **Use the terminal** — access your container shell from the web browser or from your local terminal via [`klangkc shell`](reference/cli.md)
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
- **Podman**: Rootless container engine — each workspace runs in its own container with user namespace isolation, bind mounts, and named volumes. See [Podman](reference/podman.md) for details.
- **devenv**: Nix-based development environment with auto-setup, conditional build tasks (`execIfModified`), auto-reload disabled
