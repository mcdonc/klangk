# Available Sandboxes

Klangk ships ready-to-use sandbox configurations that install and configure a
specific AI agent when the workspace is created. Each runs a setup script,
pre-configures the Klangk LLM proxy, and (for long-running agents) wires a
`default-command` and `health-check` so the agent starts with the workspace and
reports healthy.

For the **mechanism** — the `.klangk-sandbox.yaml` format, mounts, environment
variables, and setup scripts — see [Sandbox](../features/sandbox.md). The pages
below document the concrete sandboxes bundled with Klangk:

- [OpenClaw](openclaw.md) — a personal AI assistant exposed as a hosted app.
- [Hermes](hermes.md) — NousResearch's self-improving agent with a messaging gateway.
