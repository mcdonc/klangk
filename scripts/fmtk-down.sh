#!/usr/bin/env bash
# Stop the fmtk harness services (issue #2881).
#
#   devenv --quiet shell -- fmtk-down [--wipe]
#
# fmtk-up keeps the scratch backend + proxy alive across launches (fast
# re-launch); this stops them: the debug flutter run, its Chrome, the
# scratch klangkd (which takes its caddy children with it), and the
# harness caddy. --wipe also deletes the scratch state dir (fresh DB on
# the next fmtk-up; same as fmtk-up --fresh).
#
# Patterns use [x] bracket tricks so pkill never matches this script's
# own command line.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVENV_STATE="${DEVENV_STATE:-$REPO_ROOT/.devenv/state}"
STATE_DIR="${FMTK_STATE:-$DEVENV_STATE/fmtk}"
FLUTTER_PORT="${FMTK_FLUTTER_PORT:-8125}"
PROXY_PORT="${FMTK_PROXY_PORT:-8124}"

log() { printf '\033[1m[fmtk-down]\033[0m %s\n' "$*"; }

for pattern in \
  "run --debug -d chrome.*--web-port $FLUTTER_PORT" \
  "[c]hrome.*127.0.0.1:$PROXY_PORT" \
  "klangk[.]main --config $STATE_DIR/klangkd.yaml" \
  "[c]addy run --config $STATE_DIR/proxy.Caddyfile"; do
  if pkill -TERM -f "$pattern" 2>/dev/null; then
    log "stopped: $pattern"
  fi
done
sleep 2
for pattern in \
  "run --debug -d chrome.*--web-port $FLUTTER_PORT" \
  "[c]hrome.*127.0.0.1:$PROXY_PORT" \
  "klangk[.]main --config $STATE_DIR/klangkd.yaml" \
  "[c]addy run --config $STATE_DIR/proxy.Caddyfile"; do
  pkill -KILL -f "$pattern" 2>/dev/null || true
done

if [[ ${1:-} == "--wipe" ]]; then
  rm -rf "$STATE_DIR"
  log "wiped scratch state: $STATE_DIR"
elif [[ -n ${1:-} ]]; then
  log "unknown flag: $1 (only --wipe)"
  exit 2
fi
log "done"
