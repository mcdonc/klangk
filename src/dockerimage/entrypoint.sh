#!/bin/sh
# bark user is created at build time with the host UID/GID.
# The script re-execs itself as bark after the root-only chown.

# --- Root phase: fix bind-mount ownership, then drop to bark ---
if [ "$(id -u)" = "0" ]; then
  chown bark:bark /home/bark /work
  # gosu execs directly (unlike su, which stays resident in the process list)
  exec gosu bark "$0" "$@"
fi

# --- bark phase: all setup runs as bark, no root-owned files ---
set -e

PI_AGENT_DIR="/home/bark/.pi/agent"

# Set up Pi agent config (from build-time /opt/bark).
# /home/bark is a persistent bind mount, so clean the agent dir first to
# avoid stale files from previous container starts.
rm -rf "$PI_AGENT_DIR"
mkdir -p "$PI_AGENT_DIR/bin"
# Symlink extensions and npm packages from the read-only image.
ln -sf /opt/bark/pi-agent/extensions "$PI_AGENT_DIR/extensions"
ln -sf /opt/bark/pi-agent/npm "$PI_AGENT_DIR/npm"

# Symlink system fd/rg into Pi's bin dir so it doesn't re-download them
ln -sf /usr/bin/fd "$PI_AGENT_DIR/bin/fd"
ln -sf /usr/bin/rg "$PI_AGENT_DIR/bin/rg"

# Write models.json — no secrets here since the LLM proxy injects the
# API key on the host side. The container only sees the proxy URL.
cat >"$PI_AGENT_DIR/models.json" <<EOF
{
  "providers": {
    "llm-proxy": {
      "baseUrl": "$LLM_PROXY_URL",
      "api": "openai-completions",
      "apiKey": "proxy",
      "models": [
        { "id": "$LLM_MODEL" }
      ]
    }
  }
}
EOF

# Merge runtime LLM config into build-time settings (which has "packages"
# from pi install). The npm dir is symlinked above so Pi finds the packages
# without reinstalling.
jq --arg model "$LLM_MODEL" '. + {defaultProvider: "llm-proxy", defaultModel: $model}' \
  /opt/bark/pi-agent/settings.json >"$PI_AGENT_DIR/settings.json"

# Build system prompt file from static template + registered extension tools
SYSTEM_PROMPT_FILE="$PI_AGENT_DIR/system-prompt.md"
cp /opt/bark/system-prompt.md "$SYSTEM_PROMPT_FILE"

if [ -d "$PI_AGENT_DIR/extensions" ] && [ "$(ls "$PI_AGENT_DIR/extensions"/*.ts 2>/dev/null)" ]; then
  echo "" >>"$SYSTEM_PROMPT_FILE"
  echo "Registered extension tools (use these instead of bash when appropriate):" >>"$SYSTEM_PROMPT_FILE"
  for ext in "$PI_AGENT_DIR/extensions"/*.ts; do
    name=$(grep -E '^\s+name: "' "$ext" | head -1 | sed 's/.*name: "//;s/".*//')
    desc=$(grep -E '^\s+description: "' "$ext" | head -1 | sed 's/.*description: "//;s/".*//')
    if [ -n "$name" ] && [ -n "$desc" ]; then
      echo "- \`$name\`: $desc" >>"$SYSTEM_PROMPT_FILE"
    fi
  done
fi

git config --global --add safe.directory /work 2>/dev/null

# Signal that setup is complete. Terminal sessions (docker exec) source
# /etc/bash.bashrc which waits for this file before showing a prompt,
# preventing races where the user runs pi before config is ready.
# /tmp is a tmpfs, so .bark-ready is cleared on every container start.
touch /tmp/.bark-ready

# Build Pi command line
PI_CMD="pi --mode rpc --no-context-files --append-system-prompt $SYSTEM_PROMPT_FILE --session-dir /home/bark/.pi/sessions"
if [ -n "$BARK_RESUME_SESSION" ]; then
  PI_CMD="$PI_CMD --session $BARK_RESUME_SESSION"
fi

export PI_CODING_AGENT_DIR="$PI_AGENT_DIR"
# shellcheck disable=SC2086
exec env -u BARK_RESUME_SESSION $PI_CMD
