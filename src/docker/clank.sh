#!/bin/sh
# Launch Pi with session resume. Intended as a workspace default command.
# Sets up Pi agent config, then finds the most recent session file to resume.
set -e

git config --global --add safe.directory /work 2>/dev/null

PI_AGENT_DIR="/home/bark/.pi/agent"
export PI_CODING_AGENT_DIR="$PI_AGENT_DIR"
SESSION_DIR="/home/bark/.pi/sessions"

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
      "baseUrl": "$BARK_LLM_PROXY_URL",
      "api": "openai-completions",
      "apiKey": "proxy",
      "models": [
        { "id": "$BARK_LLM_MODEL" }
      ]
    }
  }
}
EOF

# Merge runtime LLM config into build-time settings (which has "packages"
# from pi install). The npm dir is symlinked above so Pi finds the packages
# without reinstalling.
jq --arg model "$BARK_LLM_MODEL" '. + {defaultProvider: "llm-proxy", defaultModel: $model}' \
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

# Build Pi command line
PI_ARGS="--no-context-files --session-dir $SESSION_DIR"
PI_ARGS="$PI_ARGS --append-system-prompt $SYSTEM_PROMPT_FILE"

# Find the most recent session file to resume
LATEST=$(find "$SESSION_DIR" -name '*.jsonl' 2>/dev/null | sort | tail -1)
if [ -n "$LATEST" ]; then
  PI_ARGS="$PI_ARGS --session $LATEST"
fi

# shellcheck disable=SC2086
exec pi $PI_ARGS
