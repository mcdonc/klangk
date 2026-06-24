#!/usr/bin/env bash
# Fast frontend iteration: run the Flutter web app through the DDC dev compiler
# (`flutter run -d web-server`) instead of the full `flutter build web --release`
# path (see scripts/flutterbuildweb.sh). DDC compiles incrementally, so a Dart
# edit recompiles in seconds via hot restart (R) / hot reload (r) rather than a
# full AOT release rebuild.
#
# This is the OPT-IN dev path. It does NOT replace flutterbuildweb.sh:
#   - Default `devenv processes up` still does the release build.
#   - `KLANGK_WEB_DEV=1 devenv processes up` makes nginx route `/` (+ assets +
#     DWDS) to THIS dev server, while `/api/` and `/ws` still go to the backend.
#     Because the frontend derives every API/WS URL from `Uri.base` (the nginx
#     origin, :8995), the pi / browser-delegate bridge is unaffected.
#
# Usage (typical two-terminal flow):
#   term1$  KLANGK_WEB_DEV=1 devenv processes up --no-tui     # backend + nginx(dev)
#   term2$  devenv shell -- flutterdevweb                     # this script; press R to reload
#
# Env:
#   KLANGK_WEB_DEV_PORT   dev-server port nginx proxies to (default 8996)
#   KLANGK_WEB_FLUTTER    flutter binary (default: `flutter` on PATH / nix)
#   KLANGK_WEB_HOT_RELOAD if "1", enable experimental web hot reload (stateful);
#                         otherwise rely on hot restart (R), which is reliable.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

# Same plugin bootstrap as the release build: fetch plugins on first run and
# rewrite the pubspec plugin path so the app compiles against the current set.
if [ -f "${KLANGK_PLUGINS_DIR:-}/plugins.yaml" ] && [ ! -f "${KLANGK_PLUGINS_DIR:-}/plugins.lock" ]; then
  echo "No plugins.lock found, running update-plugins..."
  python3 scripts/update_plugins.py
fi
python3 scripts/import_dart_plugins.py

FLUTTER="${KLANGK_WEB_FLUTTER:-flutter}"
DEV_PORT="${KLANGK_WEB_DEV_PORT:-8996}"

cd src/frontend
"$FLUTTER" --disable-analytics >/dev/null 2>&1 || true
"$FLUTTER" pub get

HOT_RELOAD_FLAG=()
if [ "${KLANGK_WEB_HOT_RELOAD:-0}" = "1" ]; then
  # Stateful web hot reload is experimental; falls back gracefully if the
  # installed Flutter doesn't accept the flag.
  if "$FLUTTER" run --help 2>/dev/null | grep -q -- '--web-experimental-hot-reload'; then
    HOT_RELOAD_FLAG=(--web-experimental-hot-reload)
    echo "Web hot reload (experimental) enabled."
  else
    echo "This Flutter has no --web-experimental-hot-reload; using hot restart (R)." >&2
  fi
fi

# Extension-free auto-reload (KLANGK_WEB_DEV_RELOAD=1): hand the dev server to
# the supervisor (scripts/flutter_reload_server.py), which owns the `flutter run`
# process, restarts it on save (a plain browser reload does NOT recompile), and
# pushes an SSE reload once it's serving again. nginx injects the EventSource
# client. Works in any browser, no Dart Debug Extension.
if [ "${KLANGK_WEB_DEV_RELOAD:-0}" = "1" ]; then
  export KLANGK_WEB_FLUTTER="$FLUTTER" KLANGK_WEB_DEV_PORT="$DEV_PORT"
  echo "Auto-reload supervisor: edit + save -> dev-server restart -> tab reloads"
  echo "  -> browse the app at nginx :${KLANGK_NGINX_PORT:-8995} (any browser)"
  exec python3 "$SCRIPT_DIR/flutter_reload_server.py"
fi

echo "Starting Flutter dev server on 127.0.0.1:${DEV_PORT}"
echo "  -> with KLANGK_WEB_DEV=1, browse the app at nginx :${KLANGK_NGINX_PORT:-8995}"
echo "  -> press R for hot restart, r for hot reload, q to quit"
exec "$FLUTTER" run -d web-server \
  --web-hostname=127.0.0.1 \
  --web-port="${DEV_PORT}" \
  --no-web-resources-cdn \
  "${HOT_RELOAD_FLAG[@]}"
