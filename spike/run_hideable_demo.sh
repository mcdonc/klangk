#!/usr/bin/env bash
# Throwaway spike (#2385, hide-keeps-running variant): the consent decider runs
# in a HIDDEN persistent tmux session (so it stays registered while you shell),
# and the popup is a transient VIEWER that attaches to it. Hiding the popup just
# detaches the viewer -- the decider keeps running. DELETE after the spike.
#
# Usage (repo root, under devenv):
#   devenv --quiet -O dotenv.enable:bool false shell -- \
#     bash spike/run_hideable_demo.sh <workspace-name-or-id> [W] [H]
#
# Keys:
#   - the consent popup is auto-shown on attach
#   - Ctrl-b p     show the consent decider (popup viewer)
#   - Ctrl-b d     INSIDE the popup: hide it (decider keeps running, stays registered)
#   - Ctrl-b d     in the shell: detach -> teardown (also kills the hidden decider)
#   - DO NOT press q in the popup -- that quits the decider for real (disconnects it).
set -euo pipefail

WS="${1:?usage: $0 <workspace-name-or-id> [W] [H]}"
W="${2:-72}"
H="${3:-20}"
SOCKET="klangk-spike"
SHELL_SESS="klangk-shell"
DEC_SESS="klangk-consent"

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

DEC_CMD="$KL consent-decide $WS"
VIEW="env -u TMUX tmux -L $SOCKET attach -t $DEC_SESS"
POPUP=(display-popup -E -w "$W" -h "$H" -x "#{e|-:#{session_width},$W}" -y "#{e|-:#{session_height},$H}")

tmux -L "$SOCKET" kill-server 2>/dev/null || true

# Hidden persistent decider session: the decider runs HERE, so it stays connected
# (registered with the daemon) regardless of whether the popup viewer is open.
tmux -L "$SOCKET" new-session -d -s "$DEC_SESS" -x "$W" -y "$H" "$DEC_CMD"
# The shell session the user attaches to (the "klangk shell" stand-in).
tmux -L "$SOCKET" new-session -d -s "$SHELL_SESS" -x 120 -y 36

# Auto-show the viewer when a client attaches to the shell session (scoped).
tmux -L "$SOCKET" set-hook -g client-attached \
  "if -F \"#{==:#{client_session},$SHELL_SESS}\" \"${POPUP[*]} \\\"$VIEW\\\"\""
# Reopen the viewer after hiding it.
tmux -L "$SOCKET" bind-key p "${POPUP[@]}" "$VIEW"

trap 'tmux -L "$SOCKET" kill-server 2>/dev/null || true' EXIT

cat <<MSG
Hide-keeps-running spike on socket "$SOCKET". Attaching to shell "$SHELL_SESS"...
  decider runs hidden in session "$DEC_SESS" -> stays registered while you shell.
  Ctrl-b p  show decider popup | Ctrl-b d (in popup) hide | Ctrl-b d (in shell) exit
  DO NOT press q in the popup (that quits the decider for real).
MSG

tmux -L "$SOCKET" attach -t "$SHELL_SESS"
