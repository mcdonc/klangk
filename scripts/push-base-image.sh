#!/usr/bin/env bash
# Build and push a MULTI-ARCH base Docker image to GHCR.
#
# Publishes both linux/amd64 and linux/arm64 variants under a single
# manifest list so that amd64 (CI) and arm64 (Apple Silicon) machines
# each pull a native base image. A multi-arch build cannot be loaded
# into the local Docker engine, so it is built and pushed in one step
# via buildx; use build-base-image.sh for a local single-arch build.
#
# Override the published architectures with KLANGK_BASE_PLATFORMS,
# e.g. KLANGK_BASE_PLATFORMS=linux/amd64 to publish amd64 only.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

IMAGE="ghcr.io/mcdonc/klangk/klangk-workspace-base"
COMMIT="$(git rev-parse --short HEAD)"
CALVER="$(date -u +%Y.%m.%d)"
VERSION="${CALVER}-${COMMIT}"
PLATFORMS="${KLANGK_BASE_PLATFORMS:-linux/amd64,linux/arm64}"
BUILDER="klangk-multiarch"

# Check if already logged in to ghcr.io
if ! docker manifest inspect "$IMAGE:latest" >/dev/null 2>&1; then
  echo "==> Logging in to ghcr.io"
  docker login ghcr.io
fi

# A multi-platform build needs a buildx builder backed by the
# docker-container driver (the default "docker" driver is single-arch).
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
  echo "==> Creating buildx builder $BUILDER"
  docker buildx create --name "$BUILDER" --driver docker-container >/dev/null
fi

echo "==> Building and pushing $IMAGE ($PLATFORMS) version $VERSION"
docker buildx build \
  --builder "$BUILDER" \
  --platform "$PLATFORMS" \
  -f src/containers/workspace/Dockerfile.base \
  -t "$IMAGE:latest" \
  -t "$IMAGE:$VERSION" \
  --push \
  "$@" src/containers/workspace/

echo "==> Done: $IMAGE:$VERSION ($PLATFORMS)"
