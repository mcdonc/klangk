#!/usr/bin/env bash
# Throwaway spike (#2385): mockup of the PERSISTENT consent-decider monitor.
# A compact tmux popup docked bottom-right over a shell, looking like
# `klangk consent-decide`: connected status + duration selector, a "waiting"
# state, then a held egress request arriving live (simulated). DELETE after spike.
#
# Run from the repo root, inside the devenv shell:
#   devenv --quiet -O dotenv.enable:bool false shell -- bash spike/run_popup_demo.sh
#
# The monitor auto-docks bottom-right when you attach. In the popup:
#   - sits at "No held requests — waiting…"
#   - after ~5s a held request appears (simulated) and counts down
#   - a = allow (resolves it)   q = hide   Ctrl-b p = bring it back
#   - Ctrl-b d = detach (tears down the disposable server)
#
# The auto-fire hook is session-scoped: it only fires for clients attaching to
# THIS session, so a client on another session (e.g. a Flutter terminal) gets
# nothing -- the shape the real feature needs.
set -euo pipefail

SOCKET="klangk-spike" # dedicated socket -> isolated from your real tmux server
SESSION="klangk-popup-spike"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO="$HERE/popup_demo.py"
PY="$(command -v python)"
# Compact monitor (54x13), docked bottom-right via formats (any terminal size).
POPUP=(display-popup -E -w 54 -h 13 -x "#{e|-:#{session_width},54}" -y "#{e|-:#{session_height},13}")

command -v tmux >/dev/null || {
  echo "tmux not found"
  exit 1
}
tmux -V | awk '{split($2,v,"."); if (v[1]<3 || (v[1]==3 && v[2]<2)) {print "tmux < 3.2, display-popup needs >=3.2: "$0; exit 1}}'
[ -x "$PY" ] || {
  echo "python not found (run under devenv shell)"
  exit 1
}

tmux -L "$SOCKET" kill-server 2>/dev/null || true
tmux -L "$SOCKET" new-session -d -s "$SESSION" -x 120 -y 36

# Auto-display the monitor on attach, scoped to THIS session.
tmux -L "$SOCKET" set-hook -g client-attached \
  "if -F \"#{==:#{client_session},$SESSION}\" \"${POPUP[*]} \\\"$PY $DEMO\\\"\""

# Re-open after it's been hidden.
tmux -L "$SOCKET" bind-key p "${POPUP[@]}" "$PY $DEMO"

trap 'tmux -L "$SOCKET" kill-server 2>/dev/null || true' EXIT

cat <<MSG
Spike ready on socket "$SOCKET", session "$SESSION". Attaching...
  The consent monitor should dock bottom-right on its own.
  a = allow | q = hide | Ctrl-b p = reopen | Ctrl-b d = detach
MSG

tmux -L "$SOCKET" attach -t "$SESSION"
