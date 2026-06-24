# Running the Hermes agent inside klangk (MiniMax via the llm-proxy)

NousResearch's **Hermes agent** runs inside klangk workspaces alongside Pi,
using **MiniMax** as the model — **without ever putting an API key in the
container**. Hermes is pointed at klangk's existing in-container `llm-proxy`,
exactly like Pi; the proxy injects the real key.

- Hermes: <https://hermes-agent.nousresearch.com/> · <https://github.com/NousResearch/hermes-agent> (MIT)
- Install: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`
- Config: `~/.hermes/config.yaml` (+ secrets in `~/.hermes/.env`)
- One-shot: `hermes -z "<prompt>"`; programmatic: `hermes chat -q "<prompt>" --quiet`

> **No keys in this doc or in the container.** The MiniMax key lives only in
> klangk's nginx `llm-proxy` (`KLANGK_LLM_API_KEY` / `.env` on the host). Hermes
> authenticates to the proxy with the **per-workspace token**, never the real key.

## How it works

Implemented as a plugin (`plugins/hermes/`) with two lifecycle hooks:

- **`on-image-build.sh`** — installs Hermes at image build time (pinned to
  `v2026.6.19`), so no runtime egress is needed.
- **`on-shell-init.sh`** — writes `~/.hermes/.env` and `~/.hermes/config.yaml`
  on every shell open, configuring Hermes to use the llm-proxy with the current
  workspace token. Skips if `KLANGK_LLM_PROXY_URL` is not set.

klangk already injects these into every workspace container:

| In-container value       | Source                                              | Meaning                                                                                        |
| ------------------------ | --------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `KLANGK_LLM_PROXY_URL`   | `http://host.containers.internal:<nginx>/llm-proxy` | OpenAI-compatible base URL; proxy forwards to `KLANGK_LLM_BASE_URL` with the real key injected |
| `KLANGK_LLM_MODEL`       | host env                                            | model id (for MiniMax, the MiniMax model id)                                                   |
| `klangk-workspace-token` | command → `/tmp/klangk/workspace-token`             | per-workspace bearer token the proxy validates                                                 |

## Verifying it works

```sh
# inside the workspace:
echo "$KLANGK_LLM_PROXY_URL"          # should be http://host.containers.internal:<nginx>/llm-proxy
klangk-workspace-token | head -c 12   # non-empty token
hermes -z "Say hi in 5 words."        # should return text via MiniMax through the proxy
```

## Caveats

- **Token rotation:** the workspace token changes on container restart —
  `on-shell-init.sh` rewrites `~/.hermes/.env` on every shell open.
- **MiniMax `<think>` tags:** klangk ships `minimax-thinking-tags.ts` for Pi.
  Hermes renders raw model text; if MiniMax emits literal `<think>…</think>`
  tags, confirm Hermes's display handles them.
- **Model id:** set `KLANGK_LLM_MODEL` (host `.env`) to the MiniMax model id;
  Hermes picks it up via `HERMES_INFERENCE_MODEL`. Per-run override:
  `hermes chat -m <model>`.
- **`hermes model` wizard:** skip it — it triggers provider OAuth/portal flows.
  The `.env` + `provider: custom` path is fully headless.
