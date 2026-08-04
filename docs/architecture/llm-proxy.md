# LLM Proxy

Klangk runs a reverse proxy (caddy or nginx) in front of the FastAPI backend. The proxy serves the Flutter web UI, proxies API and WebSocket traffic to uvicorn, proxies hosted app URLs directly to container ports, and routes the `/llm-proxy/` path described below.

Pi containers access LLMs via the **LLM proxy**, which routes `/llm-proxy/` requests to the klangkd backend. The backend uses an in-process `litellm.Router` to dispatch requests to the configured providers. This is required because:

1. **Pi is inside a container, LLMs are on the host or remote**: Pi containers can't reach `localhost:11434` (self-hosted Ollama) directly. They use `host.containers.internal` to reach the host's reverse proxy, which forwards to the backend.
2. **API key security**: API keys live only in the backend process memory (configured via `KLANGKD_LLM_MODELS`). The container's `models.json` contains only the proxy URL and a workspace JWT — no real API keys.
3. **No per-container LLM config**: The backend injects `KLANGKWS_LLM_PROXY_URL=http://host.containers.internal:<egress_port>/llm-proxy` into each container. `klangk-setup-pi` writes Pi's `models.json` with the proxy URL and `!klangk-workspace-token` as the API key (Pi resolves this command at request time; the proxy validates the workspace JWT before forwarding to the backend).

## Architecture

```text
Pi container
  → host.containers.internal:8995/llm-proxy/chat/completions
    → reverse proxy (caddy/nginx) — workspace JWT validation + container ACL
      → klangkd FastAPI backend
        → litellm.Router (in-process)
          → routes by "model" field in request body:
            → openai/gpt-4o    → https://api.openai.com/v1
            → anthropic/...    → https://api.anthropic.com/v1
            → ollama/llama3    → http://gpu:11434
```

The reverse proxy's `/llm-proxy/` block validates the workspace JWT via `auth_request` (nginx) or `forward_auth` (caddy) and enforces the container-source IP ACL. It then forwards the request to the klangkd backend, which dispatches it via the `litellm.Router`.

## Configuration

Configure models via `KLANGKD_LLM_MODELS` (env var) or `llm-models` (klangkd.yaml).

### Env var (colon-delimited strings)

```bash
# Format: provider/model:api_base:api_key
#   - provider/model: LiteLLM provider prefix + model name
#   - api_base: provider API base URL (empty = use provider default)
#   - api_key: provider API key (empty = keyless, e.g. local Ollama)
KLANGKD_LLM_MODELS="openai/gpt-4o::sk-xxx,anthropic/claude-sonnet-4::sk-ant-xxx,ollama/llama3:http://gpu:11434:"
```

### klangkd.yaml (LiteLLM-native dict format)

The recommended format for `klangkd.yaml` uses the same `model_name` / `litellm_params` shape as LiteLLM's own config. Keys accept both kebab-case and snake_case. `api_key` and `api_base` values support `file:` and `cmd:` indirection so secrets stay out of the config file.

```yaml
llm-models:
  - model_name: gpt-4
    params:
      model: openai/gpt-4o
      api-key: "cmd:pass show openai/api-key"
  - model_name: claude
    params:
      model: anthropic/claude-sonnet-4
      api-key: "file:/run/secrets/anthropic-key"
  - model_name: local-llama
    params:
      model: ollama/llama3
      api-base: "http://gpu:11434"
  - model_name: local-qwen
    params:
      model: hosted_vllm/RedHatAI/Qwen3.6-35B-A3B-NVFP4
      api-base: "http://bizon:11430"
      api-key: dummy
```

`params` is a shorthand for `litellm_params` — both are accepted. All keys within `params` that litellm supports are passed through unchanged.

### Default API key

`KLANGKD_LLM_API_KEY` provides a default API key for models that don't specify their own. This is useful when all models share the same key (e.g. a single OpenAI account):

```yaml
llm-api-key: "cmd:pass show openai/api-key"
llm-models:
  - model_name: gpt-4o
    params:
      model: openai/gpt-4o
  - model_name: gpt-4o-mini
    params:
      model: openai/gpt-4o-mini
```

## Model discovery

Pi's `llm-proxy-models.ts` extension calls `GET /llm-proxy/models` and discovers all configured models. The response matches the OpenAI `/v1/models` shape. Users pick any discovered model from Pi's model selector.

## Provider defaults

When `api_base` is empty, the following providers use their well-known API base URLs automatically: `openai`, `anthropic`, `cohere`, `mistral`, `groq`, `together_ai`, `deepseek`, `fireworks_ai`. For any other provider (or a custom endpoint), supply the full `api_base`.

## SIGHUP reconfiguration

The `LLMRouter` is a subsystem that participates in SIGHUP-triggered reconfiguration. When `klangkd.yaml` changes and a SIGHUP is sent, the router's model list is replaced in-place — no process restart required.
