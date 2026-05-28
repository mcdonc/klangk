#!/bin/sh
# Minimal container entrypoint.
set -e

chown bark:bark /home/bark /work

# Mark all directories as safe for git (bind mounts may have different ownership)
su -c "git config --global --add safe.directory '*'" bark

# Allow bark user to access the Docker socket (if mounted)
if [ -S /var/run/docker.sock ]; then
  chmod 666 /var/run/docker.sock
fi

# Signal that setup is complete. Terminal sessions (docker exec) source
# /etc/bash.bashrc which waits for this file before showing a prompt.
# /tmp is a tmpfs, so .bark-ready is cleared on every container start.
touch /tmp/.bark-ready

# Keep the container alive. Terminal sessions are started via docker exec.
exec sleep infinity
