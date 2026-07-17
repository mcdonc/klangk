# shellcheck shell=sh
# Default editor for git commit messages, crontab -e, etc.
#
# In /etc/profile.d (not /etc/bash.bashrc) so that non-interactive login
# shells — e.g. `klangk exec` running `git commit` — see EDITOR too.
# /etc/bash.bashrc only loads for interactive shells, which would hide
# this from one-shot commands. See issue #1093.
# (The workspace health check is NOT a consumer of this: it runs a
# non-login `bash -c` and sources nothing. See docs/features/health-check.md.)
export EDITOR=nano
