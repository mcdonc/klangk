# Pi Extensions

Extensions are TypeScript files collected from `$KLANGK_PLUGINS_DIR/*/extension.ts` and staged at `$KLANGK_PLUGINS_DIR/.docker/extensions/` at build time (injected via named build contexts).

- The LLM sees them in its tool list alongside built-in tools (read, write, edit, bash)
- Extensions can be server-side (run code inside the container) or client-side (delegate to the browser via the [browser bridge](../architecture/browser-bridge.md))
- `AGENTS.md` is a system prompt copied to `$HOME` on first login that configures workspace-specific agent behavior and guidelines

## Built-in Plugins

- `word-count` — fast file stats (lines, words, characters, size) via Python script (server-side)
- `pig-latin` — text to Pig Latin converter, pure TypeScript (server-side)
- `celebrate` — triggers confetti animation in the browser (client-side, via browser bridge)
- `beep` — plays an audible beep tone via Web Audio API (client-side, via browser bridge)
- `browser-fetch` — authenticated HTTP fetch using browser session cookies (client-side, via browser bridge)
- `bobdobbs` — Bob "J.R." Dobbs quote generator (client-side, via browser bridge)

For details on creating plugins, managing them, and the build integration, see the [Plugin System](../reference/plugin-system.md) reference.
