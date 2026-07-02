#!/usr/bin/env bash
# klangk health check for the openclaw sandbox.
#
# The host health monitor runs this via `bash -c` -- a NON-login shell,
# so ~/.profile, ~/.bashrc, and /etc/profile.d are NOT sourced and
# nothing the owning user set up interactively is in effect. This is
# deliberate: the probe must stay deterministic and immune to the user
# editing their shell startup (a slow nvm load or a broken ~/.profile
# must never make a 30s poll flap "unhealthy").
#
# Consequence: the openclaw binary and OPENCLAW_HOME are referenced by
# absolute path here, not via PATH. setup.sh points
# /openclaw/bin/openclaw at the nvm-installed binary (npm rewrites its
# shebang to an absolute node path, so it runs standalone without node
# on PATH) and copies this script to /openclaw/bin/healthcheck.sh; the
# sandbox config points `health-check` at that absolute path.
# See docs/features/health-check.md.
set -euo pipefail
export OPENCLAW_HOME=/openclaw

# `openclaw health` connects to the running gateway over WebSocket and
# exits non-zero if it is unreachable -- a liveness check for the
# service the service-command (`openclaw gateway`) launches.
exec /openclaw/bin/openclaw health
