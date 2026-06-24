# shellcheck shell=bash
# System-wide bash defaults for Klangk containers.
# Users can override these in ~/.bashrc on the persistent home mount.
#
# This file is sourced in two contexts:
#   1. Interactive shells (bash.bashrc) — full setup including terminal init
#   2. Non-interactive shells via BASH_ENV — environment + agent config only
#
# The BASH_ENV mechanism lets sandbox setup scripts (which run via `sh -c`
# → `bash -c`) get the same PATH, Pi agent config, and plugin hooks as
# interactive shells, without needing to explicitly call setup-clankers.

# --- Non-interactive-safe section (always runs) --------------------------

# Plugin tools on PATH (ENV in Dockerfile is overridden by login shells).
export PATH="/opt/klangk/bin:$PATH"

# Default editor for git commit, crontab -e, etc.
export EDITOR=nano

# Re-entry guard for the expensive setup below (agent config + plugin hooks).
# BASH_ENV=/etc/bash.bashrc (set in the image) makes EVERY non-interactive bash
# source this file. A heavy process tree -- e.g. an installer at image-build
# time that spawns thousands of bash subshells -- would otherwise re-run
# klangk-setup-clankers + every on-shell-init hook in each one: a fork storm
# that exhausts memory and crashes the VM. The flag is exported so children
# inherit it and skip the section (they already have the resulting env). PATH
# and EDITOR above stay unguarded -- cheap and idempotent.
if [ -z "${KLANGK_BASHRC_DONE:-}" ]; then
  export KLANGK_BASHRC_DONE=1

  # Per-user Pi agent config (extensions, settings, models, skills).
  python3 /opt/klangk/bin/klangk-setup-clankers

  # Run plugin on-shell-init hooks (alphabetical by plugin name).
  # These run as the klangk user on every shell open.
  for f in /opt/klangk/hooks/*/on-shell-init.sh; do
    # shellcheck disable=SC2181
    [ -x "$f" ] && "$f" || true
  done
fi

# --- Interactive-only section --------------------------------------------

# Exit early for non-interactive shells (sandbox setup, cron, scripts).
case $- in
  *i*) ;;
    *) return 2>/dev/null || exit 0 ;;
esac

# Keep herdr's API socket on tmpfs — virtiofs (macOS) rejects chmod on sockets.
# Per-user with random suffix to prevent predictable-path attacks in /tmp.
_herdr_dir=$(mktemp -d "/tmp/herdr-${KLANGK_USER_ID:-default}-XXXXXXXX")
export HERDR_SOCKET_PATH="$_herdr_dir/herdr.sock"

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
