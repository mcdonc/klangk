#!/bin/bash
# Configure Hermes to use klangk's llm-proxy (runs on every shell open).
# Skips if no proxy is configured (standalone mode).

[ -z "$KLANGK_LLM_PROXY_URL" ] && exit 0

mkdir -p ~/.hermes

# Write .env with proxy credentials (token refreshed every shell open).
token=$(klangk-workspace-token 2>/dev/null) || exit 0
cat >~/.hermes/.env <<EOF
OPENAI_BASE_URL=${KLANGK_LLM_PROXY_URL}
OPENAI_API_KEY=${token}
HERMES_INFERENCE_MODEL=${KLANGK_LLM_MODEL}
EOF

# Write config.yaml if not present (provider: custom + base_url).
if [ ! -f ~/.hermes/config.yaml ]; then
  cat >~/.hermes/config.yaml <<EOF
model:
  provider: custom
  base_url: "${KLANGK_LLM_PROXY_URL}"
EOF
fi
