# shellcheck shell=bash
# System-wide bash defaults for Klangk containers.
# Users can override these in ~/.bashrc on the persistent home mount.
#
# NOTE: environment exports (PATH=/opt/klangk/bin, EDITOR) live in
# /etc/profile.d/klangk-*.sh so that non-interactive login shells (bash -lc
# — the health check, klangkc exec) see them too. This file is only sourced
# for interactive shells, so anything placed here is invisible to one-shot
# non-interactive commands. See issue #1093.

# Nothing below here is meaningful during image build. The Dockerfile touches
# /tmp/.klangk-image-build before running plugin hooks and removes it after;
# any `bash -i` spawned by those hooks (e.g. hermes installer probing PATH)
# exits early here.
[ -f /tmp/.klangk-image-build ] && return 0

# Keep herdr's API socket on tmpfs — virtiofs (macOS) rejects chmod on sockets.
# Per-user with random suffix to prevent predictable-path attacks in /tmp.
_herdr_dir=$(mktemp -d "/tmp/herdr-${KLANGK_USER_ID:-default}-XXXXXXXX")
export HERDR_SOCKET_PATH="$_herdr_dir/herdr.sock"

# Block interactive shells until the entrypoint signals that setup is done.
# /tmp is a tmpfs, so .klangk-ready starts absent on every container boot
# and is created by the entrypoint when setup finishes.
trap '' INT
while [ ! -f /tmp/.klangk-ready ]; do sleep 0.1; done
trap - INT

# Change to the user's home directory (podman exec -w can't use symlinks
# without resolving them, so we start in /home and cd here instead).
cd "$HOME" 2>/dev/null

# Per-user Pi agent config (extensions, settings, models, skills).
python3 /opt/klangk/bin/klangk-setup-clankers

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
