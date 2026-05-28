#!/usr/bin/env bash
# Build base Docker image.
# Run when Dockerfile.base, apt packages, or Pi agent version changes.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

IMAGE="ghcr.io/mcdonc/bark/bark-base:latest"

echo "==> Building base image"
docker build --platform linux/amd64 \
  --build-arg BARK_UID="$(id -u)" \
  --build-arg BARK_GID="$(id -g)" \
  -f src/docker/Dockerfile.base \
  -t "$IMAGE" "$@" src/docker/

# Requires: docker login ghcr.io
#echo "==> Pushing to GHCR"
#docker push "$IMAGE"

echo "==> Done: $IMAGE"
