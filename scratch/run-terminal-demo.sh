#!/usr/bin/env bash
# Run the issue #7 terminal demo (font zoom + scrollback) — NO backend needed.
#
#   scratch/run-terminal-demo.sh            # default: chrome (web)
#   scratch/run-terminal-demo.sh macos      # native macOS window
#   scratch/run-terminal-demo.sh chrome
#
# Once it opens, click the terminal to focus it, then:
#   Cmd/Ctrl + '=' or '+'   zoom in
#   Cmd/Ctrl + '-'          zoom out
#   Cmd/Ctrl + '0'          reset zoom
#   Shift + PageUp/PageDown  scroll through scrollback
#
# On web, the in-app keydown guard stops the browser from zooming the page
# while the terminal is focused. Uses the system Flutter toolchain (not devenv).
set -euo pipefail

DEVICE="${1:-chrome}"
FRONTEND_DIR="$(cd "$(dirname "$0")/../src/frontend" && pwd)"
cd "$FRONTEND_DIR"

exec env -i HOME="$HOME" \
  PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  TERM=xterm LANG=en_US.UTF-8 \
  flutter run -d "$DEVICE" -t lib/dev/terminal_zoom_demo.dart
