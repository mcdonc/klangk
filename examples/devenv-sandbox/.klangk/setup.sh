#!/bin/bash
set -euo pipefail

# Install nix and devenv via devenv-bootstrap.
# Requires KLANGK_ALLOW_SUDO=true on the server.
#
# The /nix volume persists across workspace recreations, so this
# script is fast on subsequent runs (nix is already installed).

if ! command -v nix &>/dev/null; then
  echo "Installing nix, devenv, and cachix..."
  curl -fsSL https://raw.githubusercontent.com/mcdonc/devenv-bootstrap/main/bootstrap.py | python3 - --unattended
else
  echo "nix already installed, skipping."
fi

# Source nix so devenv is on PATH.
# shellcheck source=/dev/null
. /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh

echo "nix: $(nix --version)"
echo "devenv: $(devenv version)"
echo "Setup complete."
