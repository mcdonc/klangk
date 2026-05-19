#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

# Auto-fetch plugins on first run
if [ -f "$BARK_PLUGINS_DIR/plugins.yaml" ] && [ ! -f "$BARK_PLUGINS_DIR/plugins.lock" ]; then
  echo "No plugins.lock found, running update-plugins..."
  python3 scripts/update_plugins.py
fi

python3 scripts/import_dart_plugins.py
cd src/frontend && flutter --disable-analytics && flutter pub get && flutter build web --base-href=/ --no-wasm-dry-run --no-web-resources-cdn
rm -f build/web/flutter_service_worker.js
