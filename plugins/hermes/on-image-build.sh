#!/bin/sh
# Install Hermes agent (NousResearch) pinned to v2026.6.19 (0.17.0).
# --non-interactive + --skip-setup: no prompts; configured via on-shell-init.
# --skip-browser: no Playwright/Chromium (CLI-only in containers).
set -e

apt-get update -qq && apt-get install -y -qq ffmpeg >/dev/null

echo "downloading hermes"
curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh

echo "installing hermes"
bash /tmp/hermes-install.sh \
  --branch v2026.6.19 \
  --non-interactive \
  --skip-setup \
  --skip-browser \
  --no-skills </dev/null
rm -f /tmp/hermes-install.sh

echo "finished installing hermes"
