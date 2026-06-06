#!/usr/bin/env bash
# Build the klangk-host container image via Dockerfile.
#
# Embeds version info (CalVer + commit) into the image.
# Requires: devenv shell (for venv), flutter build web (for frontend).
#
# Usage:
#   bash scripts/dockerbuild-host.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

COMMIT="$(git rev-parse --short HEAD)"
CALVER="$(date -u +%Y.%m.%d)"
VERSION="${CALVER}-${COMMIT}"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
IMAGE="${KLANGK_HOST_IMAGE:-klangk-host}"

echo "Building $IMAGE $VERSION ..."

docker build \
  --platform "${KLANGK_PLATFORM:-linux/amd64}" \
  -f src/docker/host/Dockerfile \
  --build-arg "KLANGK_BUILD_VERSION=$VERSION" \
  --build-arg "KLANGK_BUILD_COMMIT=$COMMIT" \
  --build-arg "KLANGK_BUILD_TIMESTAMP=$TIMESTAMP" \
  --build-context "hostvenv=$DEVENV_STATE/venv" \
  -t "$IMAGE:latest" \
  -t "$IMAGE:$VERSION" \
  "$@" \
  .

echo "Done. Image: $IMAGE:$VERSION"
docker images "$IMAGE" --format "  {{.Tag}}\t{{.Size}}"
