# Hermes agent — klangk workspace plugin

Bakes NousResearch's [Hermes agent](https://github.com/NousResearch/hermes-agent)
into a klangk workspace image and auto-configures it to use klangk's `llm-proxy`,
so the agent runs **key-free** — the container only ever holds the short-lived
workspace token; the real provider key stays on the host.

## Build your own workspace with Hermes

1. Add this plugin to your `plugins.yaml` (in `$KLANGK_PLUGINS_DIR`):

   ```yaml
   plugins:
     - name: hermes
       git: git@github.com:mcdonc/klangk.git # or your fork/repo hosting the plugin
       path: plugins/hermes
       ref: main
   ```

2. Fetch plugins and build the workspace image (klangk's build feature):

   ```bash
   update-plugins                 # fetch plugins listed in plugins.yaml
   build-workspace-image          # bakes the plugin's on-image-build.sh into the image
   ```

3. Open a workspace and run the agent:

   ```bash
   hermes -z "say hi"             # one-shot
   hermes                         # interactive
   ```

## How it works

- **`on-image-build.sh`** (build time, root): `apt`-installs ffmpeg + build tools,
  then runs the Hermes installer, which lays Hermes out under the FHS
  (`/usr/local/bin/hermes`, `/usr/local/lib/hermes-agent`) — on the system PATH
  and surviving klangk's runtime `/home` bind-mount (same idea as the
  `claude-code` plugin's `npm -g`).
- **`on-shell-init.sh`** (each shell): writes `~/.hermes/.env` +
  `~/.hermes/config.yaml` pointing Hermes at `$KLANGK_LLM_PROXY_URL` with the
  workspace token as the API key and `$KLANGK_LLM_MODEL` as the model.

## Choosing the model

The model is taken from `KLANGK_LLM_MODEL` — whatever your `llm-proxy` upstream
serves. A LiteLLM gateway, for example, can expose `MiniMax-M3`; set
`KLANGK_LLM_MODEL` accordingly (host `.env`).

> **Important:** the model is written to `config.yaml` as `model.model`, **not**
> the `HERMES_INFERENCE_MODEL` env var. The env var makes Hermes auto-detect the
> provider from the model _name_, which routes a name like `MiniMax-M3` to its
> real provider instead of the klangk proxy and fails with "Connection error".

## Notes

- Validated: Hermes installs via the build, runs as the `klangk` user, and
  reaches the model **key-free** through the klangk proxy (`hermes -z` returned
  correct answers on a LiteLLM-hosted model).
- **Reasoning models:** models that emit `<think>…</think>` / `reasoning_content`
  (e.g. `MiniMax-M3`) can leave Hermes's one-shot (`hermes -z`) with "no final
  response" when the answer lands only in the reasoning channel. Prefer a
  non-reasoning model for headless `-z` use, or handle reasoning output the way
  klangk's `minimax-thinking-tags` extension does for Pi.
- The image build needs container egress to pypi.org / files.pythonhosted.org /
  astral.sh / nousresearch.com. On a dev box with an egress firewall (e.g. Little
  Snitch), allow the container's outbound during the build.
