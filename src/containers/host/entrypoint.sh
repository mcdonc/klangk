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

# Load the embedded network sidecar image into podman on first startup (#2301).
# Same pattern as the workspace image above: the image name comes from
# klangkd.yaml (network_sidecar_image), read here so the entrypoint and klangkd
# agree on which image the sidecar tar provides. The python fallback matches
# klangkd's own field default ("klangk-network-sidecar"), which is what the
# generated klangkd.yaml falls back to when the key is absent.
SIDECAR_TAR="$HOME/network-sidecar.tar"
if [ -f "$SIDECAR_TAR" ]; then
  SIDECAR_IMAGE=$(python3 -c "
import yaml
d = yaml.safe_load(open('$CONFIG')) or {}
print(d.get('network_sidecar_image') or d.get('network-sidecar-image') or 'klangk-network-sidecar')
" 2>/dev/null || echo klangk-network-sidecar)
  if ! podman image exists "$SIDECAR_IMAGE" 2>/dev/null; then
    echo "Loading network sidecar image $SIDECAR_IMAGE ..."
    podman load -i "$SIDECAR_TAR"
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
