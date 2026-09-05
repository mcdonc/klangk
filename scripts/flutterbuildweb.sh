#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091 # sourced sibling helper
source "$(dirname "${BASH_SOURCE[0]}")/_python_common.sh"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${DEVENV_ROOT:-$SCRIPT_DIR/..}"

# Materialized feature payload lives in a build-owned tempdir (#1660): the
# declaration is the checked-in features.yaml at the repo root, the payload
# (symlinked trees + features.lock + generated .dart package) is ephemeral.
# Cleaned up on exit. flutterbuildweb also shares this dir with the workspace/
# host image builds when they chain off it via build-host-image.sh — but each
# top-level build driver owns its own tempdir, so a single EXIT trap per
# driver is enough.
PAYLOAD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/klangk-features-XXXXXX")"
trap 'rm -rf "$PAYLOAD_DIR"' EXIT

# Fetch/symlink features into the payload dir, then generate the Dart
# aggregator + features.json from those trees.
#
# Git-sourced features are skipped by default (update_features.py --local-only)
# — set KLANGKBUILD_BUILD_INCLUDE_REMOTE=1 to fetch them. Keeps CI off the network
# and resilient to upstream failures (the policy dates to #1691). Every
# feature in features.yaml is a local path entry today (soliplex was vendored
# in #1686), so the skip is currently a no-op; the gate stays as the generic
# remote-feature policy for any future git entry.
UPDATE_FLAGS=(--payload-dir "$PAYLOAD_DIR")
if [ "${KLANGKBUILD_BUILD_INCLUDE_REMOTE:-0}" != "1" ]; then
  UPDATE_FLAGS+=(--local-only)
fi
"${KLANGK_PYTHON}" scripts/update_features.py "${UPDATE_FLAGS[@]}"
"${KLANGK_PYTHON}" scripts/import_dart_features.py --payload-dir "$PAYLOAD_DIR"

# flterm is forked (github.com/runyaga/flterm) to build on the nix Flutter
# (forked at 3.41 / Dart 3.11; the toolchain is now 3.47 / Dart 3.13, and
# the fork still builds) -- upstream 0.0.3 needed Dart 3.12 for private-named
# parameters; the fork removes that. No host Flutter required. KLANGKBUILD_WEB_FLUTTER
# can still override the binary; defaults to `flutter` on PATH (nix toolchain).
FLUTTER="${KLANGKBUILD_WEB_FLUTTER:-flutter}"

# TEMPORARY: wasm builds are disabled to debug a production-only issue on
# rag.enfoldsystems.net that is not reproducible locally. This builds the
# dart2js (JS) renderer instead of WasmGC. To revert, restore the wasm flags:
#   --wasm --no-strip-wasm --no-minify-wasm
# (a benign "Wasm dry run succeeded" warning is expected in JS-only mode.)
cd src/frontend
"$FLUTTER" --disable-analytics
# Skip git-lfs smudge for transitive git deps. soliplex pulls ag_ui from
# ag-ui-protocol/ag-ui.git, whose apps/dojo/e2e/fixtures/test-image.png is
# LFS-tracked with an object unauthenticated CI can't fetch (#1691-class).
# We only need ag_ui's Dart source, not its binary fixtures, so skipping the
# LFS download is correct and harmless.
export GIT_LFS_SKIP_SMUDGE=1
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

"$FLUTTER" build web --release --no-wasm-dry-run --base-href=/ --no-web-resources-cdn --source-maps --no-minify-js --no-pub

# Record the plugin-set hash so the next build only clears the cache on change.
[ -f "$PLUGINS_DEPS" ] && sha256sum "$PLUGINS_DEPS" | cut -d' ' -f1 >"$PLUGINS_MARKER"

rm -f build/web/flutter_service_worker.js

# #3219/#3228: pdfrx's pdfium wasm worker is a same-origin blob whose
# bootstrap calls importScripts() with a plain string — a TrustedScriptURL
# sink INSIDE the worker scope. Blob workers inherit the creating
# document's CSP, so require-trusted-types-for applies there while the
# document-side default policy (index.html) does not exist in the worker.
# Patch the compiled blob template to install a same-shape default policy
# in the worker before importScripts runs. Guarded: the template literal
# must appear exactly once, so a pdfrx upgrade that changes it fails the
# build loudly instead of shipping a broken PDF viewer.
PDFIUM_TT_GUARD='";importScripts("'
PDFIUM_TT_PATCH='";try{trustedTypes&&trustedTypes.createPolicy&&trustedTypes.createPolicy("default",{createScriptURL:function(s){return s}})}catch(e){}importScripts("'
if [ "$(grep -cF "$PDFIUM_TT_GUARD" build/web/main.dart.js)" != "1" ]; then
  echo "pdfrx worker blob template not found exactly once in main.dart.js —" >&2
  echo "pdfrx likely changed; update PDFIUM_TT_PATCH in scripts/flutterbuildweb.sh" >&2
  exit 1
fi
sed -i "s|$PDFIUM_TT_GUARD|$PDFIUM_TT_PATCH|" build/web/main.dart.js

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
# Inject build hash as a meta tag so the Dart app can detect stale builds.
sed -i "s|</head>|<meta name=\"klangk-build-hash\" content=\"${HASH}\" />\n</head>|" "$BUILD_DIR/index.html"
echo "Cache-bust: v=$HASH"

# #3219: TT-safe pdfium worker. pdfrx's wasm backend builds its worker
# from a blob: wrapper whose first statement is importScripts(...) — a
# TrustedScriptURL sink inside the worker (which inherits the page's
# require-trusted-types-for but can define no policy). index.html pins
# globalThis.pdfiumWasmWorkerUrl to this file instead: the real
# pdfium_worker.js inlined behind the wasm URL the wrapper used to inject
# — same origin, no importScripts, no TT sinks.
PDFFRX_ASSETS="$BUILD_DIR/assets/packages/pdfrx/assets"
{
  echo '// klangk build-time file (#3219): TT-safe pdfium worker. Generated'
  echo '// by scripts/flutterbuildweb.sh — do not edit. Replaces pdfrx'"'"'s'
  echo '// blob: worker wrapper (importScripts is a TrustedScriptURL sink'
  echo '// under require-trusted-types-for).'
  echo 'const pdfiumWasmUrl = new URL("pdfium.wasm", self.location.href).href;'
  cat "$PDFFRX_ASSETS/pdfium_worker.js"
} >"$PDFFRX_ASSETS/worker-tt.js"

# Re-emit features.json AFTER the Flutter build (#1655). flutter build web
# may regenerate build/web/ and wipe a manifest written before it, so the
# pre-build emit in import_dart_features.py is followed by this post-build
# re-emit. The manifest is a frontend sibling file (read by the frontend at
# boot + one field by klangkd for container-env bridging) and must survive
# the Flutter build. Invoke via $SCRIPT_DIR (absolute) because CWD is
# src/frontend here (the cd above); the generator resolves its own paths
# from __file__ so it lands the manifest correctly regardless of CWD. The
# payload dir is the same one populated above — still populated, still
# readable (the trap fires only on exit).
"${KLANGK_PYTHON}" "$SCRIPT_DIR/import_dart_features.py" --payload-dir "$PAYLOAD_DIR" --features-only
