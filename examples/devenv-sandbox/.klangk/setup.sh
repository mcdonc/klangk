#!/bin/bash
set -euo pipefail

# Install nix (single-user) and devenv.
# Requires KLANGK_ALLOW_SUDO=true on the server.
#
# The /nix volume persists across workspace recreations, so this
# script is fast on subsequent runs (nix is already installed).
#
# We use single-user nix (--no-daemon) because workspace containers
# don't run systemd/init to manage the nix daemon.

if ! command -v nix &>/dev/null; then
  echo "Installing nix (single-user)..."
  sudo chown -R "$(whoami)" /nix 2>/dev/null || true
  curl -L https://nixos.org/nix/install | sh -s -- --no-daemon
fi

# Add nix to PATH for the rest of this script.
export PATH="$HOME/.nix-profile/bin:$PATH"

# Enable flakes and the nix command permanently.
mkdir -p ~/.config/nix
if ! grep -q experimental-features ~/.config/nix/nix.conf 2>/dev/null; then
  echo "experimental-features = nix-command flakes" >>~/.config/nix/nix.conf
fi

# Ensure non-login shells (plain `bash`) also get nix on PATH.
# The nix installer only modifies ~/.profile (login shells).
# shellcheck disable=SC2016
if ! grep -q nix-profile ~/.bashrc 2>/dev/null; then
  echo '. "$HOME/.nix-profile/etc/profile.d/nix.sh"' >>~/.bashrc
fi

if ! command -v devenv &>/dev/null; then
  echo "Installing devenv..."
  nix profile install \
    --extra-experimental-features "nix-command flakes" \
    --accept-flake-config \
    "github:cachix/devenv/v2.1.2"
else
  echo "devenv already installed, skipping."
fi

echo "nix: $(nix --version)"
echo "devenv: $(devenv version)"
echo "Setup complete."
