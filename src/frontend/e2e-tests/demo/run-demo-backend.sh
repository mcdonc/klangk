#!/usr/bin/env bash
# Start a SELF-CONTAINED klangk backend for demo video recording.
#
# The main repo may already be running a backend (INSTANCE_ID "default" on
# :8997/:8995). `devenv processes up` here would race it for those ports, and
# devenv's native manager daemonizes — `devenv processes down` rarely stops it
# and its orphaned children keep holding the ports. Killing the main repo's
# backend isn't an option either.
#
# So this script runs the demo backend ISOLATED on a dedicated port pair +
# instance (set in .env, the only thing that wins over devenv.nix's env):
#
#   backend (uvicorn):     :$KLANGK_PORT        (.env -> 8998)
#   nginx (klangkc target)::$KLANGK_NGINX_PORT  (.env -> 8996)
#   instance id:           "video"   (unique pid file + container labels)
#
# Teardown is bulletproof and does NOT rely on `devenv processes down`:
#   1. kill -9 the native manager supervising the video instance (FIRST, so it
#      can't respawn its children);
#   2. kill -9 every process whose env carries KLANGK_INSTANCE_ID=video (the
#      backend + nginx it spawned, even if reparented to systemd as orphans);
#   3. kill -9 whatever still holds :8998/:8996 (final safety net).
#
# Usage:
#   run-demo-backend.sh            start; block until Ctrl+C (trap tears down)
#   run-demo-backend.sh start      start in background; print URL, exit
#   run-demo-backend.sh stop       tear down (idempotent)
#   run-demo-backend.sh status     exit 0 if up, 1 if down
#
# record-cli.sh + demo-seed.ts point at http://localhost:${KLANGK_NGINX_PORT}.
set -uo pipefail

WT=/home/chrism/projects/klangk/.worktrees/demo-video-scripts
cd "$WT" || exit 1

# These come from .env (devenv loads it; .env's mkDefault beats devenv.nix's
# mkOverride 1500, which a plain export does NOT). Read them back so the script
# can report/target the right ports.
DEMO_PORT="${KLANGK_PORT:-8998}"
DEMO_NGINX_PORT="${KLANGK_NGINX_PORT:-8996}"
DEMO_INSTANCE=video
URL="http://localhost:${DEMO_NGINX_PORT}"
RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# ---------------------------------------------------------------------------
# .env bootstrap
# ---------------------------------------------------------------------------
# .env is gitignored (it holds secrets), so a fresh clone won't have the demo
# backend's dedicated port pair + instance, and `devenv processes up` would
# come up on the DEFAULT ports (:8997/:8995), racing the main repo's backend.
# We maintain a clearly-marked managed block at the END of .env (never touch
# existing lines/secrets). Idempotent: re-running replaces only our block.
_ENV_BLOCK_BEGIN="# --- run-demo-backend.sh managed block (do not edit by hand) ---"
_ENV_BLOCK_END="# --- end run-demo-backend.sh managed block ---"
_ensure_env() {
  # Already configured (and nothing changed)? Skip the rewrite.
  if grep -qF "KLANGK_INSTANCE_ID=$DEMO_INSTANCE" .env 2>/dev/null &&
    grep -qF "KLANGK_PORT=$DEMO_PORT" .env 2>/dev/null &&
    grep -qF "KLANGK_NGINX_PORT=$DEMO_NGINX_PORT" .env 2>/dev/null &&
    grep -qF "KLANGK_HOSTING_HOSTNAME=localhost:$DEMO_NGINX_PORT" .env 2>/dev/null &&
    grep -qF "KLANGK_AUTH_MODES=password" .env 2>/dev/null; then
    return 0
  fi
  echo "  configuring demo ports in .env (instance=$DEMO_INSTANCE, backend=$DEMO_PORT, nginx=$DEMO_NGINX_PORT)"
  # Drop any prior managed block, then append a fresh one.
  if [ -f .env ]; then
    sed -i "/${_ENV_BLOCK_BEGIN}/,/${_ENV_BLOCK_END}/d" .env
  fi
  {
    echo ""
    echo "$_ENV_BLOCK_BEGIN"
    echo "# Dedicated demo backend port pair + instance. .env (mkDefault 1000)"
    echo "# overrides devenv.nix's mkOverride 1500; a parent export does NOT, so"
    echo "# these MUST live in .env. Managed by run-demo-backend.sh — re-runnable."
    echo "KLANGK_INSTANCE_ID=$DEMO_INSTANCE"
    echo "KLANGK_PORT=$DEMO_PORT"
    echo "KLANGK_NGINX_PORT=$DEMO_NGINX_PORT"
    # The demo exercises the password auth flow (login, register, lockout),
    # but the production default for KLANGK_AUTH_MODES (unset, no OIDC) is
    # `none` (#1374), which disables password login/registration. Pin the
    # demo backend to `password` so the default change doesn't break it.
    echo "KLANGK_AUTH_MODES=password"
    # Post-#1241: derive_hosting_info treats the env var as the
    # authoritative override, so the eager-start path (no live request)
    # builds hosted URLs that resolve through nginx on the public port.
    # Carries host[:port]; the demo's public origin is the nginx port.
    echo "KLANGK_HOSTING_HOSTNAME=localhost:$DEMO_NGINX_PORT"
    echo "$_ENV_BLOCK_END"
  } >>.env
}

# ---------------------------------------------------------------------------
# proc discovery helpers
# ---------------------------------------------------------------------------
# Demo procs are identified by the worktree path ($WT) appearing in their
# command line: the backend runs from the worktree's venv, and nginx's master
# is started with `-c <worktree>/.devenv/state/nginx/nginx.conf`. This reliably
# distinguishes them from the main repo's backend/nginx and the system nginx.
# (We can't use KLANGK_INSTANCE_ID: nginx wipes its env on startup.)
_cmdline_has_wt() {
  local p="$1"
  [ -r "/proc/$p/cmdline" ] || return 1
  tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null | grep -qF "$WT"
}

# Echo the live native-managers supervising devenv processes in this session.
_live_managers() {
  local pf m
  for pf in "$RUNTIME"/devenv-*/processes/native-manager.pid; do
    [ -f "$pf" ] || continue
    m=$(cat "$pf" 2>/dev/null || true)
    [ -n "$m" ] && kill -0 "$m" 2>/dev/null && echo "$m"
  done
}

# All PIDs belonging to the demo backend/nginx. The backend + nginx MASTER are
# matched by the worktree path in cmdline; nginx WORKERS show only
# "nginx: worker process" (no path, no env) so they are pulled in as the
# children of any demo nginx master BEFORE the master is killed.
_demo_procs() {
  local p kids
  for p in $(pgrep -f "klangk_backend.main|nginx" 2>/dev/null || true); do
    if _cmdline_has_wt "$p"; then
      echo "$p"
      kids=$(pgrep -P "$p" 2>/dev/null || true)
      [ -n "$kids" ] && echo "$kids"
    fi
  done
}

# Echo the pid of the live native manager supervising the demo backend, or "".
# Walk UP the parent chain from each demo proc until we reach a PID that is a
# live native-manager (or PID 1). Reliable even when the backend is orphaned to
# systemd (the walk finds no manager -> no respawn risk).
_find_video_manager() {
  local mgrs m vp p pp
  mgrs=$(_live_managers)
  [ -z "$mgrs" ] && return
  for vp in $(_demo_procs); do
    p=$vp
    while [ -n "$p" ] && [ "$p" != 1 ]; do
      pp=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
      [ -z "$pp" ] && break
      for m in $mgrs; do [ "$pp" = "$m" ] && echo "$m" && return; done
      p=$pp
    done
  done
}

# ---------------------------------------------------------------------------
# teardown: manager first (stop respawns), then video-instance procs, then
# whatever still holds our ports.
# ---------------------------------------------------------------------------
stop_all() {
  local m p port holders
  # 1. native manager for the demo instance (kill BEFORE its children, or it
  #    will just respawn them).
  m=$(_find_video_manager)
  if [ -n "$m" ]; then
    kill -9 "$m" 2>/dev/null || true
    rm -f "$RUNTIME"/devenv-*/processes/native-manager.pid 2>/dev/null || true
  fi
  # 2. every demo proc (backend, nginx master, AND nginx workers — collected
  #    here while still parented, before any kill in this step).
  for p in $(_demo_procs | sort -u); do
    kill -9 "$p" 2>/dev/null || true
  done
  # 3. final safety net: whatever still holds our dedicated ports (catches any
  #    orphaned nginx worker that slipped step 2).
  sleep 0.3
  for port in "$DEMO_PORT" "$DEMO_NGINX_PORT"; do
    holders=$(ss -tlnpH "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u)
    for p in $holders; do kill -9 "$p" 2>/dev/null || true; done
  done
}

is_up() { ss -tln 2>/dev/null | grep -q ":${DEMO_PORT} "; }

# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
case "${1:-run}" in
stop)
  echo "stopping demo backend (video instance)..."
  stop_all
  echo "stopped."
  ;;

status)
  if is_up; then
    echo "up — $URL"
    exit 0
  fi
  echo "down"
  exit 1
  ;;

start)
  if is_up; then
    echo "$URL (already up)"
    exit 0
  fi
  # Ensure .env has our dedicated ports (self-bootstrap; idempotent).
  _ensure_env
  # Clear any stale instance on our ports before launching.
  stop_all

  echo "starting demo backend (video) on $URL ..."
  # `devenv processes up` runs the full task chain (build-workspace-image,
  # flutter-build, etc.) then backend + nginx, all supervised by its native
  # manager. --detach daemonizes; we tear down via stop_all (above), NOT via
  # the unreliable `devenv processes down`.
  nohup devenv processes up --detach \
    >/tmp/klangk-video-processes.log 2>&1 &

  # Wait for the backend port to bind (nginx 502s until then).
  for _ in $(seq 1 120); do
    if is_up; then
      echo "$URL"
      exit 0
    fi
    sleep 1
  done
  echo "timeout waiting for backend on :${DEMO_PORT}" >&2
  echo "--- last 25 lines of log ---" >&2
  tail -25 /tmp/klangk-video-processes.log >&2
  stop_all
  exit 1
  ;;

run | "")
  # Default: start (detached), then block on a trap until killed.
  bash "$0" start || exit $?
  trap 'echo; echo "stopping demo backend..."; stop_all' INT TERM EXIT
  echo "demo backend up at $URL — Ctrl+C to stop."
  echo "logs: /tmp/klangk-video-processes.log"
  # Block until the backend dies or we get a signal (trap tears down).
  while is_up; do sleep 2; done
  ;;
esac
