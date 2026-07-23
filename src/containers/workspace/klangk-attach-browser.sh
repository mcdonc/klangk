#!/bin/sh
# Store the browser ID in the tmux global environment.
# Called by the backend after terminal_start / reattach.
#
# Usage: klangk-attach-browser <browser-id>
set -e

BROWSER_ID="${1:?Usage: klangk-attach-browser <browser-id>}"

if command -v tmux >/dev/null 2>&1 && tmux info >/dev/null 2>&1; then
  tmux set-environment -g KLANGKWS_BROWSER_ID "$BROWSER_ID"
else
  # Non-tmux fallback: write to a well-known file.
  echo "$BROWSER_ID" >/tmp/.klangk-browser-id
fi
