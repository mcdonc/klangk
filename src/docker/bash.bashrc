# shellcheck shell=bash
# System-wide bash defaults for Bark containers.
# Users can override these in ~/.bashrc on the persistent home mount.

# Ignore Ctrl+C until setup is complete and any default command has started.
trap '' INT

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

# If a default command is configured (read-only config mount), exec into it.
# BARK_CMD_STARTED guard prevents infinite recursion if the command is bash.
if [ -f /opt/bark/config/default-command ] && [ -z "$BARK_CMD_STARTED" ]; then
  BARK_CMD=$(cat /opt/bark/config/default-command)
  if [ -n "$BARK_CMD" ]; then
    export BARK_CMD_STARTED=1
    # shellcheck disable=SC2086
    exec $BARK_CMD
  fi
fi
