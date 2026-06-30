# shellcheck shell=sh
# Default editor for git commit messages, crontab -e, etc.
#
# In /etc/profile.d (not /etc/bash.bashrc) so that non-interactive login
# shells — e.g. a health check or `klangkc exec` that runs `git commit` —
# see EDITOR too. /etc/bash.bashrc only loads for interactive shells,
# which would hide this from one-shot commands. See issue #1093.
export EDITOR=nano
