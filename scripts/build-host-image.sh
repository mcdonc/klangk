#!/usr/bin/env bash
# Build the klangk-host container image via Dockerfile.
#
# Builds all prerequisites (flutter web, workspace image) then embeds
# the workspace image tarball in the host image.
#
# Usage:
#   bash scripts/build-host-image.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

bash "$SCRIPT_DIR/flutterbuildweb.sh"
bash "$SCRIPT_DIR/build-workspace-image.sh"

VERSION="$(jq -r .version "$KLANGK_VERSION_FILE")"
IMAGE="${KLANGK_HOST_IMAGE:-klangk-host}"

# Copy version file into build context for Dockerfile COPY
cp "$KLANGK_VERSION_FILE" version.json

WORKSPACE_IMAGE="${KLANGK_IMAGE_NAME:-klangk-workspace}"
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
# Export workspace image so it can be embedded in the host image.
WORKSPACE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/klangk-workspace-XXXXXX")
STAGING_DIR=$(mktemp -d "${TMPDIR:-/tmp}/klangk-staging-XXXXXX")
trap 'rm -rf "$WORKSPACE_DIR" "$STAGING_DIR"' EXIT
echo "Exporting workspace image $WORKSPACE_IMAGE from podman ..."
"$PODMAN" save -o "$WORKSPACE_DIR/workspace.tar" "$WORKSPACE_IMAGE"

echo "Building $IMAGE $VERSION ..."

docker build \
  --platform "${KLANGK_PLATFORM:-linux/amd64}" \
  -f src/containers/host/Dockerfile \
  --build-context "hostvenv=$DEVENV_STATE/venv" \
  --build-context "workspace-image=$WORKSPACE_DIR" \
  -t "$IMAGE:latest" \
  -t "$IMAGE:$VERSION" \
  "$@" \
  .

echo "Done. Image: $IMAGE:$VERSION"
docker images "$IMAGE" --format "  {{.Tag}}\t{{.Size}}"
