#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PODMAN="${KLANGKD_PODMAN_BIN:-podman}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"
"$PODMAN" pull \
  "${SIG_POLICY_ARGS[@]}" \
  ghcr.io/mcdonc/klangk/klangk-workspace-base:latest
