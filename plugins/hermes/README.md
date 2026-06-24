# @klangk/hermes

Installs the [Hermes agent](https://github.com/NousResearch/hermes-agent) (NousResearch) into workspace containers.

## What it does

- **on-image-build.sh** — Installs ffmpeg and the Hermes CLI (pinned to v2026.6.19) at image build time. Uses `--skip-browser` (no Playwright/Chromium) and `--skip-setup` (no interactive prompts).
- **on-shell-init.sh** — Configures Hermes to use klangk's llm-proxy on every shell open. Refreshes the workspace token and writes `config.yaml` on first run.

## Configuration

| Variable                      | Default | Description                                                                                                                                                             |
| ----------------------------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `KLANGK_HERMES_USE_LLM_PROXY` | `true`  | When truthy (`true` or `1`), configures Hermes to route inference through klangk's llm-proxy. Set to `false` or `0` to let users configure Hermes credentials manually. |

When `KLANGK_HERMES_USE_LLM_PROXY` is enabled, the plugin:

1. Sets `OPENAI_BASE_URL` and `OPENAI_API_KEY` in `~/.hermes/.env` (other keys in that file are preserved)
2. Writes `~/.hermes/config.yaml` with `provider: custom` pointing at the proxy (only on first shell open)

When disabled, Hermes is still installed but no proxy credentials are injected — users can run `hermes setup` to configure it themselves.
