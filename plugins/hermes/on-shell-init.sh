#!/bin/bash
# Configure Hermes to use klangk's llm-proxy (runs on every shell open).
# Skips unless KLANGK_HERMES_USE_LLM_PROXY is truthy (set via plugin config).
# Idempotent: only touches the two keys we manage; preserves all other .env values.

case "$KLANGK_HERMES_USE_LLM_PROXY" in true | 1) ;; *) exit 0 ;; esac
[ -n "$KLANGK_LLM_PROXY_URL" ] || exit 0

token=$(klangk-workspace-token 2>/dev/null) || exit 0

mkdir -p ~/.hermes

# Update OPENAI_BASE_URL and OPENAI_API_KEY in .env without clobbering other
# values. The token is refreshed every shell open; other keys (OPENROUTER,
# GOOGLE, etc.) are left untouched.
# Do NOT set HERMES_INFERENCE_MODEL — it triggers provider auto-detection
# which bypasses the custom endpoint. Model goes in config.yaml instead.
env_file=~/.hermes/.env
touch "$env_file"
# Remove existing lines for keys we manage, then append fresh values.
sed -i '/^OPENAI_BASE_URL=/d;/^OPENAI_API_KEY=/d' "$env_file"
cat >>"$env_file" <<EOF
OPENAI_BASE_URL=${KLANGK_LLM_PROXY_URL}
OPENAI_API_KEY=${token}
EOF

# Write config.yaml once — provider and model don't change between shells.
if [ ! -f ~/.hermes/config.yaml ]; then
  cat >~/.hermes/config.yaml <<EOF
model:
  provider: custom
  base_url: "${KLANGK_LLM_PROXY_URL}"
  model: "${KLANGK_LLM_MODEL}"
EOF
fi
