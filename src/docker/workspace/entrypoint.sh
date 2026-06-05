#!/bin/sh
# Minimal container entrypoint.
set -e

# Match the container's klangk user to the host UID/GID so files
# created inside the container are owned by the same user that runs
# the backend.  This avoids permission mismatches on the bind-mounted
# home directory (the backend needs to delete workspace files on cleanup).
HOST_UID="${KLANGK_HOST_UID:-}"
HOST_GID="${KLANGK_HOST_GID:-}"
CURRENT_UID=$(id -u klangk)
CURRENT_GID=$(id -g klangk)

if [ -n "$HOST_GID" ] && [ "$HOST_GID" != "$CURRENT_GID" ]; then
  groupmod -g "$HOST_GID" klangk 2>/dev/null || true
fi
if [ -n "$HOST_UID" ] && [ "$HOST_UID" != "$CURRENT_UID" ]; then
  usermod -u "$HOST_UID" klangk 2>/dev/null || true
fi

chown klangk:klangk /home/klangk /home/klangk/work

# Set up Pi agent config as the klangk user (extensions, settings, models,
# system prompt, Claude Code skills). Runs before the readiness signal so
# terminal sessions find everything in place.
su -c "python3 /usr/local/bin/setup_clankers" klangk

# Signal that setup is complete. Terminal sessions (docker exec) source
# /etc/bash.bashrc which waits for this file before showing a prompt.
# /tmp is a tmpfs, so .klangk-ready is cleared on every container start.
touch /tmp/.klangk-ready

# Keep the container alive. Terminal sessions are started via docker exec.
exec sleep infinity
