#!/usr/bin/env bash
set -euo pipefail

# Ensure state/data dirs exist
mkdir -p "${KLANGK_STATE_DIR:-${DEVENV_STATE:-/tmp/klangk-state}}" "$KLANGK_DATA_DIR"

# Load the embedded workspace image into podman on first startup.
WORKSPACE_TAR="$HOME/workspace.tar"
if [ -f "$WORKSPACE_TAR" ]; then
  IMAGE="${KLANGK_IMAGE_NAME:-klangk-workspace}"
  if ! podman image exists "$IMAGE" 2>/dev/null; then
    echo "Loading workspace image $IMAGE ..."
    podman load -i "$WORKSPACE_TAR"
  fi
fi

case "${1:-start}" in
start)
  exec supervisord -c "$HOME/etc/supervisord.conf"
  ;;
*)
  exec "$@"
  ;;
esac
