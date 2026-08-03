# LLM Proxy

Klangk runs a reverse proxy (currently nginx) in front of the FastAPI backend. The proxy serves the Flutter web UI, proxies API and WebSocket traffic to uvicorn, proxies hosted app URLs directly to container ports (keeping the Python backend out of that hot path), and provides the LLM proxy described below. Using nginx also enables `auth_request`-based JWT validation on container-to-host endpoints without adding middleware overhead to every backend request.

Pi containers access the LLM via the **LLM proxy**, a proxy location that proxies `/llm-proxy/` requests to `${KLANGKD_LLM_BASE_URL}`. This is required because:

1. **Pi is inside a container, LLM is on the host**: Pi containers can't reach `localhost:11434` (self-hosted Ollama) directly. They use `host.containers.internal` to reach the host, but the host's proxy serves the proxy endpoint.
2. **API key security**: The API key is sent in a request header by the proxy rather than being baked into the container image or passed as an env var. The container's `models.json` contains only the proxy URL (no real API key).
3. **No per-container LLM config**: The backend injects `KLANGKWS_LLM_PROXY_URL=http://host.containers.internal:<egress_port>/llm-proxy` into each container. `klangk-setup-pi` writes Pi's `models.json` with the proxy URL and `!klangk-workspace-token` as the API key (Pi resolves this command at request time; the proxy validates the workspace JWT via `auth_request` before replacing it with the real API key). `KLANGKD_LLM_BASE_URL` is only used by the proxy itself.

The proxy config (`nginx.conf`) is rendered by the Python `klangk.proxy` module (#1396) and includes:

```nginx
location /llm-proxy/ {
    auth_request /api/v1/auth/verify-workspace-token;
    proxy_pass $KLANGKD_LLM_BASE_URL/;
    proxy_set_header Authorization "Bearer $KLANGKD_LLM_API_KEY";
    proxy_ssl_server_name on;
}
```

In CI, `devenv processes up -d` starts the proxy before E2E tests run.

## Multiple models from one provider

If the upstream pointed to by `KLANGKD_LLM_BASE_URL` serves multiple models (e.g. OpenAI's API exposes `gpt-4o`, `gpt-4o-mini`, `o3`; or a self-hosted vLLM/Ollama instance serving several models), **no klangk changes are needed**. The `llm-proxy-models.ts` extension calls `GET /models` on the proxy, discovers every model the upstream advertises, and registers them all with Pi. The user picks any discovered model from Pi's model selector.

## Multiple providers (LLM aggregator sidecar)

To route to **multiple distinct providers** (e.g. OpenAI + Anthropic + a local Ollama, each with its own base URL and API key), klangk can run a **LiteLLM aggregator sidecar** (#2046). The sidecar is a podman container (`ghcr.io/berriai/litellm`) that exposes a single OpenAI-compatible endpoint and routes per-request by `model` name. The existing proxy plumbing and model discovery work unchanged.

### How it works

1. The operator configures `KLANGKD_LLM_AGGREGATOR_MODELS` with a list of `provider/model:api_base:api_key` entries.
2. At startup, klangk renders a LiteLLM `config.yaml` from those entries and runs the LiteLLM container with that config (config-only mode, no DB, no UI).
3. `KLANGKD_LLM_BASE_URL` points at the sidecar (`http://127.0.0.1:8996/v1`).
4. The proxy forwards `/llm-proxy/` to the sidecar, which routes to the correct provider based on the `model` field in each request.
5. `llm-proxy-models.ts` calls `/models` on the sidecar and discovers all models across all configured providers.

### Configuration

Set these environment variables (or their equivalents in `klangkd.yaml`):

```bash
# Required: list of provider/model entries (comma-separated).
# Format: litellm_model:api_base:api_key
#   - litellm_model: provider/model in LiteLLM notation
#   - api_base: provider API base URL (empty = use provider default)
#   - api_key: provider API key (empty = keyless, e.g. local Ollama)
KLANGKD_LLM_AGGREGATOR_MODELS="openai/gpt-4o::sk-xxx,anthropic/claude-sonnet-4::sk-ant-xxx,ollama/llama3:http://gpu:11434:"

# Point the proxy at the sidecar.
KLANGKD_LLM_BASE_URL="http://127.0.0.1:8996/v1"

# Optional: master key for the LiteLLM sidecar. Default is empty (no auth —
# the sidecar is protected by its 127.0.0.1 bind + the proxy's IP filtering).
# When set, LiteLLM enforces bearer auth: set KLANGKD_LLM_API_KEY to the SAME
# value (the proxy sends it as the bearer). Works DB-less — a matching key is
# accepted in-memory.
KLANGKD_LLM_AGGREGATOR_MASTER_KEY="sk-master"

# Optional: host port the sidecar is published on (default: 8996; LiteLLM
# always listens on 4000 inside the container).
KLANGKD_LLM_AGGREGATOR_PORT=8996

# Optional: container image (default: ghcr.io/berriai/litellm:main-stable).
KLANGKD_LLM_AGGREGATOR_IMAGE="ghcr.io/berriai/litellm:main-stable"
```

Or in `klangkd.yaml` (colon-delimited strings):

```yaml
llm-base-url: "http://127.0.0.1:8996/v1"
llm-aggregator-models:
  - "openai/gpt-4o::sk-xxx"
  - "anthropic/claude-sonnet-4::sk-ant-xxx"
  - "ollama/llama3:http://gpu:11434:"
llm-aggregator-master-key: "sk-master"
```

Or using the dict format (recommended for `klangkd.yaml` — supports `file:` and `cmd:` indirection on secrets so API keys stay out of the config file):

```yaml
llm-base-url: "http://127.0.0.1:8996/v1"
llm-aggregator-models:
  - id: openai/gpt-4o
    api-key: "cmd:pass show openai/api-key"
  - id: anthropic/claude-sonnet-4
    api-key: "file:/run/secrets/anthropic-key"
  - id: ollama/llama3
    base-url: "http://gpu:11434"
llm-aggregator-master-key: "sk-master"
```

Each dict entry has `id` (required), `base-url` (optional — omit to use provider default), and `api-key` (optional — omit for keyless providers like local Ollama). Both `api-key` and `base-url` support `file:<path>` and `cmd:<command>` indirection.

### Architecture

```text
Pi container
  → host.containers.internal:8995/llm-proxy/chat/completions
    → reverse proxy (caddy/nginx)
      → http://127.0.0.1:8996/v1/chat/completions   (LiteLLM sidecar)
        → routes by "model" field in request body:
          → openai/gpt-4o    → https://api.openai.com/v1
          → anthropic/...    → https://api.anthropic.com/v1
          → ollama/llama3    → http://gpu:11434
```

The sidecar is supervised by `LiteLLMWatchdog` (mirroring `ProxyWatchdog`): it respawns on unexpected exit with exponential backoff, and is stopped cleanly on shutdown. Settings changes via SIGHUP trigger a container restart with the re-rendered config only when aggregator settings actually changed (other SIGHUP changes are ignored). Removing all models via SIGHUP stops the sidecar. The container port is bound to `127.0.0.1` (loopback only) so the sidecar is not reachable from the LAN. By default the sidecar runs without a master key (no-auth, relying on the loopback bind + the proxy's IP filtering); set `KLANGKD_LLM_AGGREGATOR_MASTER_KEY` (and `KLANGKD_LLM_API_KEY` to the same value) for optional bearer auth.

### Provider defaults

When `api_base` is empty, the following providers use their well-known API base URLs automatically: `openai`, `anthropic`, `cohere`, `mistral`, `groq`, `together_ai`, `deepseek`, `fireworks_ai`. For any other provider (or a custom endpoint), supply the full `api_base`.

### Packaging decision

LiteLLM pulls a large dependency tree. To avoid polluting klangk's Python venv, the sidecar runs as a **podman container** using the official `ghcr.io/berriai/litellm` image. This is the cleanest NixOS-friendly route and reuses the podman infrastructure klangk already depends on for workspaces.
