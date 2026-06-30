# Hermes sandbox example

Sandbox that installs [Hermes Agent](https://github.com/NousResearch/hermes-agent)
— NousResearch's terminal-based AI assistant with a messaging gateway
(Telegram, Discord, Slack, WhatsApp, Signal) and a self-improving learning loop.

Pre-configured to route inference through the Klangk LLM proxy so hermes can
use any model the server makes available. The workspace JWT is refreshed into
`~/.hermes/.env` before every gateway start (the token rotates on each
container restart).

The gateway starts automatically via `default-command` and the container
auto-starts with the server. With no messaging platforms configured the
gateway idles for cron job execution rather than exiting, so the health check
still reports healthy.

## Usage

```bash
cd sandboxes/hermes
klangkc sandbox hermes
```

First run fetches and runs the upstream Hermes installer, writes a config
pointing at the Klangk LLM proxy, and installs the gateway wrapper. The
gateway starts automatically in the first terminal window.

To configure messaging platforms after setup:

```bash
hermes setup
```

## What gets installed

Everything installs to `/hermes` (the sandbox mount point):

- **Hermes Agent** — repo + virtualenv at `/hermes/hermes-agent`, command
  linked at `~/.local/bin/hermes` (pinned to a release branch; the version is
  the single source of truth in `setup.sh`)
- **`/hermes/config.yaml`** — routes inference through the Klangk LLM proxy
- **`/hermes/.env`** — `OPENAI_BASE_URL` / `OPENAI_API_KEY` for the proxy
  (token refreshed on every gateway start by the wrapper)
- **`/hermes/bin/klangk-hermes-gateway`** — `default-command` wrapper that
  refreshes the token then runs `hermes gateway run`

## Health check

`health-check` runs `hermes gateway status` and greps its output for the
running marker. `hermes gateway status` always exits 0 (it only prints state),
so the liveness signal is derived from the printed line rather than the exit
code — hermes's own process detection (PID file + `/proc` scan) does the work.
See [docs/features/health-check.md](../../docs/features/health-check.md).

## Why a sandbox, not a plugin

Hermes was previously a compile-time [plugin](../../docs/features/plugins.md)
baked into the image. Its installer spawns an interactive `bash -i` to probe
`PATH` — but only in the root/FHS-layout branch, which a sandbox (running
setup as the non-root `klangk` user) never takes. Converting it to a runtime
sandbox made the `/tmp/.klangk-image-build` bailout in `bash.bashrc` dead
code (now deleted) and lets each workspace configure hermes independently
without rebuilding the image. See issue #1109.
