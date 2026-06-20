# Running with Docker

The Klangk host container packages the backend, nginx proxy, Flutter
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
  -p 8995:8995 \
  -v klangk-data:/home/klangk/data \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  -e KLANGK_DEFAULT_USER=you@example.com \
  -e KLANGK_DEFAULT_PASSWORD=changeme \
  -e KLANGK_JWT_SECRET=$(openssl rand -hex 32) \
  -e KLANGK_LLM_BASE_URL=https://ollama.com/v1 \
  -e KLANGK_LLM_API_KEY=your-api-key \
  -e KLANGK_LLM_MODEL=gemma4:31b \
  ghcr.io/mcdonc/klangk/klangk-host:v2026.06.10
```

Open <http://localhost:8995> and log in with the email and password
you set above.

## What the flags do

| Flag                                    | Why                                               |
| --------------------------------------- | ------------------------------------------------- |
| `-v klangk-data:/home/klangk/data`      | Persist workspaces and database across restarts   |
| `--cap-add SYS_ADMIN`                   | Required for rootless podman inside the container |
| `--device /dev/fuse`                    | FUSE filesystem for overlay storage               |
| `--device /dev/net/tun`                 | pasta networking for workspace containers         |
| `--security-opt seccomp=unconfined`     | Allow syscalls needed for nested containers       |
| `--security-opt systempaths=unconfined` | Allow `/proc` access for nested containers        |

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
    image: ghcr.io/mcdonc/klangk/klangk-host:v2026.06.10
    ports:
      - "8995:8995"
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
      KLANGK_DEFAULT_USER: you@example.com
      KLANGK_DEFAULT_PASSWORD: changeme
      KLANGK_JWT_SECRET: change-this-to-a-random-secret
      KLANGK_LLM_BASE_URL: https://ollama.com/v1
      KLANGK_LLM_API_KEY: your-api-key
      KLANGK_LLM_MODEL: gemma4:31b

volumes:
  klangk-data:
```

Then: `docker compose up -d`

## Updating

```bash
docker pull ghcr.io/mcdonc/klangk/klangk-host:v2026.06.15
docker stop klangk
docker rm klangk
# Run the same docker run command with the new version tag
```

## Adding plugins

To add plugins beyond what ships with the image, you need to build a
custom image — see [Customizing a Deployment](customizing.md) for
instructions.

## Next steps

- [Environment Variables](../reference/environment.md) — all
  configuration options
- [Using Plugins](../features/plugins.md) — what the default plugins do
