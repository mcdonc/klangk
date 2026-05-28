#!/usr/bin/env bash
# Push the base Docker image to GHCR.
# Logs in if not already authenticated.
set -euo pipefail

IMAGE="ghcr.io/mcdonc/bark/bark-base:latest"

# Check if already logged in to ghcr.io
if ! docker manifest inspect "$IMAGE" >/dev/null 2>&1; then
  echo "==> Logging in to ghcr.io"
  docker login ghcr.io
fi

echo "==> Pushing $IMAGE"
docker push "$IMAGE"
echo "==> Done"
