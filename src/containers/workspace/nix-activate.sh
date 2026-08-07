#!/bin/sh
# /opt/klangk/bin/nix-activate.sh — source to put nix + nix-installed
# programs on PATH.
#
# Usage:
#   . /opt/klangk/bin/nix-activate.sh
#
# nix/devenv are NOT on PATH by default (#2199). Sourcing this adds:
#   - the shared base profile under /nix (nix, devenv, cachix) — it lives under
#     /nix so it survives klangk's runtime /home overlay (the workspace mounts
#     its own /home over the image's, hiding anything baked under /home);
#   - the caller's per-user install profile (programs added via
#     `nix profile install`), once it exists.
# Re-sourcing is idempotent (no duplicate PATH entries). Works for any user —
# $HOME is klangk's per-user subdir (/home/.users/<id>, or /home/<handle> for
# the agent), and the script recreates ~/.nix-profile there on first use.

# nix needs an explicit CA bundle; it does not read the Debian trust store.
export NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Recreate the conventional ~/.nix-profile symlink on first use. The image's
# symlink lives under /home, which klangk overlays at runtime; point it at the
# shared base profile under /nix instead. nix follows ~/.nix-profile, so
# programs added via `nix profile install` land here too (and are thus on
# PATH via the entry below).
if [ ! -e "$HOME/.nix-profile" ]; then
  ln -s /nix/nix-profile "$HOME/.nix-profile"
fi

# Prepend the base profile bin (nix, devenv, cachix, and anything later
# added via `nix profile install`) to PATH, ahead of every other bindir,
# unless already present.
case ":${PATH:-}:" in
*":/nix/nix-profile/bin:"*) ;;
*) PATH="/nix/nix-profile/bin${PATH:+:$PATH}" ;;
esac

export PATH
