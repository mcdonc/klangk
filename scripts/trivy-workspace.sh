#!/usr/bin/env bash
# Run Trivy vulnerability scan against the klangk-workspace container image.
#
# Usage:
#   trivy-workspace                              # scan klangk-workspace:latest
#   trivy-workspace --severity CRITICAL,HIGH     # filter by severity
#   trivy-workspace --format json                # JSON output
set -euo pipefail

IMAGE="${KLANGKD_IMAGE_NAME:-klangk-workspace}:latest"

# Save the image to a tarball so trivy can scan it without needing
# access to the podman storage directly.
TMPTAR=$(mktemp /tmp/trivy-workspace-XXXXXX.tar)
trap 'rm -f "$TMPTAR"' EXIT

echo "Exporting $IMAGE to tarball..." >&2
podman save -o "$TMPTAR" "$IMAGE"

echo "Scanning $IMAGE..." >&2
exec podman run --rm \
  -v "$TMPTAR:/image.tar:ro" \
  docker.io/aquasec/trivy image --scanners vuln --input /image.tar "$@"
