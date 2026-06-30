#!/usr/bin/env bash
# klangk health check for the hermes sandbox.
#
# The host health monitor runs this via `bash -c` -- a NON-login shell,
# so ~/.profile, ~/.bashrc, and /etc/profile.d are NOT sourced and
# nothing the owning user set up interactively is in effect. This is
# deliberate: the probe must stay deterministic and immune to the user
# editing their shell startup (a slow nvm load or a broken ~/.profile
# must never make a 30s poll flap "unhealthy").
#
# Consequence: the hermes binary and HERMES_HOME are referenced by
# absolute path here, not via PATH. setup.sh copies this script to
# /hermes/bin/healthcheck.sh; the sandbox config points `health-check`
# at that absolute path. See docs/features/health-check.md.
set -euo pipefail
export HERMES_HOME=/hermes

# `hermes gateway status` always exits 0 (it only prints state), so the
# liveness signal is derived from its output: "Gateway is running" is
# NOT a substring of "Gateway is not running" (the word after "is "
# differs), so the grep is unambiguous. hermes's own process detection
# (PID file + /proc scan + PID-reuse fingerprinting) does the work.
#
# The venv entry point has an absolute shebang to its interpreter, so it
# runs standalone without hermes/node on PATH.
/hermes/hermes-agent/venv/bin/hermes gateway status 2>&1 |
  grep -q 'Gateway is running'
