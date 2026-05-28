#!/bin/sh
# Minimal container entrypoint. Pi setup is handled by bark-pi (the
# default command script), not here.
set -e

chown bark:bark /home/bark /work

# Signal that setup is complete. Terminal sessions (docker exec) source
# /etc/bash.bashrc which waits for this file before showing a prompt.
# /tmp is a tmpfs, so .bark-ready is cleared on every container start.
touch /tmp/.bark-ready

# Keep the container alive. Terminal sessions are started via docker exec.
exec sleep infinity
