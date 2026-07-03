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
#   Scene 2  prep: seed --reset (full repave → only potemkin remain) + logout
#                 → records an on-camera login + create
#   Scene 3  prep: ensure login + rm openclaw → on-camera sandbox create;
#                 the created container STAYS RUNNING into 3b
#   Scene 3b prep: ensure login + openclaw must be healthy (carried from 3;
#                 NOT removed)
#
# The demo backend is self-contained on a dedicated port pair + instance
# ("video") so it never races the main repo's backend (:8997/:8995). This
# script ensures it's UP before recording (starting it cold ~80s the first
# time, ~19s warm thereafter) but does NOT stop it on exit: Scene 3b needs the
# container created in Scene 3 to still be running, and warm reuse across takes
# avoids the cold-start cost. Stop it manually when fully done:
#   run-demo-backend.sh stop
# KLANGK_ALLOW_AUTOSTART=1 + KLANGK_HEALTH_CHECK_INTERVAL=10 live in .env.
set -uo pipefail

WT=/home/chrism/projects/klangk/.worktrees/demo-video-scripts
cd "$WT" || exit 1
DEMO_DIR="src/frontend/e2e-tests/demo"
RECORDINGS_DIR="$DEMO_DIR/recordings"
mkdir -p "$RECORDINGS_DIR"
# Demo backend's nginx port (run-demo-backend.sh / .env: KLANGK_NGINX_PORT=8996).
SERVER="${KLANGK_DEMO_SERVER:-http://localhost:8996}"
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

# Ensure the dedicated demo backend is up (start it if not). Idempotent.
ensure_backend() {
  echo "  [prep] ensure demo backend up on ${SERVER#http://}"
  bash "$DEMO_DIR/run-demo-backend.sh" start >/dev/null || {
    echo "  [prep] FATAL: demo backend failed to start" >&2
    exit 1
  }
}

ensure_logged_out() {
  echo "  [prep] logout"
  kc logout "$SERVER" >/dev/null 2>&1 || true
}

# Echo the user currently logged in to $SERVER, or "" if not logged in.
# Used by ensure_logged_in to decide whether a logout is safe.
current_user() {
  kc status 2>/dev/null | awk '/^User/ && $2 != "(none)" {print $2}'
}

# Ensure we're logged in as $HERO WITHOUT tearing down containers.
#
# IMPORTANT: `klangkc logout` makes the backend stop the logging-out user's
# containers (api/auth.logout -> session.logout_user -> stop_and_remove).
# So a naive "logout then login" here would kill the openclaw container that
# Scene 3 created and that Scene 3b must inherit ALREADY healthy (to show
# service-command auto-start, not a reconnect re-fire). Instead: if we're
# already logged in as $HERO, do nothing (containers untouched); only log out
# when logged in as a DIFFERENT user (clears a genuinely stale session).
ensure_logged_in() {
  local cur
  cur=$(current_user)
  if [ "$cur" = "$HERO" ]; then
    echo "  [prep] already logged in as $HERO (containers preserved)"
    return 0
  fi
  echo "  [prep] login as $HERO (was: ${cur:-logged out})"
  [ -n "$cur" ] && kc logout "$SERVER" >/dev/null 2>&1 || true
  printf "%s" "$PASS" | devenv shell -- klangkc login --password-file - "$SERVER" "$HERO" 2>&1 | quiet | tail -1
}

rm_ws() {
  echo "  [prep] rm workspace '$1' (if present)"
  kc rm "$1" >/dev/null 2>&1 || true
}

run_seed() {
  echo "  [prep] seed $*"
  devenv shell -- node --experimental-strip-types \
    "$DEMO_DIR/demo-seed.ts" "$@" 2>&1 | quiet
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
  run_seed --reset
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

# The dedicated demo backend must be up before any prep that talks to it.
# Started once here so `all` (2→3→3b) shares one warm backend + one openclaw
# container across all three takes (3b needs 3's container still running).
ensure_backend

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
echo
echo "demo backend still up at $SERVER (left running for warm reuse / 3→3b continuity)."
echo "stop it when done:  bash $DEMO_DIR/run-demo-backend.sh stop"
