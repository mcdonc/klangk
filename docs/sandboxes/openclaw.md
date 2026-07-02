# OpenClaw sandbox example

Sandbox that installs [OpenClaw](https://github.com/openclaw/openclaw),
a personal AI assistant you run on your own devices.

Pre-configured to use the Klangk LLM proxy so openclaw can use any
model the server makes available. The workspace JWT is fetched
dynamically via an openclaw SecretRef exec provider.

The gateway starts automatically via `service-command` and the
container auto-starts with the server.

## Usage

> **WARNING:** OpenClaw will start in a mode where it is contactable by
> anyone who can contact your klangk server. Configure its authentication
> immediately after installation if your server answers on a public network.

```bash
cd sandboxes/openclaw
klangkc sandbox openclaw
```

First run installs Node.js 24 (via nvm), openclaw, writes a config
pointing at the Klangk LLM proxy, and runs a non-interactive onboard.
The setup script prints the hosted app URL at the end.

The gateway starts automatically in the Service terminal tab (it runs as
the workspace's agent identity in a dedicated `service` tmux session, not
in your own shell). To add messaging channels (i.e. to configure the
authentication the WARNING above refers to), run this in the Service
tab:

```bash
openclaw onboard
```

> Note: the [hermes sandbox](hermes.md#network-exposure) is the
> opposite case — its gateway is not exposed on an HTTP port and is not
> contactable by someone who can reach your klangk server.

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
