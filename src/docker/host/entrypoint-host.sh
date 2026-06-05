#!/usr/bin/env bash
set -euo pipefail

# Ensure state/data dirs exist
mkdir -p "$DEVENV_STATE/nginx" "$KLANGK_DATA_DIR"

case "${1:-start}" in
start)
  # Start nginx in background (may fail if LLM proxy vars unset)
  "$HOME/bin/nginx" &
  # Start uvicorn in foreground
  exec "$HOME/bin/klangk"
  ;;
*)
  exec "$@"
  ;;
esac
