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

COMMIT="$(git rev-parse --short HEAD)"
CALVER="$(date -u +%Y.%m.%d)"
VERSION="${CALVER}-${COMMIT}"
IMAGE="ghcr.io/mcdonc/klangk/klangk-workspace-base"

echo "==> Building base image $VERSION (${KLANGK_PLATFORM:-linux/amd64})"
POLICY_ARGS=()
if [ -n "${KLANGK_SIGNATURE_POLICY:-}" ]; then
  POLICY_ARGS+=(--signature-policy "${KLANGK_SIGNATURE_POLICY}")
fi
"$PODMAN" build "${POLICY_ARGS[@]}" \
  --platform "${KLANGK_PLATFORM:-linux/amd64}" \
  --build-arg KLANGK_UID="$(id -u)" \
  --build-arg KLANGK_GID="$(id -g)" \
  -f src/containers/workspace/Dockerfile.base \
  -t "$IMAGE:latest" \
  -t "$IMAGE:$VERSION" \
  "$@" src/containers/workspace/

echo "==> Done: $IMAGE:$VERSION"
"$PODMAN" images "$IMAGE" --format "  {{.Tag}}\t{{.Size}}"
