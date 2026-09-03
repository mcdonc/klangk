# Hosted Apps

Workspace containers can run web applications (Jupyter, Marimo, Vite dev servers, etc.) on dynamically allocated ports. Klangk's reverse proxy (Caddy) makes them accessible at predictable URLs without exposing raw container ports.

!!! note
Hosted apps are for **development and demonstration** — not permanent hosting. Port allocations and containers are ephemeral; use a dedicated hosting platform for production deployments.

!!! warning
Hosted apps are accessible to anyone who can reach the Klangk server. Do not serve sensitive content or data you don't want to be publicly visible.

## How it works

1. Each workspace gets up to **5 ports** (configurable via `KLANGKD_HOSTED_PORTS_PER_WORKSPACE`; set to `0` to disable hosting entirely) allocated from the host range starting at port 9000 (configurable via `KLANGKD_PORT_RANGE_START`). These map to container ports 8000-8004.
2. When a container starts, the port mappings are injected as `KLANGKWS_PORT_MAPPINGS` (e.g., `8000:9000,8001:9001,...`).
3. The proxy proxies requests from `/hosted/{workspace_id}/{port}/` directly to the container — no Python in the request path.

!!! note
**Works with egress filtering.** A workspace with an `allowed_domains` allow-list runs behind the [network sidecar](egress-filtering.md), which owns the shared network namespace; host ports are published on the sidecar (not the workspace — `--publish` is silently discarded under `--network container:`) and forward into the shared netns to the workspace's listener, so hosting works the same as for unrestricted workspaces. IPv4 clients are reachable; the sidecar default-denies IPv6, so IPv6-only clients to a published port fail.

## Accessing hosted apps

Start any HTTP server on a container port (8000-8004), then visit:

```text
http://<hostname>:<KLANGKD_PORT>/hosted/<workspace_id>/<host_port>/
```

For example, if your workspace ID is `abc123` and you run a server on container port 8000 (mapped to host port 9000):

```text
http://localhost:8997/hosted/abc123/9000/
```

WebSocket connections are supported — tools like Jupyter and Marimo work out of the box.

## Environment variables inside containers

| Variable                     | Example                   | Description                                   |
| ---------------------------- | ------------------------- | --------------------------------------------- |
| `KLANGKWS_PORT_MAPPINGS`     | `8000:9000,8001:9001,...` | Container-to-host port mapping (CSV)          |
| `KLANGKWS_HOSTING_HOSTNAME`  | `localhost:8997`          | Hostname for constructing hosted app URLs     |
| `KLANGKWS_HOSTING_PROTO`     | `http`                    | Protocol for hosted app URLs                  |
| `KLANGKWS_HOSTING_BASE_PATH` | `/klangk`                 | Base path prefix (empty for root deployments) |

These are injected at container start. `KLANGKWS_HOSTING_HOSTNAME` names the
browser listener (`KLANGKD_PORT`): with no `KLANGKD_HOSTING_HOSTNAME` override,
any synthetic port-less loopback hostname — the setup-time start with no
request in hand, or a `Host: localhost` arriving from a CLI connected over the
backend socket — automatically carries the browser-listener port. The whole
block is omitted in headless deployments (`KLANGKD_PORT` unset) and
when hosting is disabled — the same clean-error outcome as below.

Extensions and scripts inside the container can use these to construct correct URLs for their hosted apps. Pi's built-in `get_hosted_url` tool does this automatically.

### `klangk-hosted-url` (from inside a container)

The easiest way to get a hosted app URL from the shell is the
`klangk-hosted-url` script, baked into the workspace image:

```bash
$ klangk-hosted-url 8000
http://localhost:8997/hosted/abc123/9000/
```

Pass the **container port** (8000-8004); it resolves the mapped host port via
`KLANGKWS_PORT_MAPPINGS` and combines it with the `KLANGKWS_HOSTING_*` and
`KLANGKWS_WORKSPACE_ID` env vars to print the full URL. Use it from `setup.sh`,
your `service_command`, a `health_check`, or interactively. Pi's
`get_hosted_url` tool delegates to this same script, so the URL logic lives in
one place.

Error cases: no argument prints usage; a container port not present in
`KLANGKWS_PORT_MAPPINGS` lists the valid ports; a missing `KLANGKWS_PORT_MAPPINGS`
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

No manual configuration needed — the `KLANGKD_HOSTING_*` environment variables are only required if header-based derivation doesn't work for your setup.

## Configuration

| Variable                             | Default   | Description                                                                                                                |
| ------------------------------------ | --------- | -------------------------------------------------------------------------------------------------------------------------- |
| `KLANGKD_PORT_RANGE_START`           | `9000`    | First host port for workspace app allocations                                                                              |
| `KLANGKD_HOSTED_PORTS_PER_WORKSPACE` | `5`       | Ceiling on ports per workspace. `0` disables hosted-app serving entirely (no allocation, no hosting env, `/hosted/` 404s). |
| `KLANGKD_PORT`                       | _(unset)_ | Browser/proxy port (used in URL derivation). Must be set to serve hosted apps (hosted apps are browser-ingress).           |

Ports are allocated atomically and cleaned up automatically when workspaces are deleted.

## Disabling hosted apps

Set `KLANGKD_HOSTED_PORTS_PER_WORKSPACE=0` to turn hosted-app serving off
server-wide. This is a single knob that doubles as the count configuration:

- **No ports are allocated** — not at workspace creation, not on container
  start. Existing workspaces release their allocations on their next start.
- **No hosting env in containers** — `KLANGKWS_PORT_MAPPINGS` and the
  `KLANGKWS_HOSTING_*` vars are not injected, so `klangk-hosted-url` and the
  agent's `get_hosted_url` tool error out cleanly. The same happens in
  headless deployments (`KLANGKD_PORT` unset): `/hosted/` is served by the
  browser listener, which headless mode does not render.
- **`/hosted/<ws>/<port>/` returns 404** — the proxy locations are
  collapsed to a single `return 404` block.

A positive value (e.g. `3`) caps each workspace at that many ports. Changing
the value takes effect on each workspace's next container start (no backend
restart needed). Per-workspace overrides are tracked separately.
