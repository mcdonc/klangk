#!/usr/bin/env bash
# Full-resolution demo recorder.
#
# Playwright's built-in `video: "on"` caps the recording at ~800x450 — useless
# for a demo. This script decouples *driving* (Playwright) from *recording*
# (ffmpeg capturing a virtual X display), so the output is a true full-resolution
# .mp4 at whatever size you ask for.
#
# It spins up an Xvfb virtual display, starts ffmpeg recording it, runs your
# Playwright scene (headed, rendering to the virtual display), then stops ffmpeg
# and prints the .mp4 path + its real dimensions.
#
# Usage (from the worktree root, wrapped in devenv):
#   devenv shell -- src/frontend/e2e-tests/demo/record-demo.sh -g clanker
#   devenv shell -- src/frontend/e2e-tests/demo/record-demo.sh          # all scenes
#   KLANGK_DEMO_WIDTH=1920 KLANGK_DEMO_HEIGHT=1080 \
#     devenv shell -- src/frontend/e2e-tests/demo/record-demo.sh -g clanker
#
# Extra args (after the script name) are forwarded to `playwright test`.
# Requires: Xvfb + ffmpeg (both on the system PATH via NixOS).
#
# Output: src/frontend/e2e-tests/demo/demo-videos/<scene>-<timestamp>.mp4
#
# By default the browser chrome (~100px: tabs + omnibox) is CROPPED off the
# top so the output is clean app content at a YouTube-exact 16:9 size.
# (Playwright always shows chrome — --kiosk is ignored — so cropping is the
# reliable path; there is no black bar because the full window fills the canvas.)
#
# 2x-bigger-but-softer take: set KLANGK_DEMO_VW=960 KLANGK_DEMO_VH=540 (Flutter
# lays out 2x bigger) and KLANGK_DEMO_OUT_W=1920 KLANGK_DEMO_OUT_H=1080; this
# script upscales the 960x540 capture to 1920x1080 with lanczos. See
# playwright.demo.config.ts for why crisp-AND-2x in-browser isn't possible.

set -euo pipefail

WORKTREE_ROOT="$(git rev-parse --show-toplevel)"
cd "$WORKTREE_ROOT"

DEMO_DIR="src/frontend/e2e-tests/demo"
VIDEO_DIR="$DEMO_DIR/demo-videos"
mkdir -p "$VIDEO_DIR"

# Recording canvas. The browser renders into this; ffmpeg captures all of it.
# Default 1920x1080 (Flutter layout = page size). The Xvfb height is padded by
# CHROME_H to fit the FULL browser window (chrome + page) so the window fills
# the canvas with no gap (no bottom black bar).
WIDTH="${KLANGK_DEMO_VW:-${KLANGK_DEMO_WIDTH:-1920}}"
HEIGHT="${KLANGK_DEMO_VH:-${KLANGK_DEMO_HEIGHT:-1080}}"
# Output size (after optional upscale). Default = capture size (no upscale).
OUT_W="${KLANGK_DEMO_OUT_W:-$WIDTH}"
OUT_H="${KLANGK_DEMO_OUT_H:-$HEIGHT}"
# Browser chrome (tabs + omnibox) height in px. Measured ~90px at 1920 wide,
# ~100px at 960 wide. The Xvfb canvas is padded by this to fit the FULL window
# (chrome + page); the top CHROME_H rows are then cropped off so the output is
# clean app content only — no URL bar, no black bar. (--kiosk doesn't work
# under Playwright: it manages its own window, so chrome always shows. Cropping
# is the reliable path.)
CHROME_H="${KLANGK_DEMO_CHROME_H:-100}"
# Crop the chrome off the top → clean WIDTH x HEIGHT output (default on).
CROP="${KLANGK_DEMO_CROP:-1}"
if [ "$CROP" = "1" ]; then
  XVFB_H=$((HEIGHT + CHROME_H))
else
  XVFB_H="${KLANGK_DEMO_XVFB_H:-$((HEIGHT + CHROME_H))}"
fi
DISPLAY_NUM="${KLANGK_DEMO_DISPLAY:-99}"
export DISPLAY=":${DISPLAY_NUM}"

TS="$(date +%Y%m%d-%H%M%S)"
OUT="$VIDEO_DIR/recording-${TS}.mp4"

echo "=== demo recorder ==="
if [ "$OUT_W" != "$WIDTH" ] || [ "$OUT_H" != "$HEIGHT" ]; then
  OUT_DESC="${WIDTH}x${HEIGHT} → upscale → ${OUT_W}x${OUT_H}"
elif [ "$CROP" = "1" ]; then
  OUT_DESC="${WIDTH}x${HEIGHT} (chrome cropped)"
else
  OUT_DESC="${WIDTH}x${XVFB_H}"
fi
echo "  chrome  : ${CHROME_H}px $([ "$CROP" = "1" ] && echo "(cropped)" || echo "(kept)")"
echo "  canvas  : ${WIDTH}x${XVFB_H}  (Flutter layout ${WIDTH}x${HEIGHT})"
echo "  display : $DISPLAY"
echo "  output  : $OUT  (${OUT_DESC})"
echo

# --- 1. Clean up any stale Xvfb on this display, then start a fresh one. ---
if [ -f "/tmp/.X${DISPLAY_NUM}-lock" ]; then
  OLD_PID="$(cat "/tmp/.X${DISPLAY_NUM}-lock" 2>/dev/null || true)"
  if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "  (killing stale Xvfb pid $OLD_PID on :$DISPLAY_NUM)"
    kill "$OLD_PID" 2>/dev/null || true
  fi
  rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"
fi
sleep 0.3

Xvfb "$DISPLAY" -screen 0 "${WIDTH}x${XVFB_H}x24" -ac +extension RANDR \
  >/tmp/demo-xvfb.log 2>&1 &
XVFB_PID=$!
# Wait until Xvfb is accepting connections.
for _ in $(seq 1 40); do
  xdpyinfo >/dev/null 2>&1 && break
  sleep 0.1
done
echo "  Xvfb up (pid $XVFB_PID)"

cleanup() {
  echo
  echo "=== stopping ffmpeg + Xvfb ==="
  # Send 'q' to ffmpeg for a clean finalize, then fall back to TERM/kill.
  kill -INT "$FFMPEG_PID" 2>/dev/null || true
  for _ in $(seq 1 30); do
    kill -0 "$FFMPEG_PID" 2>/dev/null || break
    sleep 0.2
  done
  kill "$FFMPEG_PID" 2>/dev/null || true
  wait "$FFMPEG_PID" 2>/dev/null || true
  kill "$XVFB_PID" 2>/dev/null || true
  rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"
}
trap cleanup EXIT

# --- 2. Start ffmpeg recording the virtual display. ---
# -draw_mouse shows the cursor. In non-kiosk+crop mode, a crop filter drops the
# top CHROME_H rows of chrome. If OUT_W/H differ from WIDTH/HEIGHT, scale the
# capture (lanczos) — the 2x-bigger take renders 960x540 and upscales to 1080.
VF_ARGS=()
if [ "$CROP" = "1" ]; then
  VF_ARGS+=(-vf "crop=${WIDTH}:${HEIGHT}:0:${CHROME_H}")
fi
if [ "$OUT_W" != "$WIDTH" ] || [ "$OUT_H" != "$HEIGHT" ]; then
  EXISTING="${VF_ARGS[1]:-}"
  if [ -n "$EXISTING" ]; then
    VF_ARGS=(-vf "${EXISTING},scale=${OUT_W}:${OUT_H}:flags=lanczos")
  else
    VF_ARGS=(-vf "scale=${OUT_W}:${OUT_H}:flags=lanczos")
  fi
fi

ffmpeg -y -hide_banner -loglevel error \
  -f x11grab -draw_mouse 1 \
  -video_size "${WIDTH}x${XVFB_H}" \
  -framerate "${KLANGK_DEMO_FPS:-30}" \
  -i "$DISPLAY+0,0" \
  "${VF_ARGS[@]}" \
  -c:v libx264 -pix_fmt yuv420p -preset "${KLANGK_DEMO_X264_PRESET:-medium}" \
  -crf "${KLANGK_DEMO_CRF:-20}" \
  "$OUT" &
FFMPEG_PID=$!
echo "  ffmpeg recording (pid $FFMPEG_PID)"
echo

# --- 3. Run the Playwright scene(s), headed, on the virtual display. ---
# Headed (not headless) so Chromium paints to $DISPLAY=Xvfb. The demo config's
# viewport == page size; the browser window (page + chrome) fills the canvas.
echo "=== running playwright (args: $*) ==="
# `set +e` so a failing scene still lets ffmpeg finalize the recording.
set +e
"$WORKTREE_ROOT/src/frontend/e2e-tests/node_modules/.bin/playwright" test \
  --config="$DEMO_DIR/playwright.demo.config.ts" "$@"
SCENE_RC=$?
set -e

# --- 4. Finalize + report. ---
echo
echo "=== finalizing recording ==="
cleanup
trap - EXIT

if [ -f "$OUT" ]; then
  DIMS="$(ffprobe -v error -select_streams v:0 \
    -show_entries stream=width,height -of csv=p=0:s=x "$OUT" 2>/dev/null || echo "?")"
  DUR="$(ffprobe -v error -show_entries format=duration \
    -of default=noprint_wrappers=1:nokey=1 "$OUT" 2>/dev/null || echo "?")"
  echo
  echo "✓ recorded: $OUT"
  echo "  dimensions: ${DIMS}   duration: ${DUR}s"
else
  echo "✗ no recording produced" >&2
fi

exit "$SCENE_RC"
