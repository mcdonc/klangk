#!/usr/bin/env bash
# Record CLI demo scenes (2, 3, 3b) for the Klangk intro video.
#
# One reusable, idempotent driver. Each scene has well-defined prep so the
# whole thing is safe to re-run as many times as needed.
#
#   record-cli.sh 2      record Scene 2 only
#   record-cli.sh 3      record Scene 3 only
#   record-cli.sh 3b     record Scene 3b only
#   record-cli.sh all    record 2 → 3 → 3b in sequence
#
# State flow (the recording shell shares $HOME, so klangkc login state and the
# container layer persist between takes):
#   Scene 2  prep: logout + rm demo  → records an on-camera login + create
#   Scene 3  prep: ensure login + rm openclaw → on-camera sandbox create
#   Scene 3b prep: ensure login + openclaw must be healthy (carried from 3)
#
# Preconditions YOU manage (not this script): the klangk server must be up at
# $SERVER with KLANGK_ALLOW_AUTOSTART=1 + KLANGK_HEALTH_CHECK_INTERVAL=10, the
# hero (admin@example.com / adminpass) must exist + be an admin, and demo-seed
# must have been run. The openclaw host install (nvm/Node) must already be
# present in sandboxes/openclaw/ so the on-camera create is fast.
set -uo pipefail

WT=/home/chrism/projects/klangk/.worktrees/demo-video-scripts
cd "$WT" || exit 1
DEMO_DIR="src/frontend/e2e-tests/demo"
RECORDINGS_DIR="$DEMO_DIR/recordings"
mkdir -p "$RECORDINGS_DIR"
SERVER=http://localhost:8995
HERO=admin@example.com
PASS=adminpass
SCENE="${1:-}"
[ -n "$SCENE" ] || {
  echo "usage: $0 <2|3|3b|all>"
  exit 2
}

# --- helpers ---------------------------------------------------------------
# Filter devenv shell's progress noise so only real output shows.
quiet() { grep -vE "Validating|^•|Configuring|cachix|Evaluating|Loading|Running|✓ Running tasks|warning: Substituter|Using config|^✓ " || true; }

kc() { devenv shell -- klangkc "$@" 2>&1 | quiet; }

ensure_logged_out() {
  echo "  [prep] logout"
  kc logout "$SERVER" >/dev/null 2>&1 || true
}

ensure_logged_in() {
  echo "  [prep] login as $HERO"
  # logout first so a stale session never short-circuits login
  kc logout "$SERVER" >/dev/null 2>&1 || true
  printf "%s" "$PASS" | devenv shell -- klangkc login --password-file - "$SERVER" "$HERO" 2>&1 | quiet | tail -1
}

rm_ws() {
  echo "  [prep] rm workspace '$1' (if present)"
  kc rm "$1" >/dev/null 2>&1 || true
}

ensure_healthy() {
  # Warn (don't fail) — the user may handle the openclaw state in post.
  local st
  st=$(kc ls 2>/dev/null | awk -v w="$1" '$1==w{print $3}')
  echo "  [prep] $1 status = ${st:-MISSING}"
  if [ "$st" != "healthy" ]; then
    echo "  [prep] WARNING: $1 is not healthy — scene 3b may fail." >&2
  fi
}

# Kill any stale recorder display/session so each take starts clean.
clean_display() {
  pkill -9 -f "Xvfb :97" 2>/dev/null || true
  tmux kill-session -t klangk-demo 2>/dev/null || true
  rm -f /tmp/.X97-lock /tmp/.X11-unix/X97
  sleep 2
}

# record <cli_demo_scene> <output_filename>
record() {
  local scene=$1 out="$RECORDINGS_DIR/$2"
  echo
  echo "================ RECORD $scene → $out ================"
  clean_display
  KLANGK_DEMO_FONT_SIZE=28 KLANGK_DEMO_OUTPUT="$out" \
    devenv shell -- src/frontend/e2e-tests/demo/record-terminal.sh \
    python3 src/frontend/e2e-tests/demo/cli_demo.py --scene "$scene"
}

prep_2() {
  ensure_logged_in
  rm_ws demo
  ensure_logged_out
}
prep_3() {
  ensure_logged_in
  rm_ws openclaw
}
prep_3b() {
  ensure_logged_in
  ensure_healthy openclaw
}

declare -a RESULTS=()
do_scene() { # $1=label $2=prep_fn $3=cli_scene $4=filename
  local label=$1 prep=$2 cli=$3 fn=$4 rc
  echo "---------------- prep: Scene $label ----------------"
  $prep
  record "$cli" "$fn"
  rc=$?
  RESULTS+=("Scene $label ($fn): driver rc=$rc")
  return $rc
}

case "$SCENE" in
2) do_scene 2 prep_2 scene_2 scene-02-cli.mp4 ;;
3) do_scene 3 prep_3 scene_3 scene-03-sandbox.mp4 ;;
3b) do_scene 3b prep_3b scene_3b scene-03b-services.mp4 ;;
all)
  do_scene 2 prep_2 scene_2 scene-02-cli.mp4
  do_scene 3 prep_3 scene_3 scene-03-sandbox.mp4
  do_scene 3b prep_3b scene_3b scene-03b-services.mp4
  ;;
*)
  echo "unknown scene: $SCENE (use 2|3|3b|all)"
  exit 2
  ;;
esac

# --- summary ---------------------------------------------------------------
echo
echo "================ SUMMARY ================"
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo
echo "recordings:"
for f in "$RECORDINGS_DIR"/scene-0*.mp4; do
  [ -f "$f" ] || continue
  dur=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$f" 2>/dev/null)
  dim=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0:s=x "$f" 2>/dev/null)
  sz=$(du -h "$f" | cut -f1)
  printf "  %-40s %s  %ss  %s\n" "$f" "$dim" "${dur%.*}" "$sz"
done
