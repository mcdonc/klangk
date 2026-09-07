# Running with Docker

The Klangk host container packages the backend, the reverse proxy (Caddy), Flutter
web UI, and workspace image into a single Docker image. Workspace
containers run inside it via rootless podman. No source checkout or
build tools required.

## Prerequisites

- Docker (or Podman)
- An OpenAI-compatible LLM provider and API key

## Run

Set your deployment values in a `klangkd.yaml` — [Getting
Started](../getting-started.md) has a ready-to-use file covering the auth
settings, the admin identity, an LLM provider, and the image's structural
settings — then mount it over the image's copy at
`/home/klangk/etc/klangkd.yaml`:

```bash
docker run -d \
  --name klangk \
  -p 8997:8997 \
  -v klangk-data:/home/klangk/data \
  -v ./klangkd.yaml:/home/klangk/etc/klangkd.yaml:ro \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  ghcr.io/mcdonc/klangk/klangk-host:v1.0
```

Open <http://localhost:8997> and log in with the `default_user` /
`default_password` from your config file.

The published host image uses **password auth** — the examples pin
`auth_modes: password` in the mounted config, and that is the supported
configuration for the image. **`none` mode is unsupported with the
published Docker host image** — it is loopback-only by design, and the
image publishes its port, making it network-reachable. Two independent
gates (bind-safety and the proxy ACL) refuse `none` in Docker. For a
no-login single-user experience, run klangk locally (devenv, or the
bare binary) instead of the published image. See
[Auth Modes](../features/auth-modes.md) for the full explanation.

## What the flags do

| Flag                                    | Why                                               |
| --------------------------------------- | ------------------------------------------------- |
| `-v klangk-data:/home/klangk/data`      | Persist workspaces and database across restarts   |
| `-v ./klangkd.yaml:...:ro`              | Mount your `klangkd.yaml` over the image's copy   |
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
      - ./klangkd.yaml:/home/klangk/etc/klangkd.yaml:ro
    cap_add:
      - SYS_ADMIN
    devices:
      - /dev/fuse
      - /dev/net/tun
    security_opt:
      - seccomp=unconfined
      - systempaths=unconfined

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

## Air-gapped networks

Deploying on a network with no internet access? See
[Air-Gapped Deployment](airgapped.md) for the offline image transport
procedure, DNS and LLM configuration, and a settings checklist.

## Next steps

- [Configuration File](../reference/klangkd-config.md) — every
  config key and its `KLANGKD_*` env-var override
- [Feature Activation](../features/features.md) — the default features and how to turn them on
