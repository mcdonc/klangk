# shellcheck shell=sh
# Klangk feature tools and user-installed tools on PATH.
#
# Why this lives in /etc/profile.d and not /etc/bash.bashrc (issue #1093):
# /etc/profile resets PATH to a fixed system default for login shells,
# clobbering the /opt/klangk/bin prefix that the Dockerfile ENV sets. This
# snippet re-prepends it so EVERY login shell finds pi and the
# klangk-* helpers — including non-interactive login shells (`bash -lc`),
# which is what `klangk exec` uses.
# /etc/bash.bashrc was the wrong home because it is only sourced for
# interactive shells; a non-interactive login shell never saw the export.
#
# ~/.local/bin goes in FRONT of /opt/klangk/bin (#3522): executables the
# user installs there (uv tool install and other user-bin installers)
# must resolve on the next shell and must shadow the vendored klangk-*
# helpers. The prepend runs only when the entry is not already on PATH:
# the guard keeps re-sourcing idempotent, and a PATH inherited with the
# entry already present is left at its existing position. /etc/profile
# resets PATH and runs profile.d before ~/.profile is sourced, so every
# login shell composes the fresh order above; Debian's skel ~/.profile
# then re-prepends ~/.local/bin itself once the directory exists — a
# duplicate entry that is harmless (first match wins, shadowing
# unchanged). The prepend is skipped when HOME is unset or empty. The
# shell creates the directory itself (see the mkdir below), but the
# prepend tolerates it missing regardless: a PATH entry naming a
# missing directory is harmless.
#
# (The workspace health check is NOT a consumer of this: it runs a
# non-login `bash -c` and sources nothing. See
# docs/features/health-check.md.)
#
# Sourced by /etc/profile via run-parts for every login shell. /etc/profile
# runs profile.d unconditionally (no PS1 guard), so this covers `bash -lc`
# even though that shell never goes interactive. Keep POSIX-sh-clean: dash
# (the /bin/sh that /etc/profile runs under) sources this, not just bash.
case ":${PATH}:" in
*:/opt/klangk/bin:*) ;;
*) export PATH="/opt/klangk/bin:$PATH" ;;
esac
if [ -n "${HOME:-}" ]; then
  # Create the directory up front (#3522): a manual
  # `ln -s <binary> ~/.local/bin/foo` then works in a fresh workspace
  # with no mkdir first. mkdir -p is idempotent; a failure (read-only
  # home) stays quiet — a login shell must never break over this.
  [ -d "${HOME}/.local/bin" ] || mkdir -p "${HOME}/.local/bin" 2>/dev/null || :
  case ":${PATH}:" in
  *:"${HOME}/.local/bin":*) ;;
  *) export PATH="${HOME}/.local/bin:$PATH" ;;
  esac
fi
