#!/usr/bin/env bash
# Configure Hermes to use klangk's llm-proxy (runs on every shell open).
# Skips if no proxy is configured (standalone mode).

[ -z "$KLANGK_LLM_PROXY_URL" ] && exit 0

mkdir -p ~/.hermes

# Write .env with proxy credentials (token refreshed every shell open).
# Only secrets go here — the model is configured in config.yaml below.
token=$(klangk-workspace-token 2>/dev/null) || exit 0
cat >~/.hermes/.env <<EOF
OPENAI_BASE_URL=${KLANGK_LLM_PROXY_URL}
OPENAI_API_KEY=${token}
EOF

# Write config.yaml each shell so the proxy URL + model stay current (it holds
# no secrets — the token lives in .env). The model MUST be set here as
# model.model and NOT via the HERMES_INFERENCE_MODEL env var: that env var makes
# Hermes auto-detect the provider from the model NAME, which routes a model like
# "MiniMax-M3" to its real provider (Nous) instead of the klangk proxy and fails
# with "API call failed after 3 retries: Connection error". Setting the model in
# config (provider: custom) is the "use my configured endpoint" path that
# actually honours base_url.
cat >~/.hermes/config.yaml <<EOF
model:
  provider: custom
  base_url: "${KLANGK_LLM_PROXY_URL}"
  model: "${KLANGK_LLM_MODEL}"
EOF
