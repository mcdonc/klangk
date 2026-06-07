#!/usr/bin/env bash
set -euo pipefail
podman pull --signature-policy "${KLANGK_SIGNATURE_POLICY}" \
  ghcr.io/mcdonc/klangk/klangk-base:latest
