#!/usr/bin/env bash
set -euo pipefail

# klangkd runtime config — the single source of truth (supervisord.conf passes
# this path via `klangkd --config`). No KLANGKD_* env vars are set in the image.
CONFIG="$HOME/etc/klangkd.yaml"

# Load the embedded workspace image into podman at every startup (#3496).
# The nested podman store is a persistent bind mount, so the tag an earlier
# klangk version left behind survives a host upgrade; loading only when the
# tag is absent keeps new workspaces on that old workspace image. `podman
# load` retags the image to the version bundled with this host image:
# reloading the same version re-imports layers already in storage, and an
# upgrade replaces the stale tag while its old image stays available to the
# workspaces still running on it. The image name comes from klangkd.yaml
# (image_name), read here for the log line so the entrypoint and klangkd
# agree on which image the workspace tar provides. The python fallback
# matches klangkd's own field default.
WORKSPACE_TAR="$HOME/workspace.tar"
if [ -f "$WORKSPACE_TAR" ]; then
  IMAGE=$(python3 -c "
import yaml
d = yaml.safe_load(open('$CONFIG')) or {}
print(d.get('image_name') or d.get('image-name') or 'klangk-workspace')
" 2>/dev/null || echo klangk-workspace)
  echo "Loading workspace image $IMAGE ..."
  podman load -i "$WORKSPACE_TAR"
fi

# Load the embedded network sidecar image into podman at every startup
# (#2301, #3496). Same behavior as the workspace image above: the load is
# unconditional so a tag left in the persistent store by an earlier klangk
# version is retagged to the sidecar bundled with this host image. The image
# name comes from klangkd.yaml (network_sidecar_image), read here for the log
# line; the python fallback matches klangkd's own field default
# ("klangk-network-sidecar"), which is what the generated klangkd.yaml falls
# back to when the key is absent.
SIDECAR_TAR="$HOME/network-sidecar.tar"
if [ -f "$SIDECAR_TAR" ]; then
  SIDECAR_IMAGE=$(python3 -c "
import yaml
d = yaml.safe_load(open('$CONFIG')) or {}
print(d.get('network_sidecar_image') or d.get('network-sidecar-image') or 'klangk-network-sidecar')
" 2>/dev/null || echo klangk-network-sidecar)
  echo "Loading network sidecar image $SIDECAR_IMAGE ..."
  podman load -i "$SIDECAR_TAR"
fi

case "${1:-start}" in
start)
  exec supervisord -c "$HOME/etc/supervisord.conf"
  ;;
*)
  exec "$@"
  ;;
esac
