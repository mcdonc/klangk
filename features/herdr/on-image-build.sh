#!/usr/bin/env bash
# Install herdr — the terminal-based agent runtime (persistent sessions,
# pane API) from ogulcancelik/herdr GitHub releases.
#
# Architecture is detected at build time via `uname -m`. This hook runs
# inside a Dockerfile RUN loop (the on-image-build feature hook), so it
# can't rely on the TARGETARCH build arg the way a top-level Dockerfile
# line can — but uname sees the real build arch either way.
set -e

HERDR_VERSION=0.6.6

case "$(uname -m)" in
x86_64 | amd64) HERDR_ARCH=x86_64 ;;
aarch64 | arm64) HERDR_ARCH=aarch64 ;;
*)
  echo "herdr: unsupported architecture: $(uname -m)" >&2
  exit 1
  ;;
esac

echo "installing herdr ${HERDR_VERSION} (${HERDR_ARCH})"
curl -fsSL -o /usr/local/bin/herdr \
  "https://github.com/ogulcancelik/herdr/releases/download/v${HERDR_VERSION}/herdr-linux-${HERDR_ARCH}"
chmod +x /usr/local/bin/herdr
