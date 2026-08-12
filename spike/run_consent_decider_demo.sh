#!/usr/bin/env bash
# Throwaway spike (#2385, extended): run the REAL `klangk consent-decide` in the
# popup, so we can judge whether the existing app needs changes to act as the
# persistent monitor. This is LIVE: it connects to your real klangkd and
# registers a real decider for the workspace (real verdicts if holds arrive).
# DELETE after the spike.
#
# Usage (from repo root, under devenv):
#   devenv --quiet -O dotenv.enable:bool false shell -- \
#     bash spike/run_consent_decider_demo.sh <workspace-name-or-id> [W] [H]
#
# Defaults: 72x20, docked bottom-right. The popup auto-shows on attach.
#   q quits consent-decide (closes the popup) | Ctrl-b p reopens | Ctrl-b d detaches
set -euo pipefail

WS="${1:?usage: $0 <workspace-name-or-id> [W] [H]}"
W="${2:-72}"
H="${3:-20}"
SOCKET="klangk-spike"
SESSION="klangk-popup-spike"

KL="$(command -v klangk || true)"
[ -n "$KL" ] || {
  echo "klangk not on PATH (run under devenv shell)"
  exit 1
}
command -v tmux >/dev/null || {
  echo "tmux not found"
  exit 1
}
tmux -V | awk '{split($2,v,"."); if (v[1]<3 || (v[1]==3 && v[2]<2)) {print "tmux < 3.2, display-popup needs >=3.2: "$0; exit 1}}'

CMD="$KL consent-decide $WS"
POPUP=(display-popup -E -w "$W" -h "$H" -x "#{e|-:#{session_width},$W}" -y "#{e|-:#{session_height},$H}")

tmux -L "$SOCKET" kill-server 2>/dev/null || true
tmux -L "$SOCKET" new-session -d -s "$SESSION" -x 120 -y 36

# Auto-display on attach, scoped to THIS session.
tmux -L "$SOCKET" set-hook -g client-attached \
  "if -F \"#{==:#{client_session},$SESSION}\" \"${POPUP[*]} \\\"$CMD\\\"\""
# Reopen after quit.
tmux -L "$SOCKET" bind-key p "${POPUP[@]}" "$CMD"

trap 'tmux -L "$SOCKET" kill-server 2>/dev/null || true' EXIT

cat <<MSG
LIVE spike on socket "$SOCKET", session "$SESSION". Attaching...
  popup: $CMD  (${W}x${H}, docked bottom-right)
  q quit | Ctrl-b p reopen | Ctrl-b d detach
MSG

tmux -L "$SOCKET" attach -t "$SESSION"
