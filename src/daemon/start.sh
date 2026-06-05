#!/bin/sh
set -e

exec uvicorn klangk_backend.main:app \
    --host 0.0.0.0 \
    --port "${KLANGK_PORT:-8997}" \
    --ws-max-size 65536 \
    --ws-ping-interval 20 \
    --ws-ping-timeout 20
