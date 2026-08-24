# AI Coding Harnesses

Workspace containers ship with **Pi** pre-installed. Pi can connect to
your LLM backend through the [LLM proxy](../architecture/llm-proxy.md)
so no API keys are exposed inside containers.

## Prerequisites

Set these environment variables (in `.env` or your deployment config)
to enable AI features:

| Variable             | Example | Purpose                                                    |
| -------------------- | ------- | ---------------------------------------------------------- |
| `KLANGKD_LLM_MODELS` |         | Model list (see [LLM proxy](../architecture/llm-proxy.md)) |

Without these, Pi is non-functional. See
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

By default Pi uses the LLM proxy with the provider and model
configured via `KLANGKD_LLM_MODELS` and
`KLANGKD_LLM_API_KEY`. Its config is stored in `~/.pi/agent/` and
populated automatically at first login by klangk itself.

### Pi extensions

The workspace image ships with several Pi extensions pre-installed:

- **llm-proxy-models** — dynamically fetches available models from the
  LLM proxy
- **minimax-thinking-tags** — strips `<think>` tags from models that
  emit them

Extensions are installed at image build time into
`/opt/klangk/pi-agent/extensions/` and symlinked into the user's
`~/.pi/agent/` at first login. Users can install additional extensions
with `pi install`.

## System prompt

Agents share a system prompt installed at `~/AGENTS.md` on first
login. This prompt configures workspace-specific behavior:

- File and project creation conventions
- Hosted app port mappings (`$KLANGKWS_PORT_MAPPINGS`)
- The `get_hosted_url` tool for generating user-facing URLs
- Guidelines for running servers, handling large files, and web search

The system prompt is copied from the image and can be edited per-user
in the container.

## How the LLM proxy works

Pi does not have direct access to your LLM API key. Instead, klangk
configures Pi to send requests through the reverse proxy on the
host. The proxy forwards the request to the in-process litellm Router, which
injects the real `KLANGKD_LLM_API_KEY` in the `Authorization` header.

This means your LLM API key never enters the container environment.
See [LLM Proxy](../architecture/llm-proxy.md) for the full
architecture.
