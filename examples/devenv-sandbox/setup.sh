#!/bin/bash
set -euo pipefail

# Install nix (single-user) and devenv.
# No sudo needed — single-user nix installs everything under the
# current user's profile and /nix/store.
#
# The /nix volume persists across workspace recreations, so this
# script is fast on subsequent runs (nix is already installed).
#
# We use single-user nix (--no-daemon) because workspace containers
# don't run systemd/init to manage the nix daemon.

# Check if nix is installed AND working (not just the binary existing).
# A broken profile from an interrupted install can leave the binary
# present but nix non-functional.
if ! nix --version &>/dev/null; then
  echo "Installing nix (single-user)..."
  # Clean up any broken profile state from a previous attempt.
  rm -rf "$HOME/.local/state/nix" "$HOME/.nix-profile" "$HOME/.nix-defexpr" "$HOME/.nix-channels"
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
