#!/usr/bin/env bash
# Run the dist-smoke Playwright spec against a klangkd started from an
# installed wheel (#1611).
#
# The wheel is built separately (release.yml's build-wheel job). This script:
#   1. Installs the wheel into an isolated venv (not editable — catches
#      packaging bugs an editable install would hide).
#   2. Starts the real klangkd entrypoint (uvicorn on UDS + nginx child)
#      with KLANGK_PORT=18997 so nginx renders the full browser listener.
#   3. Polls /health on http://127.0.0.1:18997 until nginx + uvicorn are
#      ready.
#   4. Runs `npx playwright test --project=dist-smoke` against that URL.
#   5. Tears klangkd down on exit (trap).
#
# What this catches (before images are pushed + the GitHub release is cut):
#   - Frontend not included in wheel → 404 / blank page
#   - _DEFAULT_FRONTEND_DIR resolves wrong post-install → same
#   - Flutter build broken / incomplete → flutter-view never attaches
#   - main.dart.js missing or corrupt → engine doesn't boot
#   - klangkd entrypoint registration broken → server never starts
#   - nginx not found or config render broken → nginx refuses to start
#   - UDS proxy_pass misconfigured → nginx 502s
#   - location / missing in full template → static files not served
#
# Usage:
#   scripts/dist-smoke-test.sh <path/to/klangk-*.whl>
#
# Requires: nginx on PATH (klangkd renders the config + forks nginx as a
# child). In CI this comes from devenv; locally, run inside `devenv shell`.
set -euo pipefail

PORT="${KLANGK_PORT:-18997}"
EGRESS_PORT="${KLANGK_EGRESS_PORT:-18995}"
VENV_DIR="${SMOKE_VENV:-/tmp/klangk-smoke-venv}"
DATA_DIR="${KLANGK_DATA_DIR:-/tmp/klangk-smoke-data}"
STATE_DIR="${KLANGK_STATE_DIR:-/tmp/klangk-smoke-state}"

WHEEL="${1:-}"
if [ -z "$WHEEL" ]; then
  echo "usage: $0 <path/to/klangk-*.whl>" >&2
  exit 2
fi
if [ ! -f "$WHEEL" ]; then
  echo "error: wheel not found at $WHEEL" >&2
  exit 2
fi

# Locate the repo root from the script's location (the wheel path may be
# relative to the caller's CWD, which we resolve before cd-ing anywhere).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
WHEEL="$(cd "$(dirname "$WHEEL")" && pwd)/$(basename "$WHEEL")"

echo "=== dist-smoke-test ==="
echo "  wheel:       $WHEEL"
echo "  port:        $PORT"
echo "  venv:        $VENV_DIR"
echo "  data_dir:    $DATA_DIR"
echo "  state_dir:   $STATE_DIR"
echo

# 1. Fresh isolated venv + install the wheel (with deps — this is the real
#    "pip install klangk" exercise; if a transitive dep is missing from
#    pyproject.toml the install fails here, not at first import).
echo "=== creating isolated venv ==="
rm -rf "$VENV_DIR" "$DATA_DIR" "$STATE_DIR"
mkdir -p "$DATA_DIR" "$STATE_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
echo "=== installing wheel into venv (this is the 'pip install klangk' test) ==="
"$VENV_DIR/bin/pip" install --quiet "$WHEEL"

# Sanity: the entry point shipped in the wheel is on the venv's PATH.
if ! "$VENV_DIR/bin/klangkd" --help >/dev/null 2>&1; then
  echo "error: klangkd entry point missing or broken in the wheel" >&2
  exit 1
fi

# 2. Start klangkd from the venv. trap ensures cleanup on any exit path.
KLANGKD_PID=""
cleanup() {
  if [ -n "$KLANGKD_PID" ] && kill -0 "$KLANGKD_PID" 2>/dev/null; then
    echo "=== shutting down klangkd (pid $KLANGKD_PID) ==="
    kill "$KLANGKD_PID" 2>/dev/null || true
    wait "$KLANGKD_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "=== starting klangkd from installed wheel ==="
# Min env: password auth (so the login form is the rendered route), test
# mode (skips the workspace-image presence check), no logfire, no banner.
# --config=none opts out of the config file (post-#1607 / #1645 first-run
# generation) so the server runs from env + defaults alone.
KLANGK_PORT="$PORT" \
  KLANGK_EGRESS_PORT="$EGRESS_PORT" \
  KLANGK_DATA_DIR="$DATA_DIR" \
  KLANGK_STATE_DIR="$STATE_DIR" \
  KLANGK_AUTH_MODES=password \
  KLANGK_DEFAULT_USER=admin@example.com \
  KLANGK_DEFAULT_PASSWORD=admin \
  KLANGK_JWT_SECRET=smoke-test-secret \
  KLANGK_TEST_MODE=1 \
  LOGFIRE_TOKEN='' \
  "$VENV_DIR/bin/klangkd" --config=none &
KLANGKD_PID=$!

# 3. Poll /health. Bail early if klangkd dies during startup.
echo "=== polling http://127.0.0.1:$PORT/health ==="
READY=0
for i in $(seq 1 120); do
  if ! kill -0 "$KLANGKD_PID" 2>/dev/null; then
    echo "error: klangkd exited during startup (after ${i}s)" >&2
    exit 1
  fi
  if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "klangkd ready after ${i}s"
    READY=1
    break
  fi
  sleep 1
done
if [ "$READY" -ne 1 ]; then
  echo "error: klangkd not ready at http://127.0.0.1:$PORT/health after 120s" >&2
  exit 1
fi

# 4. Install the e2e deps + run the smoke spec. KLANGK_TEST_URL makes
#    global-setup short-circuit its own server startup (it just polls
#    /health on this URL and returns).
echo "=== installing playwright deps ==="
cd "$REPO_ROOT/src/frontend/e2e-tests"
npm install --silent

echo "=== running dist-smoke spec ==="
KLANGK_TEST_URL="http://127.0.0.1:$PORT" \
  npx playwright test --project=dist-smoke --reporter=list

# trap handles klangkd teardown.
