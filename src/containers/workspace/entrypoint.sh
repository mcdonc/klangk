#!/bin/sh
# Minimal container entrypoint.
#
# With --userns=keep-id:uid=1000,gid=1000 the host user maps to
# klangk (UID 1000) inside the container.  The entrypoint runs as
# klangk — no root privileges needed.
set -e

# Build the Pi agent skel at /opt/klangk/pi-skel/ (extensions, settings,
# models, system prompt, Claude Code skills).  Each user gets a copy
# via setup-user-pi (called from bash.bashrc on first shell).
python3 /opt/klangk/bin/setup-clankers

# Signal that setup is complete. Terminal sessions (podman exec) source
# /etc/bash.bashrc which waits for this file before showing a prompt.
# /tmp is a tmpfs, so .klangk-ready is cleared on every container start.
touch /tmp/.klangk-ready

# Keep the container alive. Terminal sessions are started via podman exec.
exec sleep infinity
