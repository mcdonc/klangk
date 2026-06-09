#!/usr/bin/env bash
#
# Build the `klangk` workspace image and seed it into the compose daemon's
# in-container rootless podman store (docs/DOCKER-COMPOSE.md).
#
# The in-container podman creates workspace containers with `--pull=never`, so
# the `klangk` image must already be in its store. This builds the image on the
# OUTER engine (docker/podman on this host) from src/containers/workspace/ and
# streams it into the running daemon container, where it persists in the
# `klangk-podman-storage` volume. Nothing is pushed to any registry; the only
# upstream dependency is a public pull of `klangk-base`.
#
# Prereqs: the stack is running (`docker compose up -d`).
#
# Env overrides:
#   ENGINE              outer container engine (default "docker"; here a podman shim)
#   COMPOSE             compose command (default "docker compose" -> podman-compose)
#   COMPOSE_SERVICE     compose service name (default "klangk")
#   KLANGK_IMAGE_NAME   image tag to build/seed (default "klangk")
#   KLANGK_PLUGINS_DIR  plugins source for staging (default: none -> empty staging)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${DEVENV_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
cd "$REPO_ROOT"

ENGINE="${ENGINE:-docker}"
COMPOSE="${COMPOSE:-docker compose}"
COMPOSE_SERVICE="${COMPOSE_SERVICE:-klangk}"
IMAGE="${KLANGK_IMAGE_NAME:-klangk}"
PLATFORM="${KLANGK_PLATFORM:-linux/amd64}"

# Stage plugin files into build-context dirs (empty staging is fine -> no
# plugins, the common case). Mirrors scripts/build-backend-image.sh.
PLUGINS_DIR="${KLANGK_PLUGINS_DIR:-$REPO_ROOT/.compose-plugins}"
STAGING="$PLUGINS_DIR/.docker"
rm -rf "$STAGING"
mkdir -p "$STAGING/extensions" "$STAGING/tools"
if [ -d "$PLUGINS_DIR" ]; then
  for d in "$PLUGINS_DIR"/*/; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    [ -f "$d/extension.ts" ] && cp "$d/extension.ts" "$STAGING/extensions/$name.ts"
    if [ -d "$d/tools" ]; then
      mkdir -p "$STAGING/tools/$name"
      cp -r "$d/tools/"* "$STAGING/tools/$name/" 2>/dev/null || true
    fi
  done
fi

echo "==> Building workspace image '$IMAGE' (pulls public klangk-base)"
$ENGINE build --platform "$PLATFORM" \
  --build-context plugin-extensions="$STAGING/extensions" \
  --build-context plugin-tools="$STAGING/tools" \
  -t "$IMAGE:latest" \
  src/containers/workspace/

# The daemon service must be up: we stream the image into its live rootless store.
if ! $COMPOSE exec -T "$COMPOSE_SERVICE" true >/dev/null 2>&1; then
  echo "error: service '$COMPOSE_SERVICE' is not running. Start it first:" >&2
  echo "         $COMPOSE up -d" >&2
  exit 1
fi

echo "==> Streaming '$IMAGE:latest' into the in-container rootless store"
# docker/podman save -> tar on stdout; piped into `podman load` inside the
# daemon container. -T disables TTY so the binary stream isn't mangled.
$ENGINE save "$IMAGE:latest" | $COMPOSE exec -T "$COMPOSE_SERVICE" podman load

echo "==> Verifying 'podman create --pull=never $IMAGE' will resolve"
if $COMPOSE exec -T "$COMPOSE_SERVICE" podman image exists "$IMAGE"; then
  echo "==> Done. '$IMAGE' is in the rootless store and persists in the"
  echo "    klangk-podman-storage volume."
else
  echo "error: image not found in the in-container store after load" >&2
  exit 1
fi
