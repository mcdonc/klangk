#!/usr/bin/env bash
# Full-resolution (≥1080p) recorder for a SCRIPTED terminal session.
#
# Playwright's built-in `video` caps browser recordings; for a *terminal*
# there is no equivalent at all. This script decouples *driving* the terminal
# (your driver script: cli_demo.py) from *recording* it (ffmpeg capturing an
# Xvfb virtual display), so the output is a true full-resolution .mp4.
#
# Pipeline:
#   1. Xvfb          — a virtual X display at the canvas size you ask for
#   2. xterm         — a real terminal emulator on that display (big font),
#                      attached to a tmux session
#   3. tmux          — owns the pty; xterm *displays* it, the driver *writes*
#                      to it via `send-keys` and reads it via `capture-pane`
#   4. ffmpeg        — captures the whole Xvfb display → true-resolution .mp4
#   5. your driver   — types the scene into the tmux session
#
# Why tmux and not pexpect? pexpect.spawn() takes the pty *master*, so no
# terminal emulator can render that session live (xterm would need to be the
# master). tmux multiplexes the pty: xterm renders it to the screen while the
# driver scripts it. See README.md "Why not pexpect?" for the full rationale.
#
# Usage (from the worktree root, wrapped in devenv):
#   # Run the built-in self-contained demo scene (no klangk server needed):
#   devenv shell -- src/frontend/e2e-tests/demo/record-terminal.sh \
#       python3 src/frontend/e2e-tests/demo/cli_demo.py --scene demo
#
#   # Run a real CLI scene against a live klangk server:
#   devenv shell -- src/frontend/e2e-tests/demo/record-terminal.sh \
#       python3 src/frontend/e2e-tests/demo/cli_demo.py --scene scene_2
#
#   # Anything after the script name is the DRIVER command + its args.
#
# Output: src/frontend/e2e-tests/demo/recordings/<scene>-<timestamp>.mp4
#
# Requires (all present on NixOS / via devenv): Xvfb, xterm, tmux, ffmpeg,
# ffprobe, xdotool. The Python driver is stdlib-only (no pexpect, no pip).
#
# Knobs (env vars):
#   KLANGK_DEMO_WIDTH        default 1920    canvas width  (px)
#   KLANGK_DEMO_HEIGHT       default 1080    canvas height (px)
#   KLANGK_DEMO_FONT         default "DejaVu Sans Mono"
#   KLANGK_DEMO_FONT_SIZE    default 22      terminal font size (pt)
#   KLANGK_DEMO_FPS          default 30      capture framerate
#   KLANGK_DEMO_CRF          default 20      x264 quality (lower = better)
#   KLANGK_DEMO_X264_PRESET  default medium
#   KLANGK_DEMO_DISPLAY      default 97      Xvfb display number to use
#   KLANGK_DEMO_TMUX_SESSION default klangk-demo
#   KLANGK_DEMO_OUTPUT       default src/frontend/e2e-tests/demo/recordings/recording-<ts>.mp4
#   KLANGK_DEMO_PROMPT       default "host $ "
#                            (PS1 for the session shell; supports ANSI escapes)

set -uo pipefail

WORKTREE_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$WORKTREE_ROOT"

WIDTH="${KLANGK_DEMO_WIDTH:-1920}"
HEIGHT="${KLANGK_DEMO_HEIGHT:-1080}"
FONT="${KLANGK_DEMO_FONT:-DejaVu Sans Mono}"
FONT_SIZE="${KLANGK_DEMO_FONT_SIZE:-22}"
FPS="${KLANGK_DEMO_FPS:-30}"
CRF="${KLANGK_DEMO_CRF:-20}"
PRESET="${KLANGK_DEMO_X264_PRESET:-medium}"
DISPLAY_NUM="${KLANGK_DEMO_DISPLAY:-97}"
SESSION="${KLANGK_DEMO_TMUX_SESSION:-klangk-demo}"
PROMPT="${KLANGK_DEMO_PROMPT:-host $ }"
export DISPLAY=":${DISPLAY_NUM}"

OUT="${KLANGK_DEMO_OUTPUT:-src/frontend/e2e-tests/demo/recordings/recording-$(date +%Y%m%d-%H%M%S).mp4}"
mkdir -p "$(dirname "$OUT")"

if [ "$#" -eq 0 ]; then
  echo "usage: $0 <driver-command> [driver-args...]" >&2
  echo "  e.g. $0 python3 src/frontend/e2e-tests/demo/cli_demo.py --scene demo" >&2
  exit 2
fi

echo "=== terminal demo recorder ==="
echo "  canvas : ${WIDTH}x${HEIGHT}"
echo "  font   : ${FONT} ${FONT_SIZE}pt"
echo "  display: $DISPLAY   tmux session: $SESSION"
echo "  output : $OUT"
echo "  driver : $*"
echo

# --- helpers ---------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }
for t in Xvfb xterm tmux ffmpeg ffprobe xdotool; do
  if ! have "$t"; then
    echo "error: missing required tool '$t'" >&2
    exit 1
  fi
done

cleanup_display() {
  if [ -n "${XVFB_PID:-}" ]; then kill "$XVFB_PID" 2>/dev/null || true; fi
  if [ -n "${XTerm_PID:-}" ]; then kill "$XTerm_PID" 2>/dev/null || true; fi
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"
  rm -f "${RCFILE:-}"
}

# --- 1. clean slate + start Xvfb -------------------------------------------
cleanup_display
if [ -f "/tmp/.X${DISPLAY_NUM}-lock" ]; then
  OLD_PID="$(cat "/tmp/.X${DISPLAY_NUM}-lock" 2>/dev/null || true)"
  if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    kill "$OLD_PID" 2>/dev/null || true
  fi
  rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"
fi
sleep 0.2

Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${HEIGHT}x24" -ac +extension RANDR \
  >/tmp/klangk-demo-xvfb.log 2>&1 &
XVFB_PID=$!
# Wait for Xvfb to create its socket. (We avoid xdpyinfo — it's not on the
# devenv PATH; the socket file is the cheapest readiness signal.)
SOCK="/tmp/.X11-unix/X${DISPLAY_NUM}"
for _ in $(seq 1 50); do
  [ -S "$SOCK" ] && break
  sleep 0.1
done
if [ ! -S "$SOCK" ]; then
  echo "error: Xvfb did not come up on $DISPLAY" >&2
  cat /tmp/klangk-demo-xvfb.log >&2
  cleanup_display
  exit 1
fi
sleep 0.3 # let the server settle into "accepting connections"
echo "  Xvfb up (pid $XVFB_PID)"

# --- 2. tmux session (shared rc-file shell) + xterm ----------------------
# Every pane — the session shell AND any `split-window` the driver creates —
# runs `bash --rcfile` against one generated file, so they share an identical
# setup: the klangk venv on PATH (so `klangk` resolves), the clean `host $ `
# prompt (NOT devenv shell, so no powerline/starship banner to hide), the
# forwarded SSH agent socket (so `klangk shell -A` works), and a steady
# non-blinking cursor. Writing it once — instead of typing the exports into
# the session via send-keys — keeps startup reliable and lets split panes
# inherit the same environment with no extra typing on camera.
VENV_BIN="$WORKTREE_ROOT/.devenv/state/venv/bin"
SSH_SOCK="${SSH_AUTH_SOCK:-/run/user/$(id -u)/ssh-agent}"
RCFILE="${KLANGK_DEMO_RCFILE:-$(mktemp -t klangk-demo-pane-rc.XXXXXX)}"
export KLANGK_DEMO_RCFILE="$RCFILE"
cat >"$RCFILE" <<RC
# klangk demo recorder — per-pane shell setup (auto-generated; do not edit).
# Sourced by every pane via 'bash --rcfile'.
export PATH="$VENV_BIN:\$PATH"
export SSH_AUTH_SOCK="$SSH_SOCK"
PS1='$PROMPT'
# Steady (non-blinking) block cursor; re-emit before each prompt so a TUI app
# that changes cursor style on exit never leaves it blinking.
PROMPT_COMMAND='printf "\033[2 q"'
printf '\033[2 q'
clear
RC

tmux new-session -d -s "$SESSION" -x "${WIDTH}" -y "${HEIGHT}" \
  "bash --rcfile '$RCFILE' -i"

# Hide ALL tmux chrome so none of it appears on camera: no status bar, no pane
# borders, no message line. (The default status bar would show at the bottom.)
tmux set -t "$SESSION" status off

# xterm attaches to the session and renders it to the virtual display.
# NOTE: `tmux attach -t`, NOT `attach-client -t` — the latter's -t names a
# *client*, so it fails and xterm exits instantly, leaving ffmpeg recording
# an empty display. `attach`/`attach-session` is the session-attaching form.
xterm -display "$DISPLAY" \
  -title "klangk-demo-recording" \
  -fs "$FONT_SIZE" -fa "$FONT" \
  -bg black -fg white -uc -bc \
  -xrm 'xterm*scrollBar:false' \
  -xrm 'xterm*cursorBlink:false' \
  -e tmux attach -t "$SESSION" &
XTerm_PID=$!

# Wait for the xterm window to map, then size it edge-to-edge on the canvas
# for a clean, full-screen look. Search by class ("xterm") — the window -title
# is overridden by tmux's own title escapes once it attaches.
WIN=""
for _ in $(seq 1 50); do
  WIN="$(xdotool search --class xterm 2>/dev/null | head -1 || true)"
  [ -n "$WIN" ] && break
  sleep 0.1
done
if [ -n "${WIN:-}" ]; then
  xdotool windowsize "$WIN" "$WIDTH" "$HEIGHT" 2>/dev/null || true
  xdotool windowmove "$WIN" 0 0 2>/dev/null || true
  xdotool windowfocus "$WIN" 2>/dev/null || true
fi
echo "  xterm up (pid $XTerm_PID, window ${WIN:-?})"

# --- 3. ffmpeg captures the whole virtual display --------------------------
ffmpeg -y -hide_banner -loglevel error \
  -f x11grab -draw_mouse 0 \
  -video_size "${WIDTH}x${HEIGHT}" \
  -framerate "$FPS" \
  -i "$DISPLAY+0,0" \
  -c:v libx264 -pix_fmt yuv420p -preset "$PRESET" -crf "$CRF" \
  "$OUT" &
FFMPEG_PID=$!
echo "  ffmpeg recording (pid $FFMPEG_PID)"
echo

# --- 4. run the driver -----------------------------------------------------
# Hand the driver the session name + display so it can drive tmux. `set +e`
# so a failing/aborted scene still lets ffmpeg finalize the recording.
export KLANGK_DEMO_TMUX_SESSION="$SESSION"
export DISPLAY
echo "=== running driver ==="
set +e
"$@"
DRIVER_RC=$?
set -e

# --- 5. finalize + report --------------------------------------------------
echo
echo "=== finalizing recording ==="

# Optionally dump every surviving pane's full scrollback to a text transcript
# next to the recording (the cheap, vision-free way to QA a take). OFF by
# default -- set KLANGK_DEMO_TRANSCRIPT=1 to enable. (Panes a scene killed
# mid-take aren't captured -- only the survivors are.)
if [ "${KLANGK_DEMO_TRANSCRIPT:-0}" = "1" ]; then
  TRANSCRIPT="${OUT%.*}.transcript.txt"
  {
    echo "# transcript for: $OUT"
    echo "# driver: $*"
    echo "# generated: $(date -Iseconds)"
    echo
    mapfile -t PANES < <(tmux list-panes -t "$SESSION" -F '#{pane_id}' 2>/dev/null)
    for pane in "${PANES[@]}"; do
      echo "================ pane ${pane} ================"
      tmux capture-pane -t "$pane" -p -S -100000 2>/dev/null
      echo
    done
  } >"$TRANSCRIPT"
  echo "  transcript: $TRANSCRIPT"
fi

# Give the last frame a beat to land, then 'q' ffmpeg for a clean mux.
sleep 0.6
kill -INT "$FFMPEG_PID" 2>/dev/null || true
for _ in $(seq 1 40); do
  kill -0 "$FFMPEG_PID" 2>/dev/null || break
  sleep 0.15
done
kill "$FFMPEG_PID" 2>/dev/null || true
wait "$FFMPEG_PID" 2>/dev/null || true
cleanup_display

if [ -f "$OUT" ]; then
  DIMS="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=width,height -of csv=p=0:s=x "$OUT" 2>/dev/null || echo "?")"
  DUR="$(ffprobe -v error -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$OUT" 2>/dev/null || echo "?")"
  echo
  echo "✓ recorded: $OUT"
  echo "  dimensions: ${DIMS}   duration: ${DUR}s   driver rc: ${DRIVER_RC}"
else
  echo "✗ no recording produced (driver rc: ${DRIVER_RC})" >&2
fi

exit "$DRIVER_RC"
