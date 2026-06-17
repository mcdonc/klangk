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

COMMIT="$(git rev-parse --short HEAD)"
CALVER="$(date -u +%Y.%m.%d)"
VERSION="${KLANGK_BUILD_VERSION:-${CALVER}-${COMMIT}}"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
IMAGE="${KLANGK_HOST_IMAGE:-klangk-host}"

WORKSPACE_IMAGE="${KLANGK_IMAGE_NAME:-klangk-workspace}"
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
# Export workspace image so it can be embedded in the host image.
WORKSPACE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/klangk-workspace-XXXXXX")
STAGING_DIR=$(mktemp -d "${TMPDIR:-/tmp}/klangk-staging-XXXXXX")
trap 'rm -rf "$WORKSPACE_DIR" "$STAGING_DIR"' EXIT
echo "Exporting workspace image $WORKSPACE_IMAGE from podman ..."
"$PODMAN" save -o "$WORKSPACE_DIR/workspace.tar" "$WORKSPACE_IMAGE"

# Stage plugin directories (skip generated dirs like .dart/, .docker/)
PLUGINS_STAGING="$STAGING_DIR/plugins"
mkdir -p "$PLUGINS_STAGING"
for d in "$KLANGK_PLUGINS_DIR"/*/; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  [[ $name == .* ]] && continue
  cp -r "$d" "$PLUGINS_STAGING/$name"
done

echo "Building $IMAGE $VERSION ..."

docker build \
  --platform "${KLANGK_PLATFORM:-linux/amd64}" \
  -f src/containers/host/Dockerfile \
  --build-arg "KLANGK_BUILD_VERSION=$VERSION" \
  --build-arg "KLANGK_BUILD_COMMIT=$COMMIT" \
  --build-arg "KLANGK_BUILD_TIMESTAMP=$TIMESTAMP" \
  --build-context "hostvenv=$DEVENV_STATE/venv" \
  --build-context "workspace-image=$WORKSPACE_DIR" \
  --build-context "plugins=$PLUGINS_STAGING" \
  -t "$IMAGE:latest" \
  -t "$IMAGE:$VERSION" \
  "$@" \
  .

echo "Done. Image: $IMAGE:$VERSION"
docker images "$IMAGE" --format "  {{.Tag}}\t{{.Size}}"
