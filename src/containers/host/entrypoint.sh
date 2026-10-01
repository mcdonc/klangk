#!/usr/bin/env bash
set -euo pipefail

# klangkd runtime config lives at $HOME/etc/klangkd.yaml (supervisord.conf
# passes that path via `klangkd --config`). No KLANGKD_* env vars are set in
# the image.

# Load the embedded workspace image into podman at every startup (#3496).
# The nested podman store is a persistent bind mount, so a tag left there by
# an earlier klangk version survives a host upgrade; loading only when the
# tag was absent kept every new workspace on that old workspace image.
# `podman load` retags the image bundled with this host image: an upgrade
# replaces the stale tag, and a restart on the same version re-imports
# layers already in storage (the tar is read and digest-checked on every
# start — the cost of not tracking loaded versions in the store). A failed
# load logs a warning and the host keeps starting: the previously loaded
# tag stays in place, and klangkd reports the image state when a workspace
# tries to use it.
WORKSPACE_TAR="$HOME/workspace.tar"
if [ -f "$WORKSPACE_TAR" ]; then
  echo "Loading workspace image from $WORKSPACE_TAR ..."
  if ! podman load -i "$WORKSPACE_TAR"; then
    echo "Warning: podman load of $WORKSPACE_TAR failed; continuing with the previously loaded image" >&2
  fi
fi

# Load the embedded network sidecar image into podman at every startup
# (#2301, #3496) — same behavior as the workspace image above: the load is
# unconditional so a tag left in the persistent store by an earlier klangk
# version is retagged to the sidecar bundled with this host image.
SIDECAR_TAR="$HOME/network-sidecar.tar"
if [ -f "$SIDECAR_TAR" ]; then
  echo "Loading network sidecar image from $SIDECAR_TAR ..."
  if ! podman load -i "$SIDECAR_TAR"; then
    echo "Warning: podman load of $SIDECAR_TAR failed; continuing with the previously loaded image" >&2
  fi
fi

# Garbage-collect images displaced by the retags above: each host upgrade
# leaves the previous version's images dangling in the persistent store
# (#3496). `podman image prune` removes only dangling images no container
# references, so workspaces still running on an old image keep it; the
# prune is best-effort.
podman image prune -f >/dev/null 2>&1 ||
  echo "Warning: podman image prune failed; continuing" >&2

case "${1:-start}" in
start)
  exec supervisord -c "$HOME/etc/supervisord.conf"
  ;;
*)
  exec "$@"
  ;;
esac
