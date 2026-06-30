# shellcheck shell=sh
# Klangk plugin tools on PATH.
#
# Why this lives in /etc/profile.d and not /etc/bash.bashrc (issue #1093):
# /etc/profile resets PATH to a fixed system default for login shells,
# clobbering the /opt/klangk/bin prefix that the Dockerfile ENV sets. This
# snippet re-prepends it so EVERY login shell finds pi and the
# klangk-* helpers — including non-interactive login shells (`bash -lc`),
# which is what the workspace health check and `klangkc exec` use.
# /etc/bash.bashrc was the wrong home because it is only sourced for
# interactive shells; a non-interactive login shell never saw the export.
#
# Sourced by /etc/profile via run-parts for every login shell. /etc/profile
# runs profile.d unconditionally (no PS1 guard), so this covers `bash -lc`
# even though that shell never goes interactive. Keep POSIX-sh-clean: dash
# (the /bin/sh that /etc/profile runs under) sources this, not just bash.
case ":${PATH}:" in
*:/opt/klangk/bin:*) ;;
*) export PATH="/opt/klangk/bin:$PATH" ;;
esac
