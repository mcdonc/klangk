#!/usr/bin/env bash
# Build the klangk-host container image via Dockerfile.
#
# Builds all prerequisites (flutter web, workspace image, network sidecar
# image) then embeds both image tarballs in the host image.
#
# Usage:
#   bash scripts/build-host-image.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

bash "$SCRIPT_DIR/flutterbuildweb.sh"

# Build the klangk wheel locally (#1603). flutterbuildweb.sh populated
# src/frontend/build/web, which the hatch build hook bundles into the wheel
# at klangk/frontend (#1600). Clean dist/ first so the Dockerfile's wheel
# glob matches exactly one file.
rm -rf src/klangk/dist
bash "$SCRIPT_DIR/build_wheel.sh"

bash "$SCRIPT_DIR/build-workspace-image.sh"
bash "$SCRIPT_DIR/build-network-sidecar.sh"

VERSION="$(jq -r .version "$KLANGKD_VERSION_FILE")"
IMAGE="${KLANGKBUILD_HOST_IMAGE:-klangk-host}"

# Copy version file into build context for Dockerfile COPY
cp "$KLANGKD_VERSION_FILE" version.json

WORKSPACE_IMAGE="${KLANGKD_IMAGE_NAME:-klangk-workspace}"
# Must match the tag in scripts/build-network-sidecar.sh and the default of
# the network_sidecar_image setting (settings.py) so all three agree on the
# name the embedded tar provides.
SIDECAR_IMAGE="klangk-network-sidecar"
PODMAN="${KLANGKD_PODMAN_BIN:-podman}"
# Export workspace image so it can be embedded in the host image. The host
# image no longer stages feature trees (#1660/#1665) — the runtime reads
# features.json from the frontend bundle, and the workspace image (built
# above) already bakes feature trees in for Pi — so there's no separate
# staging tempdir here anymore, just the workspace tarball.
WORKSPACE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/klangk-workspace-XXXXXX")
SIDECAR_DIR=$(mktemp -d "${TMPDIR:-/tmp}/klangk-sidecar-XXXXXX")
trap 'rm -rf "$WORKSPACE_DIR" "$SIDECAR_DIR"' EXIT
echo "Exporting workspace image $WORKSPACE_IMAGE from podman ..."
"$PODMAN" save -o "$WORKSPACE_DIR/workspace.tar" "$WORKSPACE_IMAGE"
echo "Exporting network sidecar image $SIDECAR_IMAGE from podman ..."
"$PODMAN" save -o "$SIDECAR_DIR/network-sidecar.tar" "$SIDECAR_IMAGE"

echo "Building $IMAGE $VERSION ..."

docker build \
  --platform "${KLANGKBUILD_PLATFORM:-linux/amd64}" \
  -f src/containers/host/Dockerfile \
  --build-context "workspace-image=$WORKSPACE_DIR" \
  --build-context "network-sidecar-image=$SIDECAR_DIR" \
  -t "$IMAGE:latest" \
  -t "$IMAGE:$VERSION" \
  "$@" \
  .

echo "Done. Image: $IMAGE:$VERSION"
docker images "$IMAGE" --format "  {{.Tag}}\t{{.Size}}"
