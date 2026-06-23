# OpenClaw sandbox example

Sandbox that installs [OpenClaw](https://github.com/openclaw/openclaw),
a personal AI assistant you run on your own devices.

Pre-configured to use the Klangk LLM proxy so openclaw can use any
model the server makes available. The workspace JWT is fetched
dynamically via an openclaw SecretRef exec provider.

## Usage

```bash
cd examples/openclaw
klangkc sandbox openclaw
```

First run installs Node.js 24 (via nvm), openclaw, writes a config
pointing at the Klangk LLM proxy, and runs a non-interactive onboard
that installs the gateway daemon. To add messaging channels afterward:

```bash
openclaw onboard
```

## What gets installed

- **nvm** — Node version manager (under `~/.nvm`)
- **Node.js 24** — runtime required by openclaw
- **openclaw** — installed globally via npm
- **klangk-secret-provider** — SecretRef exec provider that reads
  the workspace JWT via `klangk-workspace-token`
- **`~/.openclaw/openclaw.json`** — pre-seeded config using the
  Klangk LLM proxy with dynamic token auth
