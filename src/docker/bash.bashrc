# shellcheck shell=bash
# System-wide bash defaults for Bark containers.
# Users can override these in ~/.bashrc on the persistent home mount.

# Ignore Ctrl+C until setup is complete and any default command has started.
trap '' INT

# Source Nix profile if installed (adds nix, devenv to PATH)
if [ -f /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh ]; then
  . /nix/var/nix/profiles/default/etc/profile.d/nix-daemon.sh
fi

# Wait for the entrypoint to finish setup before showing a prompt.
# /tmp is a tmpfs, so .bark-ready is cleared on every container start.
while [ ! -f /tmp/.bark-ready ]; do sleep 0.1; done

# Restore Ctrl+C for interactive shell.
trap - INT

PS1='\[\033[01;34m\]\w\[\033[00m\]\$ '
HISTFILE=~/.bash_history
HISTSIZE=1000
HISTFILESIZE=2000
shopt -s histappend
PROMPT_COMMAND="history -a"
alias ls='ls --color=auto'
alias grep='grep --color=auto'

# Determine which command to exec into (if any).
# BARK_CMD_OVERRIDE (set per-session via docker exec -e) takes priority.
# Otherwise fall back to the workspace default from the config mount.
# BARK_CMD_STARTED guard prevents infinite recursion if the command is bash.
if [ -z "$BARK_CMD_STARTED" ]; then
  BARK_CMD="${BARK_CMD_OVERRIDE:-}"
  if [ -z "$BARK_CMD" ] && [ -f /opt/bark/config/default-command ]; then
    BARK_CMD=$(cat /opt/bark/config/default-command)
  fi
  if [ -n "$BARK_CMD" ]; then
    export BARK_CMD_STARTED=1
    stty sane 2>/dev/null
    # shellcheck disable=SC2086
    exec $BARK_CMD
  fi
fi
