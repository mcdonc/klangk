# OpenClaw sandbox example

Sandbox that installs [OpenClaw](https://github.com/openclaw/openclaw),
a personal AI assistant you run on your own devices.

Pre-configured to use the Klangk LLM proxy so openclaw can use any
model the server makes available. The workspace JWT is fetched
dynamically via an openclaw SecretRef exec provider.

The gateway starts automatically via `default-command` and the
container auto-starts with the server.

## Usage

```bash
cd sandboxes/openclaw
klangkc sandbox openclaw
```

First run installs Node.js 24 (via nvm), openclaw, writes a config
pointing at the Klangk LLM proxy, and runs a non-interactive onboard.
The setup script prints the hosted app URL at the end.

The gateway starts automatically in the first terminal window. To
add messaging channels:

```bash
openclaw onboard
```

## What gets installed

Everything installs to `/openclaw` (the sandbox mount point), not
the user's home directory:

- **nvm** — Node version manager (under `/openclaw/.nvm`)
- **Node.js 24** — runtime required by openclaw
- **openclaw** — installed globally via npm
- **klangk-secret-provider** — SecretRef exec provider that reads
  the workspace JWT via `klangk-workspace-token` (`/openclaw/bin/`)
- **`/openclaw/.openclaw/openclaw.json`** — pre-seeded config using
  the Klangk LLM proxy with dynamic token auth, gateway bound to
  container port 8000 for hosted app access
