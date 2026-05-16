#!/bin/sh
# bark user is created at build time with the host UID/GID.
# This entrypoint runs as root, sets up Pi config, then drops to bark.

# Don't hardcode any secrets here, its copied to the container fs.

# Set up Pi agent config in bark's home (copied from build-time /opt/bark)
PI_AGENT_DIR="/home/bark/.pi/agent"
mkdir -p "$PI_AGENT_DIR/extensions"
cp -r /opt/bark/pi-agent/extensions/* "$PI_AGENT_DIR/extensions/" 2>/dev/null

# models.json contains the API key, so we use a FIFO to prevent the bark user
# from reading it after Pi has loaded. Pi calls readFileSync on models.json at
# startup, which blocks on the FIFO until we write to it. After Pi reads it,
# the FIFO remains but is empty — cat on it blocks forever (no writer).
# Environment variables ({OLLAMA|ANTHROPIC|OPENAI|GOOGLE|GROQ|MISTRAL}_*, etc.)
# are passed by container_manager.py from the host .env.
MODELS_JSON="$PI_AGENT_DIR/models.json"
mkfifo "$MODELS_JSON"
chown bark:bark "$MODELS_JSON"
MODELS_CONTENT=$(cat << EOF
{
  "providers": {
    "ollama": {
      "baseUrl": "$OLLAMA_BASE_URL",
      "api": "openai-completions",
      "apiKey": "$OLLAMA_API_KEY",
      "models": [
        { "id": "$OLLAMA_MODEL" }
      ]
    }
  }
}
EOF
)

# settings.json not needed — we pass --provider and --model on the command line

# Fix ownership: bark's home + workspace directory
# /home/bark/.pi/sessions is bind-mounted from the host by container_manager
chown -R bark:bark /home/bark
chown bark:bark /workspace

# Allow bark to use git in /workspace
su bark -c "git config --global --add safe.directory /workspace" 2>/dev/null

# Build system prompt file from static template + registered extension tools
SYSTEM_PROMPT_FILE="$PI_AGENT_DIR/system-prompt.md"
cp /opt/bark/system-prompt.md "$SYSTEM_PROMPT_FILE"

if [ -d "$PI_AGENT_DIR/extensions" ] && [ "$(ls "$PI_AGENT_DIR/extensions"/*.ts 2>/dev/null)" ]; then
  echo "" >> "$SYSTEM_PROMPT_FILE"
  echo "Registered extension tools (use these instead of bash when appropriate):" >> "$SYSTEM_PROMPT_FILE"
  for ext in "$PI_AGENT_DIR/extensions"/*.ts; do
    name=$(grep -E '^\s+name: "' "$ext" | head -1 | sed 's/.*name: "//;s/".*//')
    desc=$(grep -E '^\s+description: "' "$ext" | head -1 | sed 's/.*description: "//;s/".*//')
    if [ -n "$name" ] && [ -n "$desc" ]; then
      echo "- \`$name\`: $desc" >> "$SYSTEM_PROMPT_FILE"
    fi
  done
fi

# Feed models.json to Pi via the FIFO in the background, then remove it.
# Pi's readFileSync blocks until this write completes.
(echo "$MODELS_CONTENT" > "$MODELS_JSON" && rm -f "$MODELS_JSON") &

# Drop to bark user and run Pi, stripping API keys from the environment.
# The keys were already captured in MODELS_CONTENT above.
# --no-context-files: don't look for AGENTS.md in workspace
# --append-system-prompt: inject instructions via system prompt instead
# Build a list of env vars to strip (all provider-related vars)
STRIP_VARS=""
for var in $(env | grep -oE '^(OLLAMA|ANTHROPIC|OPENAI|GOOGLE|GROQ|MISTRAL)_[^=]+'); do
  STRIP_VARS="$STRIP_VARS -u $var" # unsets
done

exec env $STRIP_VARS \
  su bark -c "PI_CODING_AGENT_DIR=$PI_AGENT_DIR exec pi --mode rpc --provider ollama --model $OLLAMA_MODEL --no-context-files --append-system-prompt $SYSTEM_PROMPT_FILE --session-dir /home/bark/.pi/sessions"
