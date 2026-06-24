#!/bin/bash
# Install Hermes agent (NousResearch) pinned to v2026.6.19 (0.17.0).
# --skip-setup skips the interactive API key wizard; configured via on-shell-init.
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --branch v2026.6.19 --skip-setup
