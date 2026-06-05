#!/usr/bin/env bash
# Build base Docker image.
# Run when Dockerfile.base, apt packages, or Pi agent version changes.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

IMAGE="ghcr.io/mcdonc/klangk/klangk-base:latest"

echo "==> Building base image"
docker build --platform linux/amd64 \
  --build-arg KLANGK_UID="$(id -u)" \
  --build-arg KLANGK_GID="$(id -g)" \
  -f src/docker/workspace/Dockerfile.base \
  -t "$IMAGE" "$@" src/docker/workspace/

# Requires: docker login ghcr.io
#echo "==> Pushing to GHCR"
#docker push "$IMAGE"

echo "==> Done: $IMAGE"
