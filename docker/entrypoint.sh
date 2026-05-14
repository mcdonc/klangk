#!/bin/sh
# Replace placeholder API key with actual env var
if [ -n "$OLLAMA_API_KEY" ]; then
  sed -i "s/\"placeholder\"/\"$OLLAMA_API_KEY\"/" /root/.pi/agent/models.json
fi
mkdir -p /workspace/.pi/sessions
# Copy default AGENTS.md if not already present
if [ ! -f /workspace/AGENTS.md ]; then
  cp /default-agents.md /workspace/AGENTS.md
fi
exec pi --mode rpc --session-dir /workspace/.pi/sessions
