#!/usr/bin/env bash
# Build the nix-enabled workspace image locally (single arch, loaded into
# podman). Extends the full workspace image (Dockerfile — Pi agent, features,
# klangk-* tooling) with single-user nix and devenv (#2199). Runtime /nix
# overlay + PATH wiring is #2198.
#
# Builds for KLANGKBUILD_PLATFORM (the host arch by default) so the image
# can be loaded and run locally.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"
PODMAN="${KLANGKD_PODMAN_BIN:-podman}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

WORKSPACE_IMAGE="${KLANGKD_IMAGE_NAME:-klangk-workspace}"

# The nix image layers on the full workspace image, so make sure that base
# exists locally first. Build it on demand if it's missing.
if ! "$PODMAN" image exists "${WORKSPACE_IMAGE}:latest" 2>/dev/null; then
  echo "==> Base workspace image ${WORKSPACE_IMAGE}:latest not found; building it"
  "$SCRIPT_DIR/build-workspace-image.sh"
fi

COMMIT="$(git rev-parse --short HEAD)"
CALVER="$(date -u +%Y.%m.%d)"
VERSION="${CALVER}-${COMMIT}"
IMAGE="klangk-workspace-nix"

echo "==> Building nix workspace image $VERSION (${KLANGKBUILD_PLATFORM:-linux/amd64})"
"$PODMAN" build \
  "${SIG_POLICY_ARGS[@]}" \
  --platform "${KLANGKBUILD_PLATFORM:-linux/amd64}" \
  --build-arg WORKSPACE_IMAGE="${WORKSPACE_IMAGE}:latest" \
  -f src/containers/workspace/Dockerfile-nix \
  -t "$IMAGE:latest" \
  -t "$IMAGE:$VERSION" \
  "$@" src/containers/workspace/

echo "==> Done: $IMAGE:$VERSION"
"$PODMAN" images "$IMAGE" --format "  {{.Tag}}\t{{.Size}}"
