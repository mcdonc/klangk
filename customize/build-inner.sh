#!/usr/bin/env bash
# Inner build script — runs inside the devenv shell.
# Called by build.sh; not intended to be run directly.
#
# Usage: build-inner.sh <plugins-dir> <workspace-tar-dir>
set -euo pipefail

PLUGINS_DIR="$1"
WORKSPACE_TAR_DIR="$2"

WORKSPACE_IMAGE="${KLANGK_IMAGE_NAME:-klangk-workspace}"
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
export KLANGK_PLUGINS_DIR="$PLUGINS_DIR"

# Fetch plugins
echo '--- Fetching plugins ---'
python3 scripts/update_plugins.py

# Build Flutter web (imports Dart plugins, rebuilds frontend)
echo '--- Building Flutter web ---'
bash scripts/flutterbuildweb.sh

# Build workspace image (stages extensions/tools, builds image)
echo '--- Building workspace image ---'
bash scripts/build-workspace-image.sh

# Export workspace image as tarball
echo '--- Exporting workspace image ---'
"$PODMAN" save -o "$WORKSPACE_TAR_DIR/workspace.tar" "$WORKSPACE_IMAGE"
