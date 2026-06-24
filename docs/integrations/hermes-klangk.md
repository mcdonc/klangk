# Running the Hermes agent inside klangk (MiniMax via the llm-proxy)

Packaged for review by @mcdonc. Goal: run NousResearch's **Hermes agent** inside a
klangk workspace, alongside Pi, using **MiniMax** as the model — **without ever
putting an API key in the container**. Hermes is pointed at klangk's existing
in-container `llm-proxy`, exactly like Pi is; the proxy injects the real key.

- Hermes: <https://hermes-agent.nousresearch.com/> · <https://github.com/NousResearch/hermes-agent> (MIT)
- Install: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`
- Config: `~/.hermes/config.yaml` (+ secrets in `~/.hermes/.env`)
- One-shot: `hermes -z "<prompt>"`; programmatic: `hermes chat -q "<prompt>" --quiet`

> ⚠️ **No keys in this doc or in the container.** The MiniMax key lives only in
> klangk's nginx `llm-proxy` (`KLANGK_LLM_API_KEY` / `.env` on the host). Hermes
> authenticates to the proxy with the **per-workspace token**, never the real key.

## The integration (key-free)

klangk already injects these into every workspace container (`container.py`):

| In-container value       | Source                                              | Meaning                                                                                        |
| ------------------------ | --------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `KLANGK_LLM_PROXY_URL`   | `http://host.containers.internal:<nginx>/llm-proxy` | OpenAI-compatible base URL; proxy forwards to `KLANGK_LLM_BASE_URL` with the real key injected |
| `KLANGK_LLM_MODEL`       | host env                                            | model id (for MiniMax, the MiniMax model id)                                                   |
| `klangk-workspace-token` | command → `/tmp/klangk/workspace-token`             | per-workspace bearer token the proxy validates                                                 |

Hermes's "custom OpenAI-compatible endpoint" maps onto these 1:1:

```yaml
# ~/.hermes/config.yaml
model:
  provider: custom
  base_url: "${KLANGK_LLM_PROXY_URL}" # e.g. http://host.containers.internal:8995/llm-proxy
  # api_key comes from ~/.hermes/.env (below), not inline
```

```sh
# ~/.hermes/.env  (written at container start; NO real key)
OPENAI_BASE_URL=${KLANGK_LLM_PROXY_URL}
OPENAI_API_KEY=$(klangk-workspace-token)   # per-workspace token, refreshed each start
HERMES_INFERENCE_MODEL=${KLANGK_LLM_MODEL}
```

The proxy is OpenAI-completions compatible (Pi uses it with `api:
openai-completions`); Hermes appends `/chat/completions` to `base_url`, hitting
`/llm-proxy/chat/completions`, which nginx forwards to
`${KLANGK_LLM_BASE_URL}/chat/completions` with `Authorization: Bearer <real key>`.

## Two ways to ship it

### Option A — bake into the workspace image (recommended; mirrors Pi)

Add a layer to `src/containers/workspace/Dockerfile` (the expensive install is
cached; runtime needs no egress):

```dockerfile
# Hermes agent (NousResearch). Installed at build time; configured per-user at login.
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
```

Then configure per-user at login the same way Pi is set up by
`klangk-setup-clankers.py` — add a small step that writes `~/.hermes/.env` from
the env vars above (only when `KLANGK_LLM_PROXY_URL` is set). Sketch:

```python
# in klangk-setup-clankers.py (or a sibling), run on each shell:
def setup_hermes():
    proxy = os.environ.get("KLANGK_LLM_PROXY_URL")
    if not proxy:
        return
    hdir = Path(os.environ["HOME"]) / ".hermes"
    hdir.mkdir(parents=True, exist_ok=True)
    token = subprocess.run(["klangk-workspace-token"], capture_output=True,
                           text=True).stdout.strip()
    (hdir / ".env").write_text(
        f"OPENAI_BASE_URL={proxy}\n"
        f"OPENAI_API_KEY={token}\n"
        f"HERMES_INFERENCE_MODEL={os.environ.get('KLANGK_LLM_MODEL','')}\n"
    )
    # config.yaml only needs provider=custom + base_url; key stays in .env
```

Refresh `.env` on each container start (the token rotates), exactly like
`klangk-setup-clankers.write_models()` refreshes Pi's `models.json`.

### Option B — klangkc sandbox (`.klangk-sandbox.yaml`, no image change)

Install + configure at sandbox startup via the `sandbox.setup` command. This is
"klangkc mode" — `klangkc sandbox` builds the mounts and runs setup once, then the
shell:

```yaml
# .klangk-sandbox.yaml
workspace:
  image: klangk-arm64 # or your default workspace image
sandbox:
  mount-at: ~/work
  setup: |
    set -e
    if ! command -v hermes >/dev/null; then
      curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
    fi
    mkdir -p ~/.hermes
    cat > ~/.hermes/.env <<EOF
    OPENAI_BASE_URL=${KLANGK_LLM_PROXY_URL}
    OPENAI_API_KEY=$(klangk-workspace-token)
    HERMES_INFERENCE_MODEL=${KLANGK_LLM_MODEL}
    EOF
    hermes config set model.provider custom
    hermes config set model.base_url "${KLANGK_LLM_PROXY_URL}"
```

Then `hermes` (interactive) or `hermes -z "..."` (one-shot) from the workspace
shell.

## Verifying it works

```sh
# inside the workspace:
echo "$KLANGK_LLM_PROXY_URL"          # should be http://host.containers.internal:<nginx>/llm-proxy
klangk-workspace-token | head -c 12   # non-empty token
hermes -z "Say hi in 5 words."        # should return text via MiniMax through the proxy
```

If it streams a reply, Hermes → llm-proxy → MiniMax is wired correctly with no key
in the container.

## Caveats / open items

- **Runtime egress:** the `install.sh` (and pip/npm/ffmpeg/ripgrep it pulls) needs
  general outbound internet. If the workspace restricts egress, use **Option A**
  (install at image-build time) so runtime only needs the llm-proxy.
- **Token rotation:** the workspace token changes on container restart — rewrite
  `~/.hermes/.env` on each start (Option A's per-login step handles this; Option B
  re-runs setup on sandbox (re)create).
- **MiniMax `<think>` tags:** klangk already ships `minimax-thinking-tags.ts` for
  Pi to convert literal `<think>…</think>` into thinking blocks. Hermes renders
  raw model text itself; if MiniMax emits literal tags, confirm Hermes's display
  handles them (likely fine in CLI; flag if not).
- **Model id:** set `KLANGK_LLM_MODEL` (host/`.env`) to the MiniMax model id you
  want; Hermes picks it up via `HERMES_INFERENCE_MODEL`. Per-run override:
  `hermes chat -m <model>`.
- **`hermes model` wizard:** skip it — it triggers provider OAuth/portal flows.
  The `.env` + `provider: custom` path above is fully headless.

## TL;DR for mcdonc

Hermes takes a custom OpenAI-compatible endpoint. Point it at klangk's existing
`llm-proxy` with the workspace token as the API key and `KLANGK_LLM_MODEL` as the
model — identical trust model to Pi, zero keys in the container. Ship via a
Dockerfile layer + a per-login `.env` writer (Option A), or a `.klangk-sandbox.yaml`
`setup:` block for klangkc mode (Option B).
