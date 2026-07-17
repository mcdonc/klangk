# LLM Proxy

Klangk runs a reverse proxy (currently nginx) in front of the FastAPI backend. The proxy serves the Flutter web UI, proxies API and WebSocket traffic to uvicorn, proxies hosted app URLs directly to container ports (keeping the Python backend out of that hot path), and provides the LLM proxy described below. Using nginx also enables `auth_request`-based JWT validation on container-to-host endpoints without adding middleware overhead to every backend request.

Pi containers access the LLM via the **LLM proxy**, a proxy location that proxies `/llm-proxy/` requests to `${KLANGK_LLM_BASE_URL}`. This is required because:

1. **Pi is inside a container, LLM is on the host**: Pi containers can't reach `localhost:11434` (self-hosted Ollama) directly. They use `host.containers.internal` to reach the host, but the host's proxy serves the proxy endpoint.
2. **API key security**: The API key is sent in a request header by the proxy rather than being baked into the container image or passed as an env var. The container's `models.json` contains only the proxy URL (no real API key).
3. **No per-container LLM config**: The backend injects `KLANGK_LLM_PROXY_URL=http://host.containers.internal:<egress_port>/llm-proxy` into each container. `klangk-setup-pi` writes Pi's `models.json` with the proxy URL and `!klangk-workspace-token` as the API key (Pi resolves this command at request time; the proxy validates the workspace JWT via `auth_request` before replacing it with the real API key). `KLANGK_LLM_BASE_URL` is only used by the proxy itself.

The proxy config (`nginx.conf`) is rendered by the Python `klangk.proxy` module (#1396) and includes:

```nginx
location /llm-proxy/ {
    auth_request /api/v1/auth/verify-workspace-token;
    proxy_pass $KLANGK_LLM_BASE_URL/;
    proxy_set_header Authorization "Bearer $KLANGK_LLM_API_KEY";
    proxy_ssl_server_name on;
}
```

In CI, `devenv processes up -d` starts the proxy before E2E tests run.
