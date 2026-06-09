#!/usr/bin/env bash
# Run the klangk-host image locally in a Docker container.
#
# Forwards KLANGK_PORT and KLANGK_NGINX_PORT, passes through KLANGK_* env
# vars (except paths that have container-internal defaults).
#
# Usage:
#   bash scripts/run-host-container.sh [extra docker-run flags...]
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
docker rm -f klangk-host-run 2>/dev/null || true
exec docker run --name klangk-host-run \
  -p "${KLANGK_PORT}:${KLANGK_PORT}" \
  -p "${KLANGK_NGINX_PORT}:${KLANGK_NGINX_PORT}" \
  -v "${KLANGK_DATA_DIR}:/home/klangk/data" \
  --cap-add SYS_ADMIN \
  --device /dev/fuse \
  --device /dev/net/tun \
  --security-opt seccomp=unconfined \
  --security-opt systempaths=unconfined \
  --env-file "$ENVFILE" \
  "$@" \
  klangk-host
