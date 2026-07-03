#!/usr/bin/env bash
# Build base image locally (single arch, loaded into podman).
# Run when Dockerfile.base, apt packages, or Pi agent version changes.
# Builds for KLANGK_PLATFORM (the host arch by default) so the image
# can be loaded and run locally. To publish a multi-arch base to GHCR,
# use push-base-image.sh instead.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

COMMIT="$(git rev-parse --short HEAD)"
CALVER="$(date -u +%Y.%m.%d)"
VERSION="${CALVER}-${COMMIT}"
IMAGE="ghcr.io/mcdonc/klangk/klangk-workspace-base"

echo "==> Building base image $VERSION (${KLANGK_PLATFORM:-linux/amd64})"
"$PODMAN" build \
  "${SIG_POLICY_ARGS[@]}" \
  --platform "${KLANGK_PLATFORM:-linux/amd64}" \
  -f src/containers/workspace/Dockerfile.base \
  -t "$IMAGE:latest" \
  -t "$IMAGE:$VERSION" \
  "$@" src/containers/workspace/

echo "==> Done: $IMAGE:$VERSION"
"$PODMAN" images "$IMAGE" --format "  {{.Tag}}\t{{.Size}}"
