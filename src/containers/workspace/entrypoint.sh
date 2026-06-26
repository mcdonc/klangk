#!/bin/sh
# Minimal container entrypoint.
#
# With --userns=keep-id:uid=1000,gid=1000 the host user maps to
# klangk (UID 1000) inside the container.  The entrypoint runs as
# klangk — no root privileges needed.
set -e

# Run plugin on-entrypoint hooks (alphabetical by plugin name).
# These run once per container start, as root (inside userns).
for f in /opt/klangk/plugins/*/on-entrypoint.sh; do
  [ -x "$f" ] || continue
  plugin=$(basename "$(dirname "$f")")
  label=" $plugin (on-entrypoint) "
  pad=$(((60 - ${#label}) / 2))
  line=$(printf '%*s' "$pad" '' | tr ' ' '━')
  printf '\033[33m%s%s%s\033[0m\n' "$line" "$label" "$line"
  "$f" || true
done

# Create the workspace token directory.
mkdir -p /tmp/klangk

# Signal that setup is complete. Terminal sessions (podman exec) source
# /etc/bash.bashrc which waits for this file before showing a prompt.
# Per-user Pi agent config is handled by klangk-setup-clankers (called from
# bash.bashrc on each login).
# /tmp is a tmpfs, so .klangk-ready is cleared on every container start.
touch /tmp/.klangk-ready

# Keep the container alive. Terminal sessions are started via podman exec.
exec sleep infinity
