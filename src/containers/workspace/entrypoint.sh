#!/bin/sh
# Minimal container entrypoint.
set -e

# With --userns=keep-id:uid=0,gid=0 the host user maps to root inside
# the container.  The klangk user (UID 1000) is used for terminal
# sessions (podman exec -u klangk).  Ensure its home exists and is
# owned correctly.
chown klangk:klangk /home/klangk /home/klangk/work

# Set up Pi agent config as the klangk user (extensions, settings, models,
# system prompt, Claude Code skills). Runs before the readiness signal so
# terminal sessions find everything in place.
su -c "python3 /usr/local/bin/setup_clankers" klangk

# Signal that setup is complete. Terminal sessions (podman exec) source
# /etc/bash.bashrc which waits for this file before showing a prompt.
# /tmp is a tmpfs, so .klangk-ready is cleared on every container start.
touch /tmp/.klangk-ready

# Keep the container alive. Terminal sessions are started via podman exec.
exec sleep infinity
