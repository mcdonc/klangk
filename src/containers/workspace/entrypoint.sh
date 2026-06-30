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
  line=$(printf '%*s' "$pad" '' | tr ' ' '-')
  printf '\033[33m%s%s%s\033[0m\n' "$line" "$label" "$line"
  "$f" || true
  printf '\033[33m%s\033[0m\n' "$(printf '%*s' 60 '' | tr ' ' '-')"
done

# Create the workspace token directory.
mkdir -p /tmp/klangk

# Signal that the entrypoint's one-time setup is done. The backend polls
# this sentinel (podman.wait_for_container_ready) before reporting the
# container as ready, so every consumer — terminals, exec, agent, health
# check — gets the guarantee regardless of shell. /tmp is a tmpfs, so the
# sentinel is cleared on every container start and recreated here.
touch /tmp/.klangk-ready

# Keep the container alive. Terminal sessions are started via podman exec.
exec sleep infinity
