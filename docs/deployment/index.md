# Host Container

The host container is a self-contained deployment image. It packages
the backend, nginx proxy, Flutter web UI, and the workspace image
into a single Docker image based on `python:3.13-slim`. Workspace
containers are launched inside the host container via rootless podman
(pasta networking).

The published image is available from GHCR:

```bash
docker pull ghcr.io/mcdonc/klangk/klangk-host:v2026.06.10
```

See [Running with Docker](docker.md) for how to run it, and
[Customizing](customizing.md) for building a custom image with
plugins and CA certificates.

For building the host image from source, see
[Building Images](../development/building-images.md).
