#!/bin/sh
# Launch Pi with session resume. Intended as a workspace default command.
# Sets up Pi agent config, then finds the most recent session file to resume.
set -e

git config --global --add safe.directory /home/klangk/work 2>/dev/null

PI_AGENT_DIR="/home/klangk/.pi/agent"
export PI_CODING_AGENT_DIR="$PI_AGENT_DIR"
SESSION_DIR="/home/klangk/.pi/sessions"

# Set up Pi agent config (from build-time /opt/klangk).
# /home/klangk is a persistent bind mount, so clean the agent dir first to
# avoid stale files from previous container starts.
rm -rf "$PI_AGENT_DIR"
mkdir -p "$PI_AGENT_DIR/bin"
# Symlink extensions and npm packages from the read-only image.
ln -sf /opt/klangk/pi-agent/extensions "$PI_AGENT_DIR/extensions"
ln -sf /opt/klangk/pi-agent/npm "$PI_AGENT_DIR/npm"

# Symlink system fd/rg into Pi's bin dir so it doesn't re-download them
ln -sf /usr/bin/fd "$PI_AGENT_DIR/bin/fd"
ln -sf /usr/bin/rg "$PI_AGENT_DIR/bin/rg"

# Write models.json — no secrets here since the LLM proxy injects the
# API key on the host side. The container only sees the proxy URL.
cat >"$PI_AGENT_DIR/models.json" <<EOF
{
  "providers": {
    "llm-proxy": {
      "baseUrl": "$KLANGK_LLM_PROXY_URL",
      "api": "openai-completions",
      "apiKey": "proxy",
      "models": [
        { "id": "$KLANGK_LLM_MODEL" }
      ]
    }
  }
}
EOF

# Merge runtime LLM config into build-time settings (which has "packages"
# from pi install). The npm dir is symlinked above so Pi finds the packages
# without reinstalling.
jq --arg model "$KLANGK_LLM_MODEL" '. + {defaultProvider: "llm-proxy", defaultModel: $model}' \
  /opt/klangk/pi-agent/settings.json >"$PI_AGENT_DIR/settings.json"

# Build system prompt file from static template + registered extension tools
SYSTEM_PROMPT_FILE="$PI_AGENT_DIR/system-prompt.md"
cp /opt/klangk/system-prompt.md "$SYSTEM_PROMPT_FILE"

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

# Add enabled skills via Pi's native --skill flag.
# KLANGK_SKILLS is a comma-separated list of skill directory names.
# Skills are expected at /opt/klangk/skills/<name>/ (user-mounted).
SKILLS_DIR="/opt/klangk/skills"
if [ -n "$KLANGK_SKILLS" ] && [ -d "$SKILLS_DIR" ]; then
  OLD_IFS="$IFS"
  IFS=','
  for skill_name in $KLANGK_SKILLS; do
    skill_name=$(echo "$skill_name" | tr -d ' ')
    [ -z "$skill_name" ] && continue
    if [ -d "$SKILLS_DIR/$skill_name" ]; then
      PI_ARGS="$PI_ARGS --skill $SKILLS_DIR/$skill_name"
    fi
  done
  IFS="$OLD_IFS"
fi

# Find the most recent session file to resume
LATEST=$(find "$SESSION_DIR" -name '*.jsonl' 2>/dev/null | sort | tail -1)
if [ -n "$LATEST" ]; then
  PI_ARGS="$PI_ARGS --session $LATEST"
fi

# shellcheck disable=SC2086
exec pi $PI_ARGS
