#!/usr/bin/env bash
# Build base Docker image.
# Run when Dockerfile.base, apt packages, or Pi agent version changes.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

COMMIT="$(git rev-parse --short HEAD)"
CALVER="$(date -u +%Y.%m.%d)"
VERSION="${CALVER}-${COMMIT}"
IMAGE="ghcr.io/mcdonc/klangk/klangk-base"

echo "==> Building base image $VERSION"
docker build --platform linux/amd64 \
  --build-arg KLANGK_UID="$(id -u)" \
  --build-arg KLANGK_GID="$(id -g)" \
  -f src/docker/workspace/Dockerfile.base \
  -t "$IMAGE:latest" \
  -t "$IMAGE:$VERSION" \
  "$@" src/docker/workspace/

echo "==> Done: $IMAGE:$VERSION"
docker images "$IMAGE" --format "  {{.Tag}}\t{{.Size}}"
