#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

STAMP="$DEVENV_STATE/klangk/.backend-image-hash"

# Compute a hash of all files that affect the workspace image.
CURRENT_HASH=$(find \
  scripts/build-backend-image.sh \
  src/containers/workspace/ \
  "$KLANGK_PLUGINS_DIR" \
  -type f 2>/dev/null | sort | xargs sha256sum 2>/dev/null | sha256sum | cut -d' ' -f1)

# Skip rebuild if the image exists and the hash hasn't changed.
if "$PODMAN" image exists "${KLANGK_IMAGE_NAME}" 2>/dev/null && [ -f "$STAMP" ]; then
  OLD_HASH=$(cat "$STAMP" 2>/dev/null || true)
  if [ "$CURRENT_HASH" = "$OLD_HASH" ]; then
    echo "Image ${KLANGK_IMAGE_NAME} is up to date, skipping build."
    exit 0
  fi
fi

# Auto-fetch plugins on first run
if [ -f "$KLANGK_PLUGINS_DIR/plugins.yaml" ] && [ ! -f "$KLANGK_PLUGINS_DIR/plugins.lock" ]; then
  echo "No plugins.lock found, running update-plugins..."
  python3 scripts/update_plugins.py
fi

# Stage full plugin directories outside the source tree
STAGING="$KLANGK_PLUGINS_DIR/.docker"
rm -rf "$STAGING"
mkdir -p "$STAGING/plugins"
for d in "$KLANGK_PLUGINS_DIR"/*/; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  cp -r "$d" "$STAGING/plugins/$name"
done

# Remove old containers before rebuilding so they get recreated from the new image.
# Skip when running inside a container (developing klangk in klangk).
if [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ]; then
  INSTANCE_ID=$(klangk-instance-id 2>/dev/null || true)
  if [ -n "${INSTANCE_ID:-}" ]; then
    "$PODMAN" ps -a --filter "label=klangk.instance=${INSTANCE_ID}" -q | xargs -r "$PODMAN" rm -f
  else
    "$PODMAN" ps -a --filter "label=klangk.managed=true" -q | xargs -r "$PODMAN" rm -f
  fi
fi

# Build workspace image on top of the base
"$PODMAN" build \
  "${SIG_POLICY_ARGS[@]}" \
  --pull=newer \
  --platform "${KLANGK_PLATFORM:-linux/amd64}" \
  --build-context plugins="$STAGING/plugins" \
  -t "${KLANGK_IMAGE_NAME}" "$@" src/containers/workspace/

echo "$CURRENT_HASH" >"$STAMP"
