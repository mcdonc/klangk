# AI Coding Harnesses

Workspace containers ship with **Pi** pre-installed. **Claude Code**
is available via the `claude-code`
[plugin](plugins.md). Pi can connect to your LLM backend through the
[LLM proxy](../architecture/llm-proxy.md) so no API keys are exposed
inside containers.

## Prerequisites

Set these environment variables (in `.env` or your deployment config)
to enable AI features:

| Variable              | Example                     | Purpose                    |
| --------------------- | --------------------------- | -------------------------- |
| `KLANGK_LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `KLANGK_LLM_MODEL`    | `gpt-4o`                    | Default model name         |
| `KLANGK_LLM_API_KEY`  | `sk-...`                    | Provider API key           |

Without these, Pi and the Pi agent via the chat are non-functional. See
[Environment Variables](../reference/environment.md) for the full
list.

## Pi

[Pi](https://github.com/earendil-works/pi-coding-agent) is an
open-source terminal-based coding agent. It is the default harness
in klangk workspaces.

### Using Pi from the terminal

Open a terminal tab and run:

```text
pi
```

Pi starts in interactive TUI mode with access to the workspace
filesystem (`/home/work`), shell commands, and all mapped ports. By
default it uses the LLM proxy with the provider and model configured
via `KLANGK_LLM_BASE_URL`, `KLANGK_LLM_MODEL`, and
`KLANGK_LLM_API_KEY`. Its config is stored in `~/.pi/agent/` and
populated automatically at first login by klangk itself.

### Using Pi from chat

Mention the agent handle in the [Chat](chat.md) panel:

```text
@MrBoops create a Python Flask app on port 8000
```

The agent handle and email are set via environment variables and seeded
into the database on startup. After initial seeding, the agent identity
is read from the DB; changing the env vars updates the DB on next restart.

| Variable                   | Default               |
| -------------------------- | --------------------- |
| `KLANGK_CHAT_AGENT_HANDLE` | `MrBoops`             |
| `KLANGK_CHAT_AGENT_EMAIL`  | `MrBoops@example.com` |

The agent user cannot have a password and cannot log in via credentials.

When invoked from chat, Pi runs in RPC mode — the backend manages
the subprocess and streams responses back to the chat panel. See
[Chat - AI Agent](chat.md#ai-agent-mrboops) for details on
follow-up conversations and interjections.

### Pi extensions

The workspace image ships with several Pi extensions pre-installed:

- **pi-web-agent** — web-based agent UI
- **llm-proxy-models** — dynamically fetches available models from the
  LLM proxy
- **minimax-thinking-tags** — strips `<think>` tags from models that
  emit them

Extensions are installed at image build time into
`/opt/klangk/pi-agent/extensions/` and symlinked into the user's
`~/.pi/agent/` at first login. Users can install additional extensions
with `pi install`.

## Claude Code

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) is
Anthropic's CLI coding agent. It is not pre-installed — enable it by
adding the `claude-code` plugin to your `plugins.yaml`. See
[Plugins](plugins.md) for details.

### Using Claude Code from the terminal

Open a terminal tab and run:

```text
claude
```

Claude Code connects directly to the Anthropic API — it does not use
the LLM proxy. On first run, Claude Code prompts you to authenticate
via a browser-based flow: it displays a URL, you open it in your
browser, and paste the resulting API key back into the terminal.

## System prompt

Agents share a system prompt installed at `~/AGENTS.md` on first
login. This prompt configures workspace-specific behavior:

- File and project creation conventions
- Hosted app port mappings (`$KLANGK_PORT_MAPPINGS`)
- The `get_hosted_url` tool for generating user-facing URLs
- Guidelines for running servers, handling large files, and web search

The system prompt is copied from the image and can be edited per-user
in the container.

## How the LLM proxy works

No agent has direct access to your LLM API key. Instead:

1. `setup-clankers` writes `~/.pi/agent/models.json` with
   `KLANGK_LLM_PROXY_URL` (pointing to nginx on the host) and the
   workspace JWT as the API key.
2. When an agent makes an LLM request, nginx validates the JWT via
   `auth_request`, strips it, and forwards the request to
   `KLANGK_LLM_BASE_URL` with the real `KLANGK_LLM_API_KEY` in the
   `Authorization` header.

This means API keys never enter the container environment. See
[LLM Proxy](../architecture/llm-proxy.md) for the full architecture.
