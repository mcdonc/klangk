#!/usr/bin/env bash
# Build the klangk-host container image via Dockerfile.
#
# Builds all prerequisites (flutter web, workspace image) unless pulling
# the workspace image from a registry via KLANGK_WORKSPACE_REGISTRY.
#
# Usage:
#   bash scripts/build-host-image.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

# Build prerequisites unless using a registry workspace image.
if [ -z "${KLANGK_WORKSPACE_REGISTRY:-}" ]; then
  bash "$SCRIPT_DIR/flutterbuildweb.sh"
  bash "$SCRIPT_DIR/build-backend-image.sh"
fi

COMMIT="$(git rev-parse --short HEAD)"
CALVER="$(date -u +%Y.%m.%d)"
VERSION="${CALVER}-${COMMIT}"
TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
IMAGE="${KLANGK_HOST_IMAGE:-klangk-host}"

WORKSPACE_IMAGE="${KLANGK_IMAGE_NAME:-klangk}"
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
POLICY_ARGS=()
if [ -n "${KLANGK_SIGNATURE_POLICY:-}" ]; then
  POLICY_ARGS+=(--signature-policy "${KLANGK_SIGNATURE_POLICY}")
fi

# Export workspace image so it can be embedded in the host image.
# Use local podman image if available, otherwise pull from GHCR via docker.
WORKSPACE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/klangk-workspace-XXXXXX")
trap 'rm -rf "$WORKSPACE_DIR"' EXIT
if "$PODMAN" image exists "$WORKSPACE_IMAGE" 2>/dev/null; then
  echo "Exporting workspace image $WORKSPACE_IMAGE from podman ..."
  "$PODMAN" save "${POLICY_ARGS[@]}" -o "$WORKSPACE_DIR/workspace.tar" "$WORKSPACE_IMAGE"
elif [ -n "${KLANGK_WORKSPACE_REGISTRY:-}" ]; then
  echo "Pulling workspace image from $KLANGK_WORKSPACE_REGISTRY ..."
  docker pull "$KLANGK_WORKSPACE_REGISTRY"
  # Retag so the embedded tarball uses the local image name, matching
  # KLANGK_IMAGE_NAME inside the host container.
  docker tag "$KLANGK_WORKSPACE_REGISTRY" "$WORKSPACE_IMAGE"
  docker save -o "$WORKSPACE_DIR/workspace.tar" "$WORKSPACE_IMAGE"
else
  echo "ERROR: workspace image '$WORKSPACE_IMAGE' not found in podman"
  echo "  Build it first: devenv shell -- build-backend-image"
  echo "  Or set KLANGK_WORKSPACE_REGISTRY to pull from a registry"
  exit 1
fi

echo "Building $IMAGE $VERSION ..."

docker build \
  --platform "${KLANGK_PLATFORM:-linux/amd64}" \
  -f src/containers/host/Dockerfile \
  --build-arg "KLANGK_BUILD_VERSION=$VERSION" \
  --build-arg "KLANGK_BUILD_COMMIT=$COMMIT" \
  --build-arg "KLANGK_BUILD_TIMESTAMP=$TIMESTAMP" \
  --build-context "hostvenv=$DEVENV_STATE/venv" \
  --build-context "workspace-image=$WORKSPACE_DIR" \
  -t "$IMAGE:latest" \
  -t "$IMAGE:$VERSION" \
  "$@" \
  .

echo "Done. Image: $IMAGE:$VERSION"
docker images "$IMAGE" --format "  {{.Tag}}\t{{.Size}}"
