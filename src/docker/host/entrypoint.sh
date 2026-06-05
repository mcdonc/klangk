#!/usr/bin/env bash
set -euo pipefail

# Ensure state/data dirs exist
mkdir -p "$DEVENV_STATE/nginx" "$KLANGK_DATA_DIR"

case "${1:-start}" in
start)
  exec supervisord -c "$HOME/etc/supervisord.conf"
  ;;
*)
  exec "$@"
  ;;
esac
