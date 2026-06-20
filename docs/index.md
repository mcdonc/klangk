# Klangk

Multi-User AI Sandboxing, Collaboration and Coding Platform

![Klangk workspace with terminal and chat](assets/screenshot.png)

## Why Klangk?

AI coding agents like Pi and Claude Code are powerful — they read,
write, and execute code on your behalf. But running them directly on
your machine means trusting them with everything: your files, your
credentials, your system.

Klangk isolates each project in its own container. Agents work freely
inside; your host stays safe. Add multi-user collaboration on top and
you get a platform where teams and AI work together in sandboxed
workspaces.

## Core Features

### Sandboxed Workspaces

Every workspace is a rootless Podman container with its own filesystem,
terminal sessions, and network ports. Mount your source code in,
run agents, and tear it down when you're done.

- Create from the web UI, the [CLI](reference/cli.md), or a
  [sandbox config](features/sandbox.md) file
- Bind-mount host directories or use named volumes for persistent data
- Run any container image — bring your own toolchains
- [Hosted apps](features/hosted-apps.md) map container ports to
  public URLs for web dev previews

### AI Agent Integration

Pi and Claude Code run inside workspace containers with full terminal
access. The built-in chat agent ([MrBoops](features/chat.md)) can
answer questions, run commands, and edit files — all confined to the
workspace.

- Any OpenAI-compatible LLM provider (Ollama, cloud APIs, self-hosted)
- Agent responses stream into chat alongside human conversation
- [Pi extensions](features/ai-coding-harnesses.md) for browser
  automation, MCP tools, and more

### Multi-User Collaboration

Share workspaces with teammates. Everyone gets their own home directory
inside the container, and shared terminals let you pair-program in
real time.

- [Role-based access](features/authorization.md): owners, coders,
  collaborators, spectators
- [Shared terminals](features/terminal.md) with live input for
  pair programming
- [Chat](features/chat.md) with @mentions and message history
- [File viewer](features/file-viewer.md) with drag-and-drop upload,
  preview for code, markdown, images, PDFs, video, and spreadsheets

### Terminal

Terminals run inside [tmux](features/the-shell.md) for session
persistence and window management. Access them from the web UI or
connect from your local terminal with
[`klangkc shell`](reference/cli.md).

### Administration

- [User and group management](features/admin-management.md)
- [Per-resource ACLs](features/authorization.md)
- [OIDC single sign-on](reference/oidc.md) (Google, GitHub, etc.)
- [Email invitations](features/invitations.md)

## Quick Start

Install the CLI:

```bash
pip install klangkc
```

Or run from the dev environment:

```bash
git clone https://github.com/mcdonc/klangk.git
cd klangk
devenv up
```

Then open <http://localhost:8995> in your browser.

## Architecture

![Architecture Overview](assets/architecture-overview.svg)

Klangk is a FastAPI backend serving a Flutter Web frontend, with
rootless Podman managing workspace containers. An nginx reverse proxy
ties it together. See the [architecture docs](architecture/index.md)
for details.
