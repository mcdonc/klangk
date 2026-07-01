# shellcheck shell=bash
# System-wide bash defaults for Klangk containers.
# Users can override these in ~/.bashrc on the persistent home mount.
#
# NOTE: environment exports (PATH=/opt/klangk/bin, EDITOR) live in
# /etc/profile.d/klangk-*.sh so that non-interactive login shells (bash -lc
# — e.g. `klangkc exec`) see them too. This file is only sourced
# for interactive shells, so anything placed here is invisible to one-shot
# non-interactive commands. See issue #1093. (The workspace health check is
# the exception: it runs a non-login `bash -c` and sources nothing — see
# docs/features/health-check.md.)

# Change to the user's home directory (podman exec -w can't use symlinks
# without resolving them, so we start in /home and cd here instead).
cd "$HOME" 2>/dev/null

# Per-user Pi agent config (extensions, settings, models, skills).
python3 /opt/klangk/bin/klangk-setup-pi

# Display terminal banner if configured (deployers override via
# KLANGK_TERMINAL_BANNER env var; empty string disables it).
if [ -n "${KLANGK_TERMINAL_BANNER:-}" ]; then
  printf '\033[33m%s\033[0m\n' "$KLANGK_TERMINAL_BANNER"
fi

# Run plugin on-shell-init hooks (alphabetical by plugin name).
# These run as the klangk user on every shell open.
for f in /opt/klangk/plugins/*/on-shell-init.sh; do
  [ -x "$f" ] || continue
  plugin=$(basename "$(dirname "$f")")
  label=" $plugin (on-shell-init) "
  pad=$(( (60 - ${#label}) / 2 ))
  line=$(printf '%*s' "$pad" '' | tr ' ' '-')
  printf '\033[33m%s%s%s\033[0m\n' "$line" "$label" "$line"
  "$f" || true
  printf '\033[33m%s\033[0m\n' "$(printf '%*s' 60 '' | tr ' ' '-')"
done
