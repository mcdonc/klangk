#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

# Auto-fetch plugins on first run
if [ -f "$KLANGK_PLUGINS_DIR/plugins.yaml" ] && [ ! -f "$KLANGK_PLUGINS_DIR/plugins.lock" ]; then
  echo "No plugins.lock found, running update-plugins..."
  python3 scripts/update_plugins.py
fi

python3 scripts/import_dart_plugins.py

# flterm is forked (github.com/runyaga/flterm) to build on the nix Flutter
# (3.41 / Dart 3.11) -- upstream 0.0.3 needs Dart 3.12 for private-named
# parameters; the fork removes that. No host Flutter required. KLANGK_WEB_FLUTTER
# can still override the binary; defaults to `flutter` on PATH (nix toolchain).
FLUTTER="${KLANGK_WEB_FLUTTER:-flutter}"

cd src/frontend && "$FLUTTER" --disable-analytics && "$FLUTTER" pub get && "$FLUTTER" build web --wasm --release --base-href=/ --no-web-resources-cdn --source-maps --no-strip-wasm
rm -f build/web/flutter_service_worker.js

# Cache-busting: append a content hash to flutter_bootstrap.js reference
# in index.html. Since index.html is served with no-cache headers, browsers
# always get the latest reference. The ?v= query string busts cached copies
# of the bootstrap script, which in turn loads fresh main.dart.{wasm,mjs,js}.
BUILD_DIR=build/web
# Wasm builds emit main.dart.wasm; legacy JS builds emit main.dart.js.
# Hash whichever entrypoint exists so the cache-bust survives both modes.
for f in main.dart.wasm main.dart.js; do
  if [ -f "$BUILD_DIR/$f" ]; then
    HASH=$(sha256sum "$BUILD_DIR/$f" | cut -c1-12)
    break
  fi
done
sed -i "s|flutter_bootstrap.js|flutter_bootstrap.js?v=${HASH}|" "$BUILD_DIR/index.html"
echo "Cache-bust: v=$HASH"
