# Host Container

The host container is a self-contained deployment image. It packages the backend, nginx proxy, Flutter web UI, and the workspace image into a single Docker image based on `python:3.13-slim`. Workspace containers are launched inside the host container via rootless podman (pasta networking).

The published image is available from GHCR (replace the version tag with the desired release):

```bash
docker pull ghcr.io/mcdonc/klangk/klangk-host:v2026.06.10
```

## Running

```bash
docker run -d \
  -p 8995:8995 -p 8997:8997 \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  -e KLANGK_DEFAULT_USER=admin@example.com \
  -e KLANGK_DEFAULT_PASSWORD=admin \
  -e KLANGK_JWT_SECRET=change-me \
  ghcr.io/mcdonc/klangk/klangk-host:v2026.06.10
```

Open http://localhost:8995. On first startup the embedded workspace image is automatically loaded into podman.

The five Docker flags are required for rootless podman to create workspace containers inside the host container. They grant mount capabilities (`SYS_ADMIN`), FUSE filesystem access (`/dev/fuse`), pasta networking (`/dev/net/tun`), and remove default restrictions on syscalls and `/proc` that block nested container creation.

Data is stored in `/home/klangk/data` inside the container. To persist across restarts, mount a volume:

```bash
docker run -d -v klangk-data:/home/klangk/data ...
```

## Building Locally

```bash
build-host-image
```

This builds everything from source: Flutter web, workspace image (podman), then the host image (Docker). Tagged locally with `latest` and a CalVer version (e.g., `2026.06.09-abc1234`). Only the CalVer version tag is pushed to GHCR — `:latest` is never pushed to the registry. The version is baked into `/home/klangk/version.json` and served at `GET /version`.

## Custom Image with Plugins

To build a host image with plugins baked in, see [klangk-host-with-plugins](https://github.com/mcdonc/klangk-host-with-plugins) for an example. It clones klangk at a given ref, fetches plugins, rebuilds the Flutter web frontend and workspace image with plugin support, then layers the results on top of the released host image.

## Scanning

```bash
trivy-host                        # full vulnerability scan
trivy-host --severity CRITICAL    # critical only
```

## Image Versioning

**No `:latest` tags are pushed to the registry.** Every image (host, workspace, workspace base) is pushed only with an explicit version tag. This prevents confusion when stable branches would otherwise overwrite `:latest` with an older version. Consumers always reference a specific version via `KLANGK_REF` or build locally.

Locally, `build-workspace-image` still tags `klangk-workspace:latest` in the local podman store — this is the tag the backend uses at runtime (with pull policy `never`). The local `:latest` tag is never pushed to GHCR.

## Workspace Base Image Pin

The workspace `Dockerfile` pins its base image to a specific version via a build `ARG`:

```dockerfile
ARG WORKSPACE_BASE_IMAGE=ghcr.io/mcdonc/klangk/klangk-workspace-base:2026.06.10-e973f3c
FROM $WORKSPACE_BASE_IMAGE
```

This means changes to `Dockerfile.base` on main don't silently affect other branches. The flow:

1. Someone changes `Dockerfile.base` and pushes to main.
2. The `image-workspace-base.yml` workflow builds and pushes the new base image with a versioned tag.
3. The same workflow automatically opens a PR to update the `ARG` default in `src/containers/workspace/Dockerfile` to the new version.
4. A maintainer reviews and merges the PR.

Stable/deploy branches keep their original pinned base version and are unaffected. To override at build time: `--build-arg WORKSPACE_BASE_IMAGE=ghcr.io/.../klangk-workspace-base:some-version`.
