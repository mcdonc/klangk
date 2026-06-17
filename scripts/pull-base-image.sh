#!/usr/bin/env bash
set -euo pipefail
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
"$PODMAN" pull \
  ghcr.io/mcdonc/klangk/klangk-workspace-base:latest
