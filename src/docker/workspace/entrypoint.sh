#!/bin/sh
# Minimal container entrypoint.
set -e

# The host bind-mounts $home_path as /home/klangk and pre-creates the
# work/ subdirectory before the container starts.  Both end up owned by
# the host UID.  chown is not recursive, so we must fix ownership on
# the mount-point AND the work dir individually — otherwise the
# container's klangk user cannot write to /home/klangk/work (breaks
# rsync, file creation, etc.).
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
