#!/bin/sh
# Build models.json from environment variables
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-https://ollama.com/v1}"
OLLAMA_MODEL="${OLLAMA_MODEL:-gemma4:31b}"
OLLAMA_API_KEY="${OLLAMA_API_KEY:-ollama}"

cat > /root/.pi/agent/models.json << EOF
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

cat > /root/.pi/agent/settings.json << EOF
{
  "defaultProvider": "ollama",
  "defaultModel": "$OLLAMA_MODEL"
}
EOF

mkdir -p /workspace/.pi/sessions
# Copy default AGENTS.md if not already present
if [ ! -f /workspace/AGENTS.md ]; then
  cp /default-agents.md /workspace/AGENTS.md
fi
exec pi --mode rpc --session-dir /workspace/.pi/sessions
