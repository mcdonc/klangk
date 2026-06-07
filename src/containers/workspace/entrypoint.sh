#!/bin/sh
# Minimal container entrypoint.
set -e

chown klangk:klangk /home/klangk /home/klangk/work 2>/dev/null || true

# TODO: re-enable setup_clankers once entrypoint permissions are sorted
# su -c "python3 /usr/local/bin/setup_clankers" klangk

# Signal that setup is complete. Terminal sessions (podman exec) source
# /etc/bash.bashrc which waits for this file before showing a prompt.
# /tmp is a tmpfs, so .klangk-ready is cleared on every container start.
touch /tmp/.klangk-ready

# Keep the container alive. Terminal sessions are started via podman exec.
exec sleep infinity
