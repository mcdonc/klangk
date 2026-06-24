#!/bin/bash
# Configure Hermes to use klangk's llm-proxy (runs on every shell open).
# Skips if no proxy is configured (standalone mode).

[ -z "$KLANGK_LLM_PROXY_URL" ] && exit 0

mkdir -p ~/.hermes

# Write .env with proxy credentials only (token refreshed every shell open).
# Do NOT set HERMES_INFERENCE_MODEL — it triggers provider auto-detection
# which bypasses the custom endpoint. Model goes in config.yaml instead.
token=$(klangk-workspace-token 2>/dev/null) || exit 0
cat >~/.hermes/.env <<EOF
OPENAI_BASE_URL=${KLANGK_LLM_PROXY_URL}
OPENAI_API_KEY=${token}
EOF

# Write config.yaml with provider + model (always overwrite — token/model may change).
cat >~/.hermes/config.yaml <<EOF
model:
  provider: custom
  base_url: "${KLANGK_LLM_PROXY_URL}"
  model: "${KLANGK_LLM_MODEL}"
EOF
