#!/usr/bin/env bash
# Build the klangk-host container image via Dockerfile.
#
# Embeds git commit and build timestamp into /home/klangk/version.json.
# Requires: devenv shell (for venv), flutter build web (for frontend).
#
# Usage:
#   bash scripts/dockerbuild-host.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

COMMIT="$(git rev-parse --short HEAD)"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
IMAGE="${KLANGK_HOST_IMAGE:-klangk-host}"

echo "Building $IMAGE (commit=$COMMIT, built_at=$TIMESTAMP)..."

docker build \
  --platform linux/amd64 \
  -f src/docker/Dockerfile.host \
  --build-arg "KLANGK_BUILD_COMMIT=$COMMIT" \
  --build-arg "KLANGK_BUILD_TIMESTAMP=$TIMESTAMP" \
  --build-context "hostvenv=$DEVENV_STATE/venv" \
  -t "$IMAGE" \
  "$@" \
  .

echo "Done. Image: $IMAGE"
docker images "$IMAGE" --format "  Size: {{.Size}}"
