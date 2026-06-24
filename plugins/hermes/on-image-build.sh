#!/usr/bin/env bash
# Install Hermes agent (NousResearch) pinned to v2026.6.19 (0.17.0).
# --non-interactive + --skip-setup: no prompts; configured via on-shell-init.
# --skip-browser: no Playwright/Chromium (CLI-only in containers).
set -e

echo "downloading hermes"
curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh

echo "installing hermes"
bash /tmp/hermes-install.sh \
  --branch v2026.6.19 \
  --non-interactive \
  --skip-setup \
  --skip-browser
rm -f /tmp/hermes-install.sh
