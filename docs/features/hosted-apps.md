# Hosted Apps

Workspace containers can run web applications (Jupyter, Marimo, Vite dev servers, etc.) on dynamically allocated ports. Klangk's nginx reverse proxy makes them accessible at predictable URLs without exposing raw container ports.

!!! note
Hosted apps are for **development and demonstration** — not permanent hosting. Port allocations and containers are ephemeral; use a dedicated hosting platform for production deployments.

!!! warning
Hosted apps are accessible to anyone who can reach the Klangk server. Do not serve sensitive content or data you don't want to be publicly visible.

## How it works

1. Each workspace gets **5 ports** allocated from the host range starting at port 9000 (configurable via `KLANGK_PORT_RANGE_START`). These map to container ports 8000-8004.
2. When a container starts, the port mappings are injected as `KLANGK_PORT_MAPPINGS` (e.g., `8000:9000,8001:9001,...`).
3. Nginx proxies requests from `/hosted/{workspace_id}/{port}/` directly to the container — no Python in the request path.

## Accessing hosted apps

Start any HTTP server on a container port (8000-8004), then visit:

```text
http://<hostname>:<nginx_port>/hosted/<workspace_id>/<host_port>/
```

For example, if your workspace ID is `abc123` and you run a server on container port 8000 (mapped to host port 9000):

```text
http://localhost:8995/hosted/abc123/9000/
```

WebSocket connections are supported — tools like Jupyter and Marimo work out of the box.

## Environment variables inside containers

| Variable                   | Example                   | Description                                   |
| -------------------------- | ------------------------- | --------------------------------------------- |
| `KLANGK_PORT_MAPPINGS`     | `8000:9000,8001:9001,...` | Container-to-host port mapping (CSV)          |
| `KLANGK_HOSTING_HOSTNAME`  | `localhost:8995`          | Hostname for constructing hosted app URLs     |
| `KLANGK_HOSTING_PROTO`     | `http`                    | Protocol for hosted app URLs                  |
| `KLANGK_HOSTING_BASE_PATH` | `/klangk`                 | Base path prefix (empty for root deployments) |

Extensions and scripts inside the container can use these to construct correct URLs for their hosted apps. Pi's built-in `get_hosted_url` tool does this automatically.

### `klangk-hosted-url` (from inside a container)

The easiest way to get a hosted app URL from the shell is the
`klangk-hosted-url` script, baked into the workspace image:

```bash
$ klangk-hosted-url 8000
http://localhost:8995/hosted/abc123/9000/
```

Pass the **container port** (8000-8004); it resolves the mapped host port via
`KLANGK_PORT_MAPPINGS` and combines it with the `KLANGK_HOSTING_*` and
`KLANGK_WORKSPACE_ID` env vars to print the full URL. Use it from `setup.sh`,
your `default_command`, a `health_check`, or interactively. Pi's
`get_hosted_url` tool delegates to this same script, so the URL logic lives in
one place.

Error cases: no argument prints usage; a container port not present in
`KLANGK_PORT_MAPPINGS` lists the valid ports; a missing `KLANGK_PORT_MAPPINGS`
errors out. Each exits non-zero.

## Behind a reverse proxy

When Klangk runs behind an outer reverse proxy (e.g., on a subpath like `/klangk`), the hosting info is auto-derived from standard headers:

- `X-Forwarded-Host` — used as the hostname
- `X-Forwarded-Proto` — used as the protocol
- `X-Forwarded-Prefix` — used as the base path

The generated hosted app URL becomes:

```text
https://example.com/klangk/hosted/<workspace_id>/<host_port>/
```

No manual configuration needed — the `KLANGK_HOSTING_*` environment variables are only required if header-based derivation doesn't work for your setup.

## Configuration

| Variable                  | Default | Description                                   |
| ------------------------- | ------- | --------------------------------------------- |
| `KLANGK_PORT_RANGE_START` | `9000`  | First host port for workspace app allocations |
| `KLANGK_NGINX_PORT`       | `8995`  | Nginx port (used in URL derivation)           |

Ports are allocated atomically and cleaned up automatically when workspaces are deleted.
