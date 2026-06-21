# Building Images

## Host Image

```bash
build-host-image
```

This builds everything from source: Flutter web, workspace image
(podman), then the host image (Docker). Tagged locally with `latest`
and a version tag derived from git state (release tag, branch name,
or commit). Only the version tag is pushed to GHCR — `:latest` is
never pushed to the registry. The version is baked into
`/home/klangk/version.json` and served at `GET /api/v1/version`.

## Custom Image with Plugins

To build a host image with plugins, CA certificates, or OIDC hooks
baked in, see [Customizing a Deployment](../deployment/customizing.md).

## Scanning

```bash
trivy-host                        # scan host image
trivy-workspace                   # scan workspace image
trivy-host --severity CRITICAL    # critical only
```

## Image Versioning

**No `:latest` tags are pushed to the registry.** Every image (host,
workspace, workspace base) is pushed only with an explicit version
tag. This prevents confusion when stable branches would otherwise
overwrite `:latest` with an older version. Consumers always reference
a specific version via `KLANGK_REF` or build locally.

Locally, `build-workspace-image` tags `klangk-workspace:latest`
(used by the backend at runtime with pull policy `never`) and a
deterministic version tag (`YYYY.MM.DD-<commit>`). Stale version
tags from previous builds are automatically removed so they don't
accumulate. The local `:latest` tag is never pushed to GHCR.

## Workspace Base Image Pin

The workspace `Dockerfile` pins its base image to a specific version
via a build `ARG`:

```dockerfile
ARG WORKSPACE_BASE_IMAGE=ghcr.io/mcdonc/klangk/klangk-workspace-base:2026.06.10-e973f3c
FROM $WORKSPACE_BASE_IMAGE
```

This means changes to `Dockerfile.base` on main don't silently
affect other branches. The flow:

1. Someone changes `Dockerfile.base` and pushes to main.
2. The `image-workspace-base.yml` workflow builds and pushes the new
   base image with a versioned tag.
3. The same workflow automatically opens a PR to update the `ARG`
   default in `src/containers/workspace/Dockerfile` to the new
   version.
4. A maintainer reviews and merges the PR.

Stable/deploy branches keep their original pinned base version and
are unaffected. To override at build time:
`--build-arg WORKSPACE_BASE_IMAGE=ghcr.io/.../klangk-workspace-base:some-version`.
