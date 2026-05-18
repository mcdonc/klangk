#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

# Auto-fetch plugins on first run
if [ -f "$BARK_PLUGINS_DIR/plugins.yaml" ] && [ ! -f "$BARK_PLUGINS_DIR/plugins.lock" ]; then
  echo "No plugins.lock found, running update-plugins..."
  python3 scripts/update_plugins.py
fi

# Stage plugin files outside the source tree
STAGING="$BARK_PLUGINS_DIR/.docker"
rm -rf "$STAGING"
mkdir -p "$STAGING/extensions" "$STAGING/tools"
for d in "$BARK_PLUGINS_DIR"/*/; do
  [ -d "$d" ] || continue
  name=$(basename "$d")
  [ -f "$d/extension.ts" ] && cp "$d/extension.ts" "$STAGING/extensions/$name.ts"
  if [ -d "$d/tools" ]; then
    mkdir -p "$STAGING/tools/$name"
    cp -r "$d/tools/"* "$STAGING/tools/$name/" 2>/dev/null
  fi
done

# Remove old containers before rebuilding so they get recreated from the new image
docker ps -a --filter "label=bark.instance=${BARK_INSTANCE_ID}" -q | xargs -r docker rm -f

# Pull latest base image in case it was updated by CI
docker pull ghcr.io/mcdonc/bark/bark-pi-base:latest || true

# Build workspace image on top of the base
docker build --platform linux/amd64 \
  --build-context plugin-extensions="$STAGING/extensions" \
  --build-context plugin-tools="$STAGING/tools" \
  -t "${BARK_IMAGE_NAME}" "$@" src/dockerimage/
