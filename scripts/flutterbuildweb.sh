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

# TEMPORARY: wasm builds are disabled to debug a production-only issue on
# rag.enfoldsystems.net that is not reproducible locally. This builds the
# dart2js (JS) renderer instead of WasmGC. To revert, restore the wasm flags:
#   --wasm --no-strip-wasm --no-minify-wasm
# (a benign "Wasm dry run succeeded" warning is expected in JS-only mode.)
cd src/frontend
"$FLUTTER" --disable-analytics
"$FLUTTER" pub get

# Guard against a stale web plugin registrant. Flutter's incremental web build
# does not reliably re-run the web_plugin_registrant target when the plugin set
# changes, so a newly-added web plugin (e.g. video_player_web, url_launcher_web)
# can compile into the bundle *unregistered* -> "UnimplementedError: init() has
# not been implemented" at runtime. `flutter pub get` (above) rewrites
# .flutter-plugins-dependencies; when its contents change vs the last build, drop
# the build cache so the registrant is regenerated from the current plugin set.
# Incremental builds are preserved while the plugin set is unchanged.
PLUGINS_DEPS=.flutter-plugins-dependencies
PLUGINS_MARKER=.dart_tool/.klangk-web-plugins.sha256
if [ -f "$PLUGINS_DEPS" ]; then
  NEW_PLUGINS_HASH="$(sha256sum "$PLUGINS_DEPS" | cut -d' ' -f1)"
  if [ "$(cat "$PLUGINS_MARKER" 2>/dev/null || true)" != "$NEW_PLUGINS_HASH" ]; then
    echo "Plugin set changed -> clearing build cache to regenerate web plugin registrant"
    rm -rf .dart_tool/flutter_build
  fi
fi

"$FLUTTER" build web --release --base-href=/ --no-web-resources-cdn --source-maps --no-minify-js

# Record the plugin-set hash so the next build only clears the cache on change.
[ -f "$PLUGINS_DEPS" ] && sha256sum "$PLUGINS_DEPS" | cut -d' ' -f1 >"$PLUGINS_MARKER"

rm -f build/web/flutter_service_worker.js

# Inline `sourcesContent` into the source maps so devtools (especially
# Firefox, which doesn't handle the org-dartlang-sdk:/// scheme dart2js/
# dart2wasm emit) can resolve every frame without network fetches. Resolves
# 100% of sources from the on-disk Dart SDK, Flutter Engine, pub-cache, and
# app tree; the .map files grow (~25MB each) but the .wasm/.js artifacts are
# unaffected.
FLUTTER_SDK_DIR="$(cd "$(dirname "$(readlink -f "$(command -v "$FLUTTER")")")/.." && pwd)"
# Inline whichever maps exist: wasm builds emit main.dart.wasm.map, JS-only
# builds emit only main.dart.js.map.
MAPS=()
for m in build/web/main.dart.wasm.map build/web/main.dart.js.map; do
  [ -f "$m" ] && MAPS+=("$m")
done
if [ ${#MAPS[@]} -gt 0 ]; then
  python3 "$SCRIPT_DIR/inline_sources_in_map.py" "$FLUTTER_SDK_DIR" "${MAPS[@]}"
fi

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
