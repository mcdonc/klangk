#!/usr/bin/env bash
# Run the dist-smoke Playwright spec against a klangkd started from an
# installed wheel — OUTSIDE any devenv / nix shell (#1611).
#
# The whole point: prove a bare `pip install klangk && klangkd` works on a
# stock Ubuntu runner. caddy is apt-installed (klangkd forks it as a child
# via shutil.which); python3 + venv come from the runner; npm + Playwright
# install their own Chromium. Nothing from devenv. This is the audience the
# #1607 / #1645 first-run story targets — if the wheel doesn't serve a
# working login page in this environment, the release is broken.
#
# The wheel is built separately (release.yml's build-wheel job). This script:
#   1. Installs the wheel into an isolated venv (not editable — catches
#      packaging bugs an editable install would hide).
#   2. Imports klangk.main.build_app as a sanity check — surfaces a
#      ModuleNotFoundError fast if a transitive dep is missing from
#      pyproject.toml (klangkd --help is too shallow; Typer exits before
#      main() runs and build_app is imported lazily inside it).
#   3. Starts the real klangkd entrypoint (caddy engine on UDS upstream +
#      Flutter Web frontend served from the wheel's bundled klangk/frontend/
#      — KLANGKD_FRONTEND_DIR is unset so settings falls back to
#      _DEFAULT_FRONTEND_DIR, NOT inherited from any dev shell).
#   4. Polls /health until caddy + uvicorn are ready.
#   5. Installs the e2e npm deps + Playwright Chromium, then runs
#      `npx playwright test --project=dist-smoke` against that URL.
#   6. Tears klangkd down on exit (trap).
#
# What this catches (before anything publishes):
#   - Frontend not included in wheel → 404 / blank page
#   - _DEFAULT_FRONTEND_DIR resolves wrong post-install → same
#   - Flutter build broken / incomplete → flutter-view never attaches
#   - main.dart.js missing or corrupt → engine doesn't boot
#   - klangkd entrypoint registration broken → server never starts
#   - caddy binary not found → engine refuses to start
#   - Caddyfile render broken → caddy rejects the config
#   - UDS upstream misconfigured → caddy 502s
#   - Missing transitive dep in pyproject.toml → import fails fast
#
# Usage:
#   scripts/dist-smoke-test.sh <path/to/klangk-*.whl>
#
# Requires on PATH (apt-installable on Ubuntu): caddy, curl, python3,
# python3-venv, npm, node. release.yml's dist-smoke-test job apt-installs
# the first set; npm/node come from the GitHub Actions runner image.
#
# Running locally without cutting a release: build the wheel from your
# current branch, then run the script directly (the script itself makes
# zero release.yml assumptions). On a stock NixOS/devenv host caddy is
# already on PATH:
#   devenv shell -- bash scripts/flutterbuildweb.sh   # produce the frontend
#   devenv shell -- bash scripts/build_wheel.sh       # produce the wheel
#   bash scripts/dist-smoke-test.sh src/klangk/dist/klangk-*.whl
# Or via CI without tagging: `gh workflow run dist-smoke.yml --ref <branch>`
# (.github/workflows/dist-smoke.yml is the standalone smoke workflow —
# build-wheel + dist-smoke-test, no publish. release.yml no longer runs
# the smoke; it publishes without waiting on it.)
set -euo pipefail

PORT="${KLANGKD_PORT:-18997}"
EGRESS_PORT="${KLANGKD_EGRESS_PORT:-18995}"
VENV_DIR="${SMOKE_VENV:-/tmp/klangk-smoke-venv}"
DATA_DIR="${KLANGKD_DATA_DIR:-/tmp/klangk-smoke-data}"
STATE_DIR="${KLANGKD_STATE_DIR:-/tmp/klangk-smoke-state}"

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

echo "=== dist-smoke-test (no devenv) ==="
echo "  wheel:       $WHEEL"
echo "  port:        $PORT"
echo "  venv:        $VENV_DIR"
echo "  data_dir:    $DATA_DIR"
echo "  state_dir:   $STATE_DIR"
echo "  caddy:       $(command -v caddy || echo '(NOT FOUND — apt install caddy')"
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

# Sanity: klangk imports end-to-end. `klangkd --help` is NOT enough — Typer
# handles --help and exits before main() runs, and build_app is imported
# lazily inside main(), so --help proves only the entry-point registration,
# not that the wheel's deps are complete. Importing build_app directly
# surfaces a ModuleNotFoundError with a real traceback if a transitive dep
# is missing from pyproject.toml. (#1705 review, I4.)
if ! "$VENV_DIR/bin/python" -c 'from klangk.main import build_app' >/dev/null 2>&1; then
  echo "error: klangk imports fail from the installed wheel — missing dep?" >&2
  "$VENV_DIR/bin/python" -c 'from klangk.main import build_app'
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
#
# UNSET KLANGKD_FRONTEND_DIR — if a parent shell (e.g. a dev's devenv shell)
# exports it pointing at src/frontend/build/web, klangkd would serve the dev
# tree instead of the wheel-bundled frontend, defeating the smoke. Empty
# string does NOT work — pydantic-settings accepts the empty string literally
# and Path("").exists() is False, so the UI mount is skipped with a warning.
# `env -u` actually removes the var so settings falls back to
# _DEFAULT_FRONTEND_DIR = <site-packages>/klangk/frontend — the wheel's copy.
# (#1705 review, B1.)
#
# Redirect stdio to $STATE_DIR/server.log so a failure artifact contains
# klangkd's output (a backgrounded process's inherited stdio would otherwise
# land only in the GH Actions step log). Mirrors the e2e harness's
# global-setup.ts openSync(logFd) pattern. (#1705 review, I3.)
LOG_PATH="$STATE_DIR/server.log"
env -u KLANGKD_FRONTEND_DIR \
  KLANGKD_PORT="$PORT" \
  KLANGKD_EGRESS_PORT="$EGRESS_PORT" \
  KLANGKD_DATA_DIR="$DATA_DIR" \
  KLANGKD_STATE_DIR="$STATE_DIR" \
  KLANGKD_AUTH_MODES=password \
  KLANGKD_DEFAULT_USER=admin@example.com \
  KLANGKD_DEFAULT_PASSWORD=admin \
  KLANGKD_JWT_SECRET=smoke-test-secret \
  KLANGKD_TEST_MODE=1 \
  LOGFIRE_TOKEN='' \
  "$VENV_DIR/bin/klangkd" --config=none >"$LOG_PATH" 2>&1 &
KLANGKD_PID=$!
echo "klangkd pid=$KLANGKD_PID, log=$LOG_PATH"

# 3. Poll /health. Bail early if klangkd dies during startup, and dump the
#    log tail so the actual error reaches the step output (not just the
#    uploaded artifact).
echo "=== polling http://127.0.0.1:$PORT/health ==="
READY=0
for i in $(seq 1 120); do
  if ! kill -0 "$KLANGKD_PID" 2>/dev/null; then
    echo "error: klangkd exited during startup (after ${i}s)" >&2
    echo "--- last 50 lines of $LOG_PATH ---" >&2
    tail -n 50 "$LOG_PATH" >&2 2>/dev/null || true
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
  echo "--- last 50 lines of $LOG_PATH ---" >&2
  tail -n 50 "$LOG_PATH" >&2 2>/dev/null || true
  exit 1
fi

# 4. Install the e2e deps + Playwright's own Chromium (no devenv-bundled
#    browser — `npx playwright install chromium` fetches the one pinned by
#    @playwright/test 1.59.1 in package.json). KLANGKBUILD_TEST_URL makes
#    global-setup short-circuit its own server startup (it just polls
#    /health on this URL and returns).
#
#    SKIP_PLAYWRIGHT=1 skips the browser test (useful on distros Playwright
#    doesn't support yet, e.g. Ubuntu 26.04, or in container-only runs).
if [ -n "${SKIP_PLAYWRIGHT:-}" ]; then
  echo "=== playwright smoke skipped (SKIP_PLAYWRIGHT set) ==="
else
  echo "=== installing playwright deps + chromium ==="
  cd "$REPO_ROOT/src/frontend/e2e-tests"
  npm install --silent
  npx playwright install --with-deps chromium

  echo "=== running dist-smoke spec ==="
  KLANGKBUILD_TEST_URL="http://127.0.0.1:$PORT" \
    npx playwright test --project=dist-smoke --reporter=list
fi

# 5. Workspace lifecycle smoke test (optional — requires podman + a
#    workspace image). Exercises the full create → start → exec → stop
#    path using the klangk CLI from the installed wheel. Skipped when
#    podman is absent (e.g. lightweight CI runners that only test the
#    frontend).
#
#    The CLI auto-discovers the co-located klangkd via the UDS socket
#    when KLANGKD_STATE_DIR is set, and auto-logs in when the server is
#    in "none" auth mode. But the running server uses "password" mode
#    (for the Playwright login test above), so we restart with "none"
#    mode for the CLI phase.
KLANGK="$VENV_DIR/bin/klangk"
SKIP_CONTAINER_SMOKE="${SKIP_CONTAINER_SMOKE:-}"
if [ -n "$SKIP_CONTAINER_SMOKE" ]; then
  echo "=== container smoke skipped (SKIP_CONTAINER_SMOKE set) ==="
elif ! command -v podman >/dev/null 2>&1; then
  echo "=== container smoke skipped (podman not on PATH) ==="
else
  WS_IMAGE="${KLANGKD_IMAGE_NAME:-klangk-workspace}"
  if ! podman image exists "localhost/$WS_IMAGE:latest" 2>/dev/null; then
    # Build a minimal workspace image for the smoke test: the real
    # klangk-workspace is heavy (custom Dockerfile + features); the smoke
    # only needs node + a klangk user so `podman exec -u klangk` works.
    echo "=== building minimal smoke workspace image ==="
    SMOKE_DF="$(mktemp)"
    cat >"$SMOKE_DF" <<'DOCKERFILE'
FROM docker.io/library/node:22-slim
RUN useradd -o -u 1000 -m -d /home/klangk -s /bin/bash klangk && \
    apt-get update -qq && apt-get install -y --no-install-recommends \
      bash tmux && rm -rf /var/lib/apt/lists/*
USER klangk
WORKDIR /home/klangk
DOCKERFILE
    if ! podman build -t "localhost/$WS_IMAGE:latest" -f "$SMOKE_DF" .; then
      echo "=== container smoke skipped (failed to build smoke image) ==="
      rm -f "$SMOKE_DF"
      SKIP_CONTAINER_SMOKE=1
    fi
    rm -f "$SMOKE_DF"
  fi
  if [ -n "${SKIP_CONTAINER_SMOKE:-}" ]; then
    true # skip — message already printed above
  else
    echo "=== restarting klangkd in none-auth mode for CLI smoke ==="
    # Stop the password-mode server
    kill "$KLANGKD_PID" 2>/dev/null || true
    wait "$KLANGKD_PID" 2>/dev/null || true
    KLANGKD_PID=""

    # Clear state for a fresh start
    rm -rf "${STATE_DIR:?}"/* "${DATA_DIR:?}"/*

    env -u KLANGKD_FRONTEND_DIR \
      KLANGKD_PORT="$PORT" \
      KLANGKD_EGRESS_PORT="$EGRESS_PORT" \
      KLANGKD_DATA_DIR="$DATA_DIR" \
      KLANGKD_STATE_DIR="$STATE_DIR" \
      KLANGKD_AUTH_MODES=none \
      KLANGKD_DEFAULT_USER=admin@example.com \
      KLANGKD_JWT_SECRET=smoke-test-secret \
      KLANGKD_LLM_MODELS="openai/test:http://localhost:11434/v1:test" \
      LOGFIRE_TOKEN='' \
      "$VENV_DIR/bin/klangkd" --config=none >"$LOG_PATH" 2>&1 &
    KLANGKD_PID=$!
    echo "klangkd restarted pid=$KLANGKD_PID (none-auth + podman)"

    READY=0
    for i in $(seq 1 120); do
      if ! kill -0 "$KLANGKD_PID" 2>/dev/null; then
        echo "error: klangkd exited during startup (after ${i}s)" >&2
        tail -n 50 "$LOG_PATH" >&2 2>/dev/null || true
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
      echo "error: klangkd not ready after 120s" >&2
      tail -n 50 "$LOG_PATH" >&2 2>/dev/null || true
      exit 1
    fi

    # The CLI discovers the server via the UDS socket at
    # $KLANGK_STATE_DIR/klangk.sock (co-located auto-discovery, #1676).
    # Note: the CLI env var is KLANGK_STATE_DIR (no D), while the server
    # uses KLANGKD_STATE_DIR. Both must point to the same directory.
    export KLANGK_STATE_DIR="$STATE_DIR"

    echo "=== CLI: create workspace ==="
    "$KLANGK" create smoke-ws
    echo "=== CLI: list workspaces ==="
    "$KLANGK" ls

    echo "=== CLI: start workspace ==="
    "$KLANGK" start smoke-ws

    # Wait for the container to be running
    echo "=== waiting for container ==="
    CONTAINER_READY=0
    for i in $(seq 1 60); do
      if podman ps --format '{{.Names}}' 2>/dev/null | grep -q klangk; then
        echo "container running after ${i}s"
        CONTAINER_READY=1
        break
      fi
      sleep 1
    done
    if [ "$CONTAINER_READY" -ne 1 ]; then
      echo "error: workspace container not running after 60s" >&2
      podman ps -a >&2 2>/dev/null || true
      tail -n 50 "$LOG_PATH" >&2 2>/dev/null || true
      exit 1
    fi

    echo "=== exec command in workspace container ==="
    # Use podman exec directly rather than `klangk exec` — the CLI exec
    # path involves a WebSocket connection with auth token negotiation
    # that is exercised by the Playwright e2e suite. Here we just verify
    # the container is functional and can run commands.
    CONTAINER_NAME=$(podman ps --format '{{.Names}}' 2>/dev/null | grep klangk | head -1)
    EXEC_OUTPUT=$(podman exec "$CONTAINER_NAME" echo "hello from workspace")
    echo "  exec output: $EXEC_OUTPUT"
    if echo "$EXEC_OUTPUT" | grep -q "hello from workspace"; then
      echo "  exec: PASS"
    else
      echo "error: exec did not return expected output" >&2
      exit 1
    fi

    echo "=== CLI: stop workspace ==="
    "$KLANGK" stop smoke-ws

    # Verify container stopped
    sleep 2
    if podman ps --format '{{.Names}}' 2>/dev/null | grep -q klangk; then
      echo "error: container still running after stop" >&2
      exit 1
    fi
    echo "  container stopped: PASS"

    echo "=== container smoke: ALL PASSED ==="
  fi # SKIP_CONTAINER_SMOKE
fi   # podman / SKIP_CONTAINER_SMOKE

# trap handles klangkd teardown.
