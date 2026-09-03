# Running with Docker

The Klangk host container packages the backend, the reverse proxy (Caddy), Flutter
web UI, and workspace image into a single Docker image. Workspace
containers run inside it via rootless podman. No source checkout or
build tools required.

## Prerequisites

- Docker (or Podman)
- An OpenAI-compatible LLM provider and API key

## Run

```bash
docker run -d \
  --name klangk \
  -p 8997:8997 \
  -v klangk-data:/home/klangk/data \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  -e KLANGKD_DEFAULT_USER=you@example.com \
  -e KLANGKD_DEFAULT_PASSWORD=changeme \
  -e KLANGKD_AUTH_MODES=password \
  -e KLANGKD_JWT_SECRET=$(openssl rand -hex 32) \
  -e KLANGKD_LLM_MODELS="openai/gpt-4o::your-api-key" \
  ghcr.io/mcdonc/klangk/klangk-host:v1.0
```

Open <http://localhost:8997> and log in with the email and password
you set above.

The published host image uses **password auth** — the examples pin
`KLANGKD_AUTH_MODES=password`, and that is the supported configuration
for the image. The default mode for a local install is `none`
(no-login, loopback-only), but **`none` is an unsupported configuration
with the published Docker host image.** The image publishes its port
(`-p 8997:8997`), making it network-reachable, while `none` mode is
loopback-only by design — it freely issues an admin token with no
password, so its entire security model is "only the operator's loopback
can reach it." Two independent gates refuse it in Docker:

1. **Bind-safety gate.** `none` mode won't boot on a non-loopback bind
   (publishing the port requires binding a non-loopback address like
   `0.0.0.0`). Setting `KLANGKD_ALLOW_INSECURE_NO_AUTH=1` overrides this —
   but only to warn you and expose a free-admin-token endpoint to the
   whole network.
2. **Proxy `/auth/local` ACL.** Even past the bind gate, a host browser
   reaching the container through the published port-forward appears at
   the container as the Docker bridge/gateway IP (e.g. `172.17.0.1`),
   not `127.0.0.1`, so the loopback-only ACL denies `/api/v1/auth/local`
   with `403`.

For a no-login single-user experience, run klangk locally (devenv, or
the bare binary on your own machine) instead of the published image.
See [Auth Modes](../features/auth-modes.md).

## What the flags do

| Flag                                    | Why                                               |
| --------------------------------------- | ------------------------------------------------- |
| `-v klangk-data:/home/klangk/data`      | Persist workspaces and database across restarts   |
| `--cap-add SYS_ADMIN`                   | Required for rootless podman inside the container |
| `--device /dev/fuse`                    | FUSE filesystem for overlay storage               |
| `--device /dev/net/tun`                 | pasta networking for workspace containers         |
| `--security-opt seccomp=unconfined`     | Allow syscalls needed for nested containers       |
| `--security-opt systempaths=unconfined` | Allow `/proc` access for nested containers        |

## Data persistence

All klangk data (database, workspaces, home directories) is stored
in `/home/klangk/data` inside the container. The `-v klangk-data:/home/klangk/data`
flag mounts a Docker volume there so data survives container removal.

**Without the volume, you lose everything when the container is
removed.** The volume is included in both the `docker run` and
`docker-compose.yml` examples above.

To use a host directory instead of a Docker volume:

```bash
mkdir -p ./klangk-data
docker run -d -v ./klangk-data:/home/klangk/data ...
```

## Stopping and restarting

```bash
docker stop klangk
docker start klangk
```

Your workspaces, files, and database are preserved in the
`klangk-data` volume.

## Using docker-compose

Create a `docker-compose.yml`:

```yaml
services:
  klangk:
    image: ghcr.io/mcdonc/klangk/klangk-host:v1.0
    ports:
      - "8997:8997"
    volumes:
      - klangk-data:/home/klangk/data
    cap_add:
      - SYS_ADMIN
    devices:
      - /dev/fuse
      - /dev/net/tun
    security_opt:
      - seccomp=unconfined
      - systempaths=unconfined
    environment:
      KLANGKD_DEFAULT_USER: you@example.com
      KLANGKD_DEFAULT_PASSWORD: changeme
      KLANGKD_AUTH_MODES: password
      KLANGKD_JWT_SECRET: change-this-to-a-random-secret
      KLANGKD_LLM_MODELS: "openai/gemma4:31b:https://ollama.com/v1:"
      KLANGKD_LLM_API_KEY: your-api-key

volumes:
  klangk-data:
```

Then: `docker compose up -d`

## Updating

```bash
docker pull ghcr.io/mcdonc/klangk/klangk-host:v1.0
docker stop klangk
docker rm klangk
# Run the same docker run command with the new version tag
```

## Adding features

To add features beyond what ships with the image, you need to build a
custom image — see [Customizing a Deployment](customizing.md) for
instructions.

## Next steps

- [Environment Variables](../reference/environment.md) — all
  configuration options
- [Feature Activation](../features/features.md) — the default features and how to turn them on
