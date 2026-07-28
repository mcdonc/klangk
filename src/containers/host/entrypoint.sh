#!/usr/bin/env bash
set -euo pipefail

# klangkd runtime config — the single source of truth (supervisord.conf passes
# this path via `klangkd --config`). No KLANGKD_* env vars are set in the image.
CONFIG="$HOME/etc/klangkd.yaml"

# Load the embedded workspace image into podman on first startup. The image
# name comes from klangkd.yaml (image_name), read here so the entrypoint and
# klangkd agree on which image the workspace tar provides. The python fallback
# matches klangkd's own field default.
WORKSPACE_TAR="$HOME/workspace.tar"
if [ -f "$WORKSPACE_TAR" ]; then
  IMAGE=$(python3 -c "
import yaml
d = yaml.safe_load(open('$CONFIG')) or {}
print(d.get('image_name') or d.get('image-name') or 'klangk-workspace')
" 2>/dev/null || echo klangk-workspace)
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
