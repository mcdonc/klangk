#!/bin/bash
set -euo pipefail

echo "sandbox setup: running in $(pwd)"
echo "sandbox setup: HOME=$HOME"
echo "sandbox setup: whoami=$(whoami)"

# Source secrets if available
[ -f ~/.env ] && echo "sandbox setup: .env found" || echo "sandbox setup: no .env"

# Verify mounts
[ -d ~/.ssh ] && echo "sandbox setup: ~/.ssh mounted" || echo "sandbox setup: no ~/.ssh"
[ -f ~/.gitconfig ] && echo "sandbox setup: ~/.gitconfig copied" || echo "sandbox setup: no ~/.gitconfig"

echo "sandbox setup: done"
