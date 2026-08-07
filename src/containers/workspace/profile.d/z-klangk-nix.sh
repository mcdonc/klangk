# shellcheck shell=sh
# shellcheck disable=SC1091  # intentionally source nix's own activation
# klangk nix feature activation (#2202).
#
# Sourced by login shells (via /etc/profile -> run-parts /etc/profile.d). When
# klangkd mounts the feature's /nix into a workspace it sets KLANGKWS_NIX=1;
# this activates nix (PATH + SSL certs) so `nix`/`devenv` work by default, in
# ANY image — no manual sourcing and no nix image required.
#
# Both guards must hold, so a non-nix workspace (or an unrelated /nix) is left
# untouched:
#   - KLANGKWS_NIX=1  : klangkd's signal that the feature mounted /nix, AND
#   - /nix/nix-profile/etc/profile.d/nix.sh exists : /nix really is the mount.
#
# nix's own activation (nix.sh) targets ~/.nix-profile. The feature mount lives
# at /nix/nix-profile; the workspace's /home is overlaid (so the seed's
# ~/.nix-profile isn't present), so we point ~/.nix-profile at the mount first.
if [ "${KLANGKWS_NIX:-}" = "1" ] &&
  [ -f /nix/nix-profile/etc/profile.d/nix.sh ]; then
  export NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
  # nix.sh only activates when both HOME and USER are set.
  export USER="${USER:-klangk}"
  if [ -n "$HOME" ] && [ ! -e "$HOME/.nix-profile" ]; then
    ln -s /nix/nix-profile "$HOME/.nix-profile"
  fi
  . /nix/nix-profile/etc/profile.d/nix.sh
fi
