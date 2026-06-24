#!/usr/bin/env bash
# Install Hermes agent (NousResearch) pinned to v2026.6.19 (0.17.0).
# --non-interactive + --skip-setup: no prompts; configured via on-shell-init.
# --skip-browser: no Playwright/Chromium (CLI-only in containers).
#
# Runs as root during `podman build`, so the installer uses its FHS layout
# (/usr/local/bin/hermes, /usr/local/lib/hermes-agent) — on the system PATH and
# surviving klangk's runtime /home bind-mount, the same way the claude-code
# plugin's `npm -g` does.
set -e

# The installer apt-installs ffmpeg + build tools. The base image ships with
# empty apt lists, so refresh them first — otherwise the installer aborts with
# "Unable to locate package ffmpeg".
apt-get update -qq
apt-get install -y -qq ffmpeg build-essential python3-dev libffi-dev

echo "downloading hermes"
curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh

echo "installing hermes"
bash /tmp/hermes-install.sh \
  --branch v2026.6.19 \
  --non-interactive \
  --skip-setup \
  --skip-browser
rm -f /tmp/hermes-install.sh
