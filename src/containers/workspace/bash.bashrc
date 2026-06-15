# shellcheck shell=bash
# System-wide bash defaults for Klangk containers.
# Users can override these in ~/.bashrc on the persistent home mount.

# Ignore Ctrl+C until setup is complete and any default command has started.
trap '' INT

# Wait for the entrypoint to finish setup before showing a prompt.
# /tmp is a tmpfs, so .klangk-ready is cleared on every container start.
while [ ! -f /tmp/.klangk-ready ]; do sleep 0.1; done

# Restore Ctrl+C for interactive shell.
trap - INT

# Change to the user's home directory (podman exec -w can't use symlinks
# without resolving them, so we start in /home and cd here instead).
cd "$HOME" 2>/dev/null

# Per-user Pi agent config.  Shared config lives at /home/.pi/agent/
# (written by setup_clankers at container start).  Each user gets their
# own ~/.pi/agent with symlinks to shared resources and copies of files
# they may customize.
python3 /opt/klangk/bin/setup-user-pi

# Determine which command to exec into (if any).
# KLANGK_CMD_OVERRIDE (set per-session via podman exec -e) takes priority.
# Otherwise fall back to the workspace default from the config mount.
# KLANGK_CMD_STARTED guard prevents infinite recursion if the command is bash.
if [ -z "$KLANGK_CMD_STARTED" ]; then
  KLANGK_CMD="${KLANGK_CMD_OVERRIDE:-}"
  if [ -z "$KLANGK_CMD" ] && [ -f /opt/klangk/config/default-command ]; then
    KLANGK_CMD=$(cat /opt/klangk/config/default-command)
  fi
  if [ -n "$KLANGK_CMD" ]; then
    export KLANGK_CMD_STARTED=1
    stty sane 2>/dev/null
    # shellcheck disable=SC2086
    exec $KLANGK_CMD
  fi
fi
