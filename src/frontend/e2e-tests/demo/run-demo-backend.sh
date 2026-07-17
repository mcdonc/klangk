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
# instance (set in .demo-env, the only thing that wins over devenv.nix's env):
#
#   backend (uvicorn):     127.0.0.1:$KLANGK_PORT  (.demo-env -> 8998; TCP because
#                                                  KLANGK_LISTEN=127.0.0.1)
#   nginx (klangk target)::$KLANGK_EGRESS_PORT     (.demo-env -> 8996)
#   instance id:           "video"   (unique pid file + container labels)
#
# KLANGK_LISTEN=127.0.0.1 is load-bearing: post-#1400 the default listen is
# None → klangkd binds a UDS and renders the headless (no-browser) template.
# The demo needs the browser UI, so we force TCP loopback → nginx renders the
# full browser template and the web-UI scenes can drive it.
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
# record-cli.sh + demo-seed.ts point at http://localhost:${KLANGK_EGRESS_PORT}.
set -uo pipefail

# Resolve the worktree root from this script's location (it lives at
# <worktree>/src/frontend/e2e-tests/demo/), so the launcher works from any
# worktree — not a hardcoded path that drifts when the worktree is renamed.
WT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)" || {
  echo "FATAL: not inside a git worktree" >&2
  exit 1
}
cd "$WT" || exit 1

# These come from .demo-env (sourced at launch; .demo-env's values beat
# devenv.nix's env. block). Read them back so the script can report/target the
# right ports.
DEMO_PORT="${KLANGK_PORT:-8998}"
DEMO_NGINX_PORT="${KLANGK_EGRESS_PORT:-8996}"
DEMO_LISTEN="${KLANGK_LISTEN:-127.0.0.1}"
# `both` = password + OIDC. The login screen shows the OIDC button above the
# password fields, which the demo's login-card click coordinates assume (see
# demo-helpers.ts demoLogin). Override with KLANGK_AUTH_MODES if you want a
# different mode for a one-off run.
DEMO_AUTH_MODES="${KLANGK_AUTH_MODES:-both}"
# Short, stable state dir under /tmp. The worktree-relative default that
# devenv.nix exports (KLANGK_STATE_DIR=.devenv/state/klangk) is too long for
# a UDS path — klangkd binds <state_dir>/klangk.sock, and AF_UNIX caps
# sun_path at 108 bytes (#1531). A long worktree path (e.g.
# .worktrees/issue-1505-update-intro-video-demo-to-work-against-latest-main
# /klangk.sock) overflows it and the backend crashes on boot. /tmp is short
# and survives across runs (so the demo container images + DB persist).
#
# NOTE: do NOT read KLANGK_STATE_DIR here — devenv.nix exports it as the
# long worktree path, which is exactly the value we're trying to avoid.
# Use KLANGK_DEMO_STATE_DIR to override. This value is exported in the
# `start` command's env (it can't go in .demo-env — .demo-env doesn't
# override devenv.nix's env.KLANGK_STATE_DIR).
DEMO_STATE_DIR="${KLANGK_DEMO_STATE_DIR:-/tmp/klangk-demo}"
DEMO_INSTANCE=video
# Fake OIDC provider config. `both` mode requires at least one provider or
# klangkd refuses to boot; the demo never actually authenticates via OIDC
# (every scene uses password login), so the values are fake — they just need
# to parse so the "Log in with <provider>" button renders.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_OIDC_CONFIG="$_SCRIPT_DIR/demo-oidc.yaml"
# Bootstrap admin = the server's KLANGK_DEFAULT_USER. demo-seed.ts logs in as
# this account to manage users + run the destructive reset. Must match the
# seed's BOOTSTRAP_EMAIL / BOOTSTRAP_PASSWORD defaults (see demo-seed.ts).
DEMO_BOOTSTRAP_EMAIL="${KLANGK_DEFAULT_USER:-admin@plope.com}"
DEMO_BOOTSTRAP_PASSWORD="${KLANGK_DEFAULT_PASSWORD:-admin}"
# LLM provider the live-agent scenes (pi -p in scene 2, clanker in 6/8) call
# via the /llm-proxy. Override with KLANGK_DEMO_LLM_* if you want a different
# provider for the demo. Defaults to z.ai (glm-5.2); the API key uses the
# cmd: indirection so the secret is read at boot, not stored in .demo-env.
DEMO_LLM_BASE_URL="${KLANGK_DEMO_LLM_BASE_URL:-https://api.z.ai/api/coding/paas/v4}"
DEMO_LLM_API_KEY="${KLANGK_DEMO_LLM_API_KEY:-cmd:cat /run/agenix/zai-authtoken-chrism2}"
DEMO_LLM_MODEL="${KLANGK_DEMO_LLM_MODEL:-glm-5.2}"
URL="http://localhost:${DEMO_NGINX_PORT}"

# ---------------------------------------------------------------------------
# .demo-env bootstrap
# ---------------------------------------------------------------------------
# .demo-env is gitignored (it holds secrets), so a fresh clone won't have the
# demo backend's dedicated port pair + instance, and `devenv processes up`
# would come up on the DEFAULT ports (:8997/:8995), racing the main repo's
# backend. We maintain a clearly-marked managed block at the END of .demo-env
# (never touch existing lines/secrets). Idempotent: re-running replaces only
# our block.
_ENV_BLOCK_BEGIN="# --- run-demo-backend.sh managed block (do not edit by hand) ---"
_ENV_BLOCK_END="# --- end run-demo-backend.sh managed block ---"
_ensure_env() {
  # Already configured (and nothing changed)? Skip the rewrite.
  if grep -qF "KLANGK_INSTANCE_ID=$DEMO_INSTANCE" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_PORT=$DEMO_PORT" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_EGRESS_PORT=$DEMO_NGINX_PORT" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_LISTEN=$DEMO_LISTEN" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_STATE_DIR=$DEMO_STATE_DIR" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_OIDC_CONFIG=$DEMO_OIDC_CONFIG" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_DEFAULT_USER=$DEMO_BOOTSTRAP_EMAIL" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_LLM_BASE_URL='$DEMO_LLM_BASE_URL'" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_LLM_MODEL='$DEMO_LLM_MODEL'" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_HOSTING_HOSTNAME=localhost:$DEMO_NGINX_PORT" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_AUTH_MODES=$DEMO_AUTH_MODES" .demo-env 2>/dev/null &&
    grep -qF "KLANGK_ALLOW_AUTOSTART=1" .demo-env 2>/dev/null; then
    return 0
  fi
  echo "  configuring demo ports in .demo-env (instance=$DEMO_INSTANCE, backend=$DEMO_PORT, nginx=$DEMO_NGINX_PORT)"
  # Create the short state dir so klangkd can place its UDS + rendered
  # nginx.conf there. /tmp/klangk-demo survives across runs for warm reuse;
  # demo-seed.ts --reset wipes the DB + users, not the dir.
  mkdir -p "$DEMO_STATE_DIR"
  # Drop any prior managed block, then append a fresh one.
  if [ -f .demo-env ]; then
    sed -i "/${_ENV_BLOCK_BEGIN}/,/${_ENV_BLOCK_END}/d" .demo-env
  fi
  {
    echo ""
    echo "$_ENV_BLOCK_BEGIN"
    echo "# Demo backend config. devenv.nix does NOT enable dotenv, so .demo-env"
    echo "# is not auto-loaded — the 'start' command sources it inside the devenv"
    echo "# shell so these values win over devenv.nix's env. block. Managed by"
    echo "# run-demo-backend.sh — re-runnable."
    echo "KLANGK_INSTANCE_ID=$DEMO_INSTANCE"
    echo "KLANGK_PORT=$DEMO_PORT"
    echo "KLANGK_EGRESS_PORT=$DEMO_NGINX_PORT"
    # Bootstrap admin (the server's KLANGK_DEFAULT_USER). demo-seed.ts logs in
    # as this account to create the hero + cast users. klangkd creates it at
    # startup with KLANGK_DEFAULT_PASSWORD. Must match the seed's
    # BOOTSTRAP_EMAIL/BOOTSTRAP_PASSWORD defaults.
    echo "KLANGK_DEFAULT_USER=$DEMO_BOOTSTRAP_EMAIL"
    echo "KLANGK_DEFAULT_PASSWORD=$DEMO_BOOTSTRAP_PASSWORD"
    # LLM provider for the live-agent scenes (pi -p, clanker). The API key
    # uses cmd: indirection so klangkd resolves the secret at boot — it's
    # not stored literally in .demo-env. Override via KLANGK_DEMO_LLM_* if needed.
    # Values are single-quoted: .demo-env is `source`d by bash, and unquoted
    # `cmd:cat /path` would be parsed as `VAR=cmd:cat` + run `/path` (the
    # shell's per-command env-assignment syntax), leaving the var unset.
    echo "KLANGK_LLM_BASE_URL='$DEMO_LLM_BASE_URL'"
    echo "KLANGK_LLM_API_KEY='$DEMO_LLM_API_KEY'"
    echo "KLANGK_LLM_MODEL='$DEMO_LLM_MODEL'"
    # Force a TCP loopback bind so nginx renders the full (browser) template.
    # The post-#1400 default is a UDS → headless (no browser); the demo needs
    # the browser UI, so this override is load-bearing.
    echo "KLANGK_LISTEN=$DEMO_LISTEN"
    # Short state dir under /tmp: klangkd binds <state_dir>/klangk.sock and
    # AF_UNIX caps sun_path at 108 bytes (#1531). The worktree-relative path
    # that devenv.nix sets is too long for a deep worktree. .demo-env is sourced
    # inside the devenv shell (see the `start` command) so this value wins
    # over devenv.nix's env.KLANGK_STATE_DIR. /tmp survives across runs.
    echo "KLANGK_STATE_DIR=$DEMO_STATE_DIR"
    # The demo exercises both the password auth flow (login, register,
    # lockout) AND the OIDC button on the login screen (the "Log in with
    # <provider>" surface the web-UI scenes show). Pin to `both` so the
    # password fields AND the OIDC button are present — the production
    # default of `none` (#1374) disables both. Also: the login-card click
    # coordinates in demo-helpers.ts were measured for `both` mode (the
    # OIDC button shifts the fields down), so they need `both` to land.
    echo "KLANGK_AUTH_MODES=$DEMO_AUTH_MODES"
    # Point at the fake OIDC provider config (see demo-oidc.yaml) so `both`
    # mode boots and the login button renders. The issuer is never contacted.
    echo "KLANGK_OIDC_CONFIG=$DEMO_OIDC_CONFIG"
    # Post-#1241: derive_hosting_info treats the env var as the
    # authoritative override, so the eager-start path (no live request)
    # builds hosted URLs that resolve through nginx on the public port.
    # Carries host[:port]; the demo's public origin is the nginx port.
    echo "KLANGK_HOSTING_HOSTNAME=localhost:$DEMO_NGINX_PORT"
    # Scene 3 (sandbox) needs auto-start so the workspace container boots
    # automatically when the sandbox config requests it.
    echo "KLANGK_ALLOW_AUTOSTART=1"
    echo "$_ENV_BLOCK_END"
  } >>.demo-env
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

# All PIDs belonging to the demo backend/nginx. klangkd runs from this
# worktree's venv (so the worktree path is in its cmdline); nginx's master
# is started with `-c <state_dir>/nginx.conf` (so the state_dir is in its
# cmdline). nginx WORKERS show only "nginx: worker process" (no path, no
# env) so they are pulled in as the children of any demo nginx master
# BEFORE the master is killed. (We can't use KLANGK_INSTANCE_ID: nginx
# wipes its env on startup.)
_cmdline_has_state_dir() {
  local p="$1"
  [ -r "/proc/$p/cmdline" ] || return 1
  tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null | grep -qF "$DEMO_STATE_DIR"
}

_demo_procs() {
  local p kids
  for p in $(pgrep -f "klangkd.launcher|klangkd.main|nginx" 2>/dev/null || true); do
    if _cmdline_has_wt "$p" || _cmdline_has_state_dir "$p"; then
      echo "$p"
      kids=$(pgrep -P "$p" 2>/dev/null || true)
      [ -n "$kids" ] && echo "$kids"
    fi
  done
}

# ---------------------------------------------------------------------------
# teardown: kill the demo procs, then whatever still holds our ports.
# (No devenv process manager to fight — klangkd is launched directly.)
# ---------------------------------------------------------------------------
stop_all() {
  local p port holders
  # 1. every demo proc (klangkd, nginx master, AND nginx workers — collected
  #    here while still parented, before any kill in this step).
  for p in $(_demo_procs | sort -u); do
    kill -9 "$p" 2>/dev/null || true
  done
  # 2. final safety net: whatever still holds our dedicated ports (catches any
  #    orphaned nginx worker that slipped step 1).
  sleep 0.3
  for port in "$DEMO_PORT" "$DEMO_NGINX_PORT"; do
    holders=$(ss -tlnpH "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | sort -u)
    for p in $holders; do kill -9 "$p" 2>/dev/null || true; done
  done
}

is_up() { ss -tln 2>/dev/null | grep -q ":${DEMO_NGINX_PORT} "; }

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
  # Ensure .demo-env has our dedicated ports (self-bootstrap; idempotent).
  _ensure_env
  # Clear any stale instance on our ports before launching.
  stop_all

  echo "starting demo backend (video) on $URL ..."
  # Launch klangkd DIRECTLY (not via `devenv processes up`). The process
  # manager spawns its children in a freshly nix-evaluated environment that
  # ignores the current shell's exports, so sourcing .demo-env around it has no
  # effect — klangkd would still see devenv.nix's KLANGK_STATE_DIR (the long
  # worktree path, which overflows AF_UNIX's 108-byte sun_path, #1531) and
  # the default ports (racing the main repo's backend on :8997/:8995).
  #
  # Source .demo-env INSIDE the devenv shell (after devenv's env setup) so
  # .demo-env's values win, then exec klangkd with --config=none (all config
  # from env). `set -a` makes every assignment in .demo-env an export. nohup + & detaches so
  # the script can return after the port comes up; teardown is stop_all.
  #
  # The task chain (build-workspace-image, flutter-build) is NOT needed here
  # — the workspace image is already built (the main repo's devenv builds it
  # into the shared podman store) and the demo doesn't serve the Flutter UI
  # from this worktree's build (nginx points elsewhere). klangkd + the image
  # is all the demo needs.
  nohup devenv --quiet shell -- bash -c '
    set -a; . ./.demo-env; set +a
    exec python3 -m klangk.launcher --config=none
  ' >/tmp/klangk-video-processes.log 2>&1 &

  # Wait for nginx to bind (uvicorn binds a UDS, not TCP, so :$DEMO_PORT is
  # never listened on; nginx on :$DEMO_NGINX_PORT is the readiness signal).
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
