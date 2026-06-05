#!/usr/bin/env bash
# This script simulates the podman run command used by container.py

# Using defaults based on the code for demonstration; replace with real paths if needed.
HOST_PATH="/tmp/klangk-work-test"
HOME_PATH="/tmp/klangk-home-test"
WORKSPACE_ID="test-workspace-123"
IMAGE_NAME="klangk:latest"

# Ensure bind mount directories exist
mkdir -p "$HOST_PATH"
mkdir -p "$HOME_PATH"

podman run -it --rm \
  --name "klangk-test-container" \
  --label "klangk.managed=true" \
  --label "klangk.instance=default" \
  --label "klangk.workspace-id=$WORKSPACE_ID" \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  --tmpfs /run:rw,noexec,nosuid,size=16m \
  --tmpfs /var/log:rw,noexec,nosuid,size=16m \
  --mount type=bind,source="$HOME_PATH",target=/home/klangk \
  --add-host=host.containers.internal:host-gateway \
  -p 9000:8000 -p 9001:8001 -p 9002:8002 -p 9003:8003 -p 9004:8004 \
  -e KLANGK_LLM_PROXY_URL=http://host.containers.internal:8995/llm-proxy \
  -e KLANGK_LLM_MODEL=gemma4:31b \
  -e PI_SKIP_VERSION_CHECK=1 \
  -e KLANGK_PORT_MAPPINGS=8000:9000,8001:9001,8002:9002,8003:9003,8004:9004 \
  -e KLANGK_WORKSPACE_ID="$WORKSPACE_ID" \
  -e KLANGK_HOSTING_HOSTNAME=localhost \
  -e KLANGK_HOSTING_PROTO=http \
  -e KLANGK_HOSTING_BASE_PATH="" \
  -i -a stdout -a stderr \
  "$IMAGE_NAME"
