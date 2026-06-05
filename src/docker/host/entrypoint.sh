#!/usr/bin/env bash
set -euo pipefail

# Ensure state/data dirs exist
mkdir -p "${KLANGK_STATE_DIR:-${DEVENV_STATE:-/tmp/klangk-state}}/nginx" "$KLANGK_DATA_DIR"

case "${1:-start}" in
start)
  exec supervisord -c "$HOME/etc/supervisord.conf"
  ;;
*)
  exec "$@"
  ;;
esac
