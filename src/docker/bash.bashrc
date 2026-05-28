# shellcheck shell=bash
# System-wide bash defaults for Bark containers.
# Users can override these in ~/.bashrc on the persistent home mount.

# Ignore Ctrl+C until setup is complete and any default command has started.
trap '' INT

# Wait for the entrypoint to finish setup before showing a prompt.
# Prevents races where the user runs pi before config files are ready.
# /tmp is a tmpfs, so .bark-command is cleared on every container start.
while [ ! -f /tmp/.bark-command ]; do sleep 0.1; done

# No default command — restore Ctrl+C and set up interactive shell.
trap - INT

PS1='\[\033[01;34m\]\w\[\033[00m\]\$ '
HISTFILE=~/.bash_history
HISTSIZE=1000
HISTFILESIZE=2000
shopt -s histappend
PROMPT_COMMAND="history -a"
alias ls='ls --color=auto'
alias grep='grep --color=auto'

# If .bark-command contains a command, exec into it (replaces this shell).
# Guard against infinite recursion if the command is bash itself.
BARK_CMD=$(cat /tmp/.bark-command)
if [ -n "$BARK_CMD" ] && [ -z "$BARK_CMD_STARTED" ]; then
  export BARK_CMD_STARTED=1
  # shellcheck disable=SC2086
  exec $BARK_CMD
fi
