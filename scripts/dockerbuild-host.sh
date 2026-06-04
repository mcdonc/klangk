#!/usr/bin/env bash
# Build the klangk-host container image via devenv/nix2container.
#
# Embeds git commit and build timestamp into /version.json inside the image.
# Requires: devenv shell (for venv), NIX_CONFIG="pure-eval = false" (for venv copy).
#
# Usage:
#   bash scripts/dockerbuild-host.sh          # build + load into docker
#   bash scripts/dockerbuild-host.sh --no-load  # build only (print nix store path)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

no_load=false
for arg in "$@"; do
  case "$arg" in
  --no-load) no_load=true ;;
  esac
done

export KLANGK_BUILD_COMMIT
KLANGK_BUILD_COMMIT="$(git rev-parse --short HEAD)"
export KLANGK_BUILD_TIMESTAMP
KLANGK_BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
export NIX_CONFIG="pure-eval = false"

echo "Building klangk-host image (commit=$KLANGK_BUILD_COMMIT, built_at=$KLANGK_BUILD_TIMESTAMP)..."
devenv container build processes

if [ "$no_load" = false ]; then
  echo "Loading image into Docker..."
  devenv container copy processes --registry docker-daemon:
  echo "Done. Image: klangk-host:latest"
  docker images klangk-host --format "  Size: {{.Size}}"
fi
