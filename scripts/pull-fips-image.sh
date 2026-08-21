#!/usr/bin/env bash
# Pull the published FIPS workspace image and retag it for local use (#2631).
#
# The published image (ghcr.io/mcdonc/klangk/klangk-workspace-fips, built
# + proofed by image-workspace-fips.yml on GitHub-hosted runners) replaces
# the per-run local OpenSSL compile in CI e2e — compiling on the shared
# self-hosted host starved sibling suites' podman execs (#2631).
#
# Retags to klangk-workspace-fips:latest, the name test_fips_e2e.py and
# KLANGKD_IMAGE_NAME expect. Fails loudly (non-zero) when the pull fails so
# callers can fall back to a local, capped build.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PODMAN="${KLANGKD_PODMAN_BIN:-podman}"
# shellcheck source=_podman_common.sh disable=SC1091
source "$SCRIPT_DIR/_podman_common.sh"

SRC="${1:-ghcr.io/mcdonc/klangk/klangk-workspace-fips:latest}"
LOCAL="${KLANGKBUILD_FIPS_IMAGE_NAME:-klangk-workspace-fips}"
echo "Pulling FIPS workspace image ${SRC} ..."
"$PODMAN" pull \
  "${SIG_POLICY_ARGS[@]}" \
  "$SRC"
"$PODMAN" tag "$SRC" "${LOCAL}:latest"
echo "Tagged ${SRC} as ${LOCAL}:latest"
