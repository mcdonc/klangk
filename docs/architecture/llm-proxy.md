# LLM Proxy

Klangk runs a reverse proxy (Caddy) in front of the FastAPI backend. The proxy serves the Flutter web UI, proxies API and WebSocket traffic to uvicorn, proxies hosted app URLs directly to container ports, and routes the `/llm-proxy/` path described below.

Pi containers access LLMs via the **LLM proxy**, which routes `/llm-proxy/` requests to the klangkd backend. The backend dispatches requests to the configured providers either via an in-process `litellm.Router` (multi-provider mode) or by forwarding directly to a single upstream (passthrough mode). This is required because:

1. **Pi is inside a container, LLMs are on the host or remote**: Pi containers can't reach `localhost:11434` (self-hosted Ollama) directly. They use `host.containers.internal` to reach the host's reverse proxy, which forwards to the backend.
2. **API key security**: API keys live only in the backend process memory (configured via `KLANGKD_LLM_MODELS`). The container's `models.json` contains only the proxy URL and a workspace JWT — no real API keys.
3. **No per-container LLM config**: The backend injects `KLANGKWS_LLM_PROXY_URL=http://host.containers.internal:<egress_port>/llm-proxy` into each container. `klangk-setup-pi` writes Pi's `models.json` with the proxy URL and `!klangk-workspace-token` as the API key (Pi resolves this command at request time; the proxy validates the workspace JWT before forwarding to the backend).

## Architecture

```text
Pi container
  → host.containers.internal:8995/llm-proxy/chat/completions
    → reverse proxy (Caddy) — workspace JWT validation + container ACL
      → klangkd FastAPI backend
        → passthrough (single provider) or litellm.Router (multi-provider)
          → upstream LLM provider(s)
```

The reverse proxy's `/llm-proxy/` block validates the workspace JWT via `forward_auth` (Caddy) and enforces the container-source IP ACL. It then forwards the request to the klangkd backend, which re-validates the workspace JWT itself (#2959, defense-in-depth): a request arriving without a valid workspace token — including a user login token or no token at all — is rejected with `401`. The proxy is therefore unreachable from outside workspace containers even when the backend port is directly reachable.

## Operating modes

### Passthrough mode (single provider)

When `llm-models` has exactly one entry with a wildcard `model_name` (containing `*`), the Router bypasses litellm and forwards requests directly to the upstream. This is the simplest setup and preserves the old single-provider experience:

- `GET /llm-proxy/models` queries the upstream's `/models` endpoint, so all models the upstream supports are automatically discovered by Pi.
- `POST /llm-proxy/chat/completions` forwards the request body verbatim — the `model` field reaches the upstream unchanged.

```yaml
# Single provider — all its models are automatically available
llm-models:
  - model_name: "*"
    params:
      api-base: http://bizon:11430
      api-key: dummy
```

```bash
# Equivalent env var (the model field is ignored in passthrough mode
# but must be present for the colon-delimited format to parse)
KLANGKD_LLM_MODELS="openai/*:http://bizon:11430:dummy"
```

This is the right choice when you have a single LLM endpoint (Ollama, vLLM, OpenAI, etc.) and want all its models available without listing them individually.

### Router mode (multiple providers)

When `llm-models` has multiple entries or entries without wildcards, the in-process `litellm.Router` handles request routing by matching the `model` field in each request to a configured `model_name`:

```yaml
# Multiple providers — litellm routes by model name
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
```

In this mode:

- `GET /llm-proxy/models` returns the configured `model_name` values.
- `POST /llm-proxy/chat/completions` routes by the `model` field in the request body to the matching provider.
- If `model` is empty, missing, or doesn't match any configured name, the first model is used as a fallback (preserving backwards compatibility with the old proxy).

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

Pi's `llm-proxy-models.ts` extension calls `GET /llm-proxy/models` and discovers available models. The response matches the OpenAI `/v1/models` shape. In passthrough mode, models are discovered from the upstream; in router mode, the configured `model_name` values are returned.

## Provider defaults

When `api_base` is empty, the following providers use their well-known API base URLs automatically: `openai`, `anthropic`, `cohere`, `mistral`, `groq`, `together_ai`, `deepseek`, `fireworks_ai`. For any other provider (or a custom endpoint), supply the full `api_base`.

## SIGHUP reconfiguration

The `LLMRouter` is a subsystem that participates in SIGHUP-triggered reconfiguration. When `klangkd.yaml` changes and a SIGHUP is sent, the router's model list is replaced in-place — no process restart required. The mode (passthrough vs. router) is re-evaluated on each reconfigure.
