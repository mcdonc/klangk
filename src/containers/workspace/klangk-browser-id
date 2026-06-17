#!/bin/sh
# Print the current browser ID to stdout.
# Used by git-credential-klangk and Pi extensions to identify
# which browser tab to route requests to.
#
# Reads from tmux global environment first, falls back to env var,
# then to the file written by klangk-attach in non-tmux mode.
set -e

# Try tmux environment (updated on every reattach).
if command -v tmux >/dev/null 2>&1 && tmux info >/dev/null 2>&1; then
  ID=$(tmux show-environment -g KLANGK_BROWSER_ID 2>/dev/null | cut -d= -f2-)
  if [ -n "$ID" ]; then
    echo "$ID"
    exit 0
  fi
fi

# Env var fallback (set at podman exec time, may be stale).
if [ -n "$KLANGK_BROWSER_ID" ]; then
  echo "$KLANGK_BROWSER_ID"
  exit 0
fi

# File fallback (non-tmux mode).
if [ -f /tmp/.klangk-browser-id ]; then
  cat /tmp/.klangk-browser-id
  exit 0
fi

exit 1
