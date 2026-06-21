#!/bin/sh
# Minimal container entrypoint.
#
# With --userns=keep-id:uid=1000,gid=1000 the host user maps to
# klangk (UID 1000) inside the container.  The entrypoint runs as
# klangk — no root privileges needed.
set -e

# Run plugin on-entrypoint hooks (alphabetical by plugin name).
# These run once per container start, as root (inside userns).
for f in /opt/klangk/hooks/*/on-entrypoint.sh; do
  [ -x "$f" ] && "$f" || true
done

# Create the workspace token directory. /run is a tmpfs owned by root,
# so this must happen at entrypoint time (before dropping privileges).
mkdir -p /run/klangk
chown klangk:klangk /run/klangk 2>/dev/null || true

# Signal that setup is complete. Terminal sessions (podman exec) source
# /etc/bash.bashrc which waits for this file before showing a prompt.
# Per-user Pi agent config is handled by setup-clankers (called from
# bash.bashrc on each login).
# /tmp is a tmpfs, so .klangk-ready is cleared on every container start.
touch /tmp/.klangk-ready

# Keep the container alive. Terminal sessions are started via podman exec.
exec sleep infinity
