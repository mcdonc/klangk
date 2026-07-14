# Hosting & Nginx

**nginx is the primary access point** (port 8995 locally). It proxies API/WebSocket to uvicorn and proxies hosted app URLs directly to container ports (no Python in the hosted app path).

- FastAPI serves API endpoints and Flutter frontend static files on port 8997 (not accessed directly by users).
- Hosted app URLs (`/hosted/{workspace_id}/{port}/`) are handled by an nginx regex location that extracts the port and proxies to `127.0.0.1:{port}`.
- Subpath hosting (e.g., `/klangk/`) handled by an outer nginx that sends `X-Forwarded-Prefix`, `X-Forwarded-Host`, and `X-Forwarded-Proto` headers. Klangk's `_derive_hosting_info` uses these to generate correct hosted app URLs. The outer nginx also rewrites `<base href>` via `sub_filter`.
- Frontend derives API URLs from `<base href>` — works on both root and subpath.
- WebSocket proxying via nginx `proxyWebsockets`.

## Topology

The devenv.nix runs nginx as the primary access point:

```text
nginx reverse proxy (port 8995)
    ├── /hosted/{ws_id}/{port}/ → container port (direct proxy)
    └── /                       → Klangk backend (port 8997)
```

In production behind a reverse proxy with subpath:

```text
outer nginx (443)
    ├── /klangk/hosted/{ws_id}/{port}/ → container port (direct proxy)
    └── /klangk/                       → klangk nginx (port 8995)
                                         └── / → uvicorn (port 8997)
```

## Ports

- `KLANGK_PORT` (default unset): **Browser access point** — nginx serves UI, API, WebSocket, and proxies hosted app URLs directly to container ports. Unset ⇒ headless mode (no browser listener). Suggested `8997` ([#1542](https://github.com/mcdonc/klangk/issues/1542)).
- `KLANGK_EGRESS_PORT` (default `8995`): Container-egress port — the nginx listener for container→backend traffic (`/llm-proxy`, browser-delegate bridge, chat). Must differ from `KLANGK_PORT`.
- `KLANGK_NGINX_PORT`: **Deprecated** alias for `KLANGK_EGRESS_PORT`; rename it.
- `9000+`: User app ports (5 per workspace, mapped to container ports 8000-8004)

## Tailscale and LLM Proxy

If the LLM provider is on a Tailscale host (e.g., a self-hosted Ollama on another machine in the tailnet), `KLANGK_LLM_BASE_URL` **must use the Tailscale IP address**, not a hostname.

The nginx LLM proxy uses lazy DNS resolution (so nginx can start even if the LLM host is temporarily unreachable). This means nginx sends raw DNS queries to the resolvers from `/etc/resolv.conf`. On a Tailscale host, those resolvers include MagicDNS (`100.100.100.100`), but MagicDNS only resolves tailnet names through the system resolver stack — raw UDP DNS queries from nginx don't go through Tailscale's networking, so both bare hostnames and FQDNs fail to resolve.

Meanwhile, `KLANGK_DNS_SERVERS=100.100.100.100,8.8.8.8` is still needed for workspace containers, because podman configures container DNS with search domains that make MagicDNS work correctly inside containers.

```bash
# In .env on a Tailscale host:
KLANGK_LLM_BASE_URL=http://100.122.115.33:11434/v1   # Tailscale IP, not hostname
KLANGK_DNS_SERVERS=100.100.100.100,8.8.8.8            # for containers (works fine)
```

Tailscale IPs are stable and don't change, so using the IP directly is safe.
