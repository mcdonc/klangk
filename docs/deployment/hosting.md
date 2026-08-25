# Hosting & Proxy

**The reverse proxy (Caddy) is the primary access point** (port 8997 locally). It proxies API/WebSocket to uvicorn and proxies hosted app URLs directly to container ports (no Python in the hosted app path).

- FastAPI serves API endpoints and Flutter frontend static files on port 8997 (not accessed directly by users).
- Hosted app URLs (`/hosted/{workspace_id}/{port}/`) are handled by a proxy regex location that extracts the port and proxies to `127.0.0.1:{port}`.
- Subpath hosting (e.g., `/klangk/`) handled by an outer nginx that sends `X-Forwarded-Prefix`, `X-Forwarded-Host`, and `X-Forwarded-Proto` headers. Klangk's `_derive_hosting_info` uses these to generate correct hosted app URLs. The outer nginx also rewrites `<base href>` via `sub_filter`.
- Frontend derives API URLs from `<base href>` — works on both root and subpath.
- WebSocket proxying via the proxy.

## Topology

The devenv.nix runs the proxy as the primary access point:

```text
klangk reverse proxy (port 8997)
    ├── /hosted/{ws_id}/{port}/ → container port (direct proxy)
    └── /                       → Klangk backend (port 8997)
```

In production behind a reverse proxy with subpath:

```text
outer nginx (443)
    ├── /klangk/hosted/{ws_id}/{port}/ → container port (direct proxy)
    └── /klangk/                       → klangk proxy (port 8997)
                                         └── / → uvicorn (port 8997)
```

## Ports

- `KLANGKD_PORT` (default unset): **Browser access point** — the proxy serves UI, API, WebSocket, and proxies hosted app URLs directly to container ports. Unset ⇒ headless mode (no browser listener). Suggested `8997` ([#1542](https://github.com/mcdonc/klangk/issues/1542)).
- `KLANGKD_EGRESS_PORT` (default `8995`): Container-egress port — the proxy listener for container→backend traffic (`/llm-proxy`, browser-delegate bridge). Must differ from `KLANGKD_PORT`.
- `KLANGKD_PROXY_PORT`: **Deprecated** alias for `KLANGKD_EGRESS_PORT`; rename it. (Renamed from `KLANGKD_NGINX_PORT`; the old name is no longer recognized.)
- `9000+`: User app ports (5 per workspace, mapped to container ports 8000-8004)

For TLS termination or an outer reverse proxy, see
[Behind a Reverse Proxy](behind-a-proxy.md).

## Tailscale and LLM Proxy

If the LLM provider is on a Tailscale host (e.g., a self-hosted Ollama on another machine in the tailnet), the `api-base` in `KLANGKD_LLM_MODELS` **must use the Tailscale IP address**, not a hostname. The in-process litellm Router resolves DNS through the host's resolver stack; on a Tailscale host, bare hostnames and MagicDNS FQDNs may not resolve correctly from the backend process.

`KLANGKD_DNS_SERVERS=100.100.100.100,8.8.8.8` is still needed for workspace containers, because podman configures container DNS with search domains that make MagicDNS work correctly inside containers.

```bash
# In .env on a Tailscale host:
KLANGKD_DNS_SERVERS=100.100.100.100,8.8.8.8            # for containers (works fine)
```

Tailscale IPs are stable and don't change, so using the IP directly is safe.
