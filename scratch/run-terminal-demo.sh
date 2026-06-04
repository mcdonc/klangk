#!/usr/bin/env bash
# Run the issue #7 terminal demo (font zoom + scrollback) — NO backend needed.
#
#   scratch/run-terminal-demo.sh            # default: chrome (web)
#   scratch/run-terminal-demo.sh macos      # native macOS window
#   scratch/run-terminal-demo.sh linux      # native Linux (GTK) window
#   scratch/run-terminal-demo.sh chrome
#
# Once it opens, click the terminal to focus it, then:
#   Cmd/Ctrl + '=' or '+'    zoom in
#   Cmd/Ctrl + '-'           zoom out
#   Cmd/Ctrl + '0'           reset zoom
#   Shift + PageUp/PageDown  scroll through scrollback
#
# On web, the in-app keydown guard stops the browser from zooming the page
# while the terminal is focused.
#
# Toolchain: uses the system Flutter (NOT devenv).
#   - macOS native (-d macos): needs Xcode; run from a system shell.
#   - Linux native (-d linux): needs GTK3 dev libs + clang + cmake + ninja +
#     pkg-config. The libghostty native lib is fetched prebuilt for
#     x86_64/aarch64 glibc (no Zig needed). For headless/CI, wrap with:
#       xvfb-run -a -s '-screen 0 1280x1024x24 +extension GLX' \
#         scratch/run-terminal-demo.sh linux
set -euo pipefail

DEVICE="${1:-chrome}"
FRONTEND_DIR="$(cd "$(dirname "$0")/../src/frontend" && pwd)"
cd "$FRONTEND_DIR"

# devenv pollutes these on macOS and breaks the native build hook (xcrun);
# unsetting them is harmless on Linux.
unset SDKROOT DEVELOPER_DIR 2>/dev/null || true
# Prefer a Homebrew Flutter on macOS; on Linux this path simply won't exist and
# the ambient PATH (e.g. /usr/bin, snap) is used.
export PATH="/opt/homebrew/bin:$PATH"

exec flutter run -d "$DEVICE" -t lib/dev/terminal_zoom_demo.dart
