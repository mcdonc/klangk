#!/usr/bin/env bash
set -euo pipefail
PODMAN="${KLANGK_PODMAN_BIN:-podman}"
POLICY_ARGS=()
if [ -n "${KLANGK_SIGNATURE_POLICY:-}" ]; then
  POLICY_ARGS+=(--signature-policy "${KLANGK_SIGNATURE_POLICY}")
fi
"$PODMAN" pull "${POLICY_ARGS[@]}" \
  ghcr.io/mcdonc/klangk/klangk-base:latest
