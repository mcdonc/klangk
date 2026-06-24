#!/bin/bash
# OpenClaw SecretRef exec provider that resolves secrets via
# klangk-workspace-token.  OpenClaw sends a JSON request on stdin
# and expects a JSON response on stdout.
#
# Request:  {"protocolVersion":1,"provider":"klangk","ids":["workspace-token"]}
# Response: {"protocolVersion":1,"values":{"workspace-token":"<jwt>"}}
#
# Install to ~/.local/bin/klangk-secret-provider (copied by setup.sh).

set -euo pipefail

# Consume the JSON request from stdin (required by the protocol).
cat >/dev/null

# Extract the ids array (simple jq-free parsing for the common case).
# We only support "workspace-token" as an id.
token=$(/opt/klangk/bin/klangk-workspace-token 2>/dev/null) || token=""

if [ -n "$token" ]; then
  printf '{"protocolVersion":1,"values":{"workspace-token":"%s"}}' "$token"
else
  printf '{"protocolVersion":1,"values":{},"errors":{"workspace-token":{"message":"klangk-workspace-token failed"}}}'
fi
