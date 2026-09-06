#!/usr/bin/env bash
# fmtk harness: one command to a debug frontend driven against a live
# backend (issue #2881).
#
#   devenv --quiet shell -- fmtk-up
#
# Boots, from the repo root (run devenv from the ROOT — src/frontend has
# its own devenv.lock without flutter):
#
#   1. a scratch klangkd on 127.0.0.1:8998 (own state dir under
#      $DEVENV_STATE/fmtk, password auth, fixed admin creds) — a fresh DB,
#      so it never touches the dev stack's data or ports (8997/8995);
#   2. an origin-splitting caddy on 127.0.0.1:8124: /api/* and /ws go to
#      the backend, everything else to the flutter dev server on 8125 —
#      the frontend is same-origin only, so the debug app must be loaded
#      through this proxy to reach the backend;
#   3. the fixture (scripts/fmtk-seed.py): fmtk-admin owner plus
#      fmtk-collaborator / fmtk-coder / fmtk-spectator role members on
#      the "fmtk-verify" workspace;
#   4. `flutter run --debug -d chrome` on 8125 with CHROME_EXECUTABLE set
#      to scripts/fmtk-chrome.sh, which rewrites the opened URL to the
#      proxy origin (8124).
#
# Then prints the Dart VM Service ws:// URI plus a ready-to-paste fmtk
# prefix, and waits. Ctrl-C stops the flutter run but KEEPS the backend
# + proxy running (and the seeded state), so the next `fmtk-up` skips
# steps 1-3 and is ready in roughly the flutter compile time alone.
# `fmtk-down` (scripts/fmtk-down.sh) stops the kept services;
# `fmtk-up --fresh` wipes the scratch state first (fresh DB).
#
# Overridable: FMTK_BACKEND_PORT (8998), FMTK_EGRESS_PORT (8996),
# FMTK_PROXY_PORT (8124), FMTK_FLUTTER_PORT (8125), FMTK_STATE.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEVENV_ROOT="${DEVENV_ROOT:-$REPO_ROOT}"
DEVENV_STATE="${DEVENV_STATE:-$REPO_ROOT/.devenv/state}"
BACKEND_PORT="${FMTK_BACKEND_PORT:-8998}"
EGRESS_PORT="${FMTK_EGRESS_PORT:-8996}"
PROXY_PORT="${FMTK_PROXY_PORT:-8124}"
FLUTTER_PORT="${FMTK_FLUTTER_PORT:-8125}"
STATE_DIR="${FMTK_STATE:-$DEVENV_STATE/fmtk}"
VENV_PYTHON="$DEVENV_STATE/venv/bin/python"
START=$SECONDS

log() { printf '\033[1m[fmtk-up +%2ds]\033[0m %s\n' "$((SECONDS - START))" "$*"; }
die() {
  log "ERROR: $*"
  reap_boot_pids
  exit 1
}

if [[ ${1:-} == "--fresh" ]]; then
  "$REPO_ROOT/scripts/fmtk-down.sh" --wipe || true
elif [[ -n ${1:-} ]]; then
  die "unknown flag: $1 (only --fresh)"
fi

# --- preflight ---------------------------------------------------------
for tool in caddy flutter curl; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not on PATH (run inside the devenv shell, from the repo root)"
done
[[ -x $VENV_PYTHON ]] || die "venv python missing at $VENV_PYTHON (enter the devenv shell once first)"
if ss -tln "sport = :$FLUTTER_PORT" | grep -q LISTEN; then
  die "port $FLUTTER_PORT already in use (a previous fmtk-up still running? override with FMTK_FLUTTER_PORT)"
fi
mkdir -p "$STATE_DIR"

# Children we must clean up on exit: only the flutter run. The backend
# and caddy are deliberately KEPT across launches (fast re-launch), so
# they are tracked in BOOT_PIDS only until healthy — die() below reaps
# them if the boot itself fails, but a successful launch leaves them
# running (fmtk-down stops them).
FLUTTER_PID=""
BOOT_PIDS=()
reap_boot_pids() {
  local pid
  for pid in "${BOOT_PIDS[@]:-}"; do
    [[ -n $pid ]] && kill -TERM -- "-$pid" 2>/dev/null
  done
}
cleanup() {
  trap - EXIT INT TERM
  log "stopping the flutter run (backend + proxy stay up for fast re-launch; fmtk-down stops them)"
  [[ -n $FLUTTER_PID ]] && kill -TERM -- "-$FLUTTER_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT INT TERM

# Is OUR scratch backend (the one whose --config lives in $STATE_DIR)
# alive and healthy? Only then may we reuse it — never adopt an
# unrelated listener on the port.
backend_is_ours_and_healthy() {
  pgrep -f "klangk.main --config $STATE_DIR/klangkd.yaml" >/dev/null 2>&1 &&
    curl -sf -o /dev/null "http://127.0.0.1:$BACKEND_PORT/api/v1/config"
}

wait_http() { # wait_http <url> <name> <timeout_s>
  local deadline=$((SECONDS + $3))
  while ((SECONDS < deadline)); do
    curl -sf -o /dev/null "$1" && return 0
    sleep 1
  done
  die "timed out waiting for $2 at $1"
}

# --- 1. scratch backend (reused when ours and healthy) ------------------
if backend_is_ours_and_healthy; then
  log "reusing running scratch klangkd on :$BACKEND_PORT"
else
  for port in "$BACKEND_PORT" "$EGRESS_PORT"; do
    if ss -tln "sport = :$port" | grep -q LISTEN; then
      die "port $port is in use by something else (not our healthy scratch backend) — run fmtk-down, or override FMTK_*_PORT"
    fi
  done
  cat >"$STATE_DIR/klangkd.yaml" <<EOF
port: "$BACKEND_PORT"
listen: "127.0.0.1"
egress_port: "$EGRESS_PORT"
idle_timeout_seconds: "300"
auth_modes: password
jwt_secret: fmtk-scratch-secret
default_user: admin@example.com
default_password: admin123abc
api_rate_limit: "0"  # fmtk drives machine-speed /api bursts from one IP
state_dir: "$STATE_DIR/klangk"
data_dir: "$STATE_DIR/klangk/data"
EOF
  log "starting scratch klangkd on :$BACKEND_PORT (state: $STATE_DIR/klangk)"
  setsid "$VENV_PYTHON" -m klangk.main --config "$STATE_DIR/klangkd.yaml" \
    >"$STATE_DIR/klangkd.log" 2>&1 &
  BOOT_PIDS+=($!)
  wait_http "http://127.0.0.1:$BACKEND_PORT/api/v1/config" "backend" 90
  BOOT_PIDS=()
fi

# --- 2. origin-splitting proxy (reused when healthy) -------------------
if curl -sf -o /dev/null "http://127.0.0.1:$PROXY_PORT/api/v1/config" &&
  pgrep -f "caddy run --config $STATE_DIR/proxy.Caddyfile" >/dev/null 2>&1; then
  log "reusing caddy proxy on :$PROXY_PORT"
else
  if ss -tln "sport = :$PROXY_PORT" | grep -q LISTEN; then
    die "port $PROXY_PORT is in use by something else — run fmtk-down, or override FMTK_PROXY_PORT"
  fi
  cat >"$STATE_DIR/proxy.Caddyfile" <<EOF
http://:$PROXY_PORT {
	bind 127.0.0.1
	handle /api/* {
		reverse_proxy 127.0.0.1:$BACKEND_PORT
	}
	handle /ws {
		reverse_proxy 127.0.0.1:$BACKEND_PORT
	}
	handle /ws/* {
		reverse_proxy 127.0.0.1:$BACKEND_PORT
	}
	handle {
		reverse_proxy 127.0.0.1:$FLUTTER_PORT
	}
}
EOF
  log "starting caddy proxy on :$PROXY_PORT (api+ws -> :$BACKEND_PORT, rest -> :$FLUTTER_PORT)"
  setsid caddy run --config "$STATE_DIR/proxy.Caddyfile" --adapter caddyfile \
    >"$STATE_DIR/caddy.log" 2>&1 &
  BOOT_PIDS+=($!)
  wait_http "http://127.0.0.1:$PROXY_PORT/api/v1/config" "proxy" 30
  BOOT_PIDS=()
fi

# --- 3. fixture (idempotent; a no-op on a kept backend) ----------------
log "seeding fixture (idempotent)"
"$VENV_PYTHON" "$REPO_ROOT/scripts/fmtk-seed.py" --url "http://127.0.0.1:$BACKEND_PORT" ||
  die "seed failed (see above)"

# --- 4. flutter debug run ----------------------------------------------
# --no-pub skips `flutter pub get` when deps are already resolved
# (package_config exists and predates pubspec.lock); the first run in a
# cold checkout resolves normally.
FLUTTER_ARGS=(run --debug -d chrome --web-hostname 127.0.0.1 --web-port "$FLUTTER_PORT")
PKG_CONFIG="$REPO_ROOT/src/frontend/.dart_tool/package_config.json"
if [[ -f $PKG_CONFIG && ! $PKG_CONFIG -nt "$REPO_ROOT/src/frontend/pubspec.lock" ]]; then
  FLUTTER_ARGS+=(--no-pub)
fi
log "starting flutter run --debug -d chrome (first build can take a while)"
cd "$REPO_ROOT/src/frontend" || exit 2
CHROME_EXECUTABLE="$REPO_ROOT/scripts/fmtk-chrome.sh" \
  setsid flutter "${FLUTTER_ARGS[@]}" \
  >"$STATE_DIR/flutter.log" 2>&1 &
FLUTTER_PID=$!

VM_URI=""
deadline=$((SECONDS + 600))
while ((SECONDS < deadline)); do
  VM_URI="$(grep -o 'ws://[^ ]*/ws' "$STATE_DIR/flutter.log" | head -1 || true)"
  [[ -n $VM_URI ]] && break
  kill -0 "$FLUTTER_PID" 2>/dev/null || {
    tail -30 "$STATE_DIR/flutter.log"
    die "flutter run exited before exposing a VM service"
  }
  sleep 2
done
[[ -n $VM_URI ]] || die "no Dart VM Service in flutter.log after 600s"

CDP_PORT="$(pgrep -af 'chrome' | grep -o 'remote-debugging-port=[0-9]*' | cut -d= -f2 | head -1 || true)"

cat <<EOF

=====================================================================
 fmtk harness ready (#2881)

   VM service:    $VM_URI
   app (chrome):  http://127.0.0.1:$PROXY_PORT/#/
   backend:       http://127.0.0.1:$BACKEND_PORT   (admin@example.com / admin123abc)
   chrome CDP:    ${CDP_PORT:-unknown — pgrep chrome for remote-debugging-port}

 Fixture logins (password fmtk-Pass123! for all):
   fmtk-admin@example.com          admin group + owner -> everything
   fmtk-collaborator@example.com   collaborators bucket
   fmtk-coder@example.com          coders bucket
   fmtk-spectator@example.com      spectators bucket, NO Sharing tab

 Drive it (from the repo root, another shell):

   URI='$VM_URI'
   devenv --quiet -O dotenv.enable:bool false shell -- fmtk exec \\
     --name semantic_snapshot --vm-service-uri "\$URI" --args '{}'

 Logs: $STATE_DIR/{klangkd,caddy,flutter}.log — Ctrl-C stops the flutter run
 and KEEPS backend+proxy for a fast re-launch; 'fmtk-down' stops them
 ('fmtk-down --wipe' also deletes the scratch state).
=====================================================================

EOF
wait "$FLUTTER_PID"
