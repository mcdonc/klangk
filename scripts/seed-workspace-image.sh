#!/usr/bin/env bash
#
# Seed the locally-built workspace image into the daemon stack's rootless
# podman store (DOCKER.md §9). Fully local — NOTHING is pushed to any registry.
#
# The daemon's in-container podman runs with `--pull=never` by default, so the
# `klangk` image must already be in its store. This transfers the image you
# built with scripts/dockerbuild.sh into the running backend container's
# rootless store, where it persists in the `klangk-podman-storage` volume.
#
# Prereqs:
#   - the image is built locally:  scripts/dockerbuild.sh   (pulls public
#     klangk-base; no credentials, no push)
#   - the stack is running:        docker compose up -d
#
# Usage:
#   scripts/seed-workspace-image.sh [IMAGE]      # IMAGE defaults to $KLANGK_IMAGE_NAME or "klangk"
#
# Env overrides:
#   KLANGK_IMAGE_NAME   image tag to seed (default "klangk")
#   COMPOSE_FILE        compose file (default docker-compose.yml at repo root)
#   BACKEND_SERVICE     compose service name (default "backend")
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${DEVENV_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
cd "$REPO_ROOT"

IMAGE="${1:-${KLANGK_IMAGE_NAME:-klangk}}"
BACKEND_SERVICE="${BACKEND_SERVICE:-backend}"
TAG="$IMAGE:latest"
TAR="/tmp/klangk-seed-$$.tar"
REMOTE_TAR="/tmp/klangk-seed-$$.tar"

cleanup() {
  rm -f "$TAR" 2>/dev/null || true
  docker compose exec -T --user root "$BACKEND_SERVICE" rm -f "$REMOTE_TAR" 2>/dev/null || true
}
trap cleanup EXIT

# 1. The image must exist in the host docker engine (build it first).
if ! docker image inspect "$TAG" >/dev/null 2>&1; then
  echo "error: image '$TAG' not found in docker. Build it first:" >&2
  echo "         scripts/dockerbuild.sh" >&2
  exit 1
fi

# 2. The backend service must be running (we load into its live rootless store).
if ! docker compose exec -T "$BACKEND_SERVICE" true >/dev/null 2>&1; then
  echo "error: backend service '$BACKEND_SERVICE' is not running. Start it:" >&2
  echo "         docker compose up -d" >&2
  exit 1
fi

echo "==> Saving $TAG"
docker save "$TAG" -o "$TAR"

echo "==> Copying into the backend container"
docker compose cp "$TAR" "$BACKEND_SERVICE:$REMOTE_TAR"
# docker cp lands the file root-owned; the rootless user must be able to read it.
docker compose exec -T --user root "$BACKEND_SERVICE" chmod 644 "$REMOTE_TAR"

echo "==> Loading into the rootless podman store"
docker compose exec -T "$BACKEND_SERVICE" podman load -i "$REMOTE_TAR"

echo "==> Verifying 'podman create --pull=never $IMAGE' resolves"
if docker compose exec -T "$BACKEND_SERVICE" podman image exists "$IMAGE"; then
  echo "==> Done. '$IMAGE' is in the rootless store and persists in the"
  echo "    klangk-podman-storage volume."
else
  echo "error: image not found in the store after load" >&2
  exit 1
fi
