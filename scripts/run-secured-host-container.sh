#!/usr/bin/env bash
# Run the klangk-host image with minimal privileges for security testing.
#
# Unlike run-host-container.sh (which uses --privileged for convenience),
# this script uses the tightest set of flags that still allow rootless
# podman to work inside the container.
#
# Usage:
#   bash scripts/run-secured-host-container.sh [extra docker-run flags...]
set -euo pipefail

ENVFILE=$(mktemp)
trap 'rm -f "$ENVFILE"' EXIT
env | grep '^KLANGK_' |
  grep -v '^KLANGK_DATA_DIR=' |
  grep -v '^KLANGK_PLUGINS_DIR=' |
  grep -v '^KLANGK_VERSION_FILE=' |
  grep -v '^KLANGK_OIDC_CONFIG=' |
  grep -v '^KLANGK_AUTH_MODES=' \
    >"$ENVFILE"
docker rm -f klangk-host-secured 2>/dev/null || true
exec docker run --name klangk-host-secured \
  -p "${KLANGK_PORT}:${KLANGK_PORT}" \
  -p "${KLANGK_NGINX_PORT}:${KLANGK_NGINX_PORT}" \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  --env-file "$ENVFILE" \
  "$@" \
  klangk-host
