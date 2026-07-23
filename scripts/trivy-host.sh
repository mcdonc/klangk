#!/usr/bin/env bash
# Run Trivy vulnerability scan against the klangk-host container image.
#
# Usage:
#   bash scripts/trivy-host.sh              # scan klangk-host:latest
#   bash scripts/trivy-host.sh --severity CRITICAL,HIGH  # filter by severity
set -euo pipefail

IMAGE="${KLANGKBUILD_HOST_IMAGE:-klangk-host:latest}"

exec docker run --rm \
  -v /var/run/docker.sock:/var/run/docker.sock \
  aquasec/trivy image --scanners vuln "$@" "$IMAGE"
