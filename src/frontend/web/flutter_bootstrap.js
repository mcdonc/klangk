{{flutter_build_config}}
{{flutter_js}}

// CanvasKit from this origin, always. By default the web loader fetches
// it from https://www.gstatic.com/flutter-canvaskit/… — the deployment
// contract serves every asset from local services, and the served CSP's
// connect-src/script-src admit first-party origins only. A built
// frontend ships its own canvaskit/ copy next to this file; a dev run
// (`flutter run`) has none, so the fmtk proxy serves the flutter SDK's
// copy at the same relative path (scripts/fmtk-up.sh).
//
// First-party fallback fonts (#3228). The web engine's font fallback
// service lazily fetches Noto script/emoji fallback fonts (and, when the
// app's FontManifest carries no Roboto family, a boot-time Roboto) from configuration.fontFallbackBaseUrl — by default
// https://fonts.gstatic.com/s/, which the served CSP blocks. Point it at
// the vendored same-origin mirror instead (the exact gstatic URL layout,
// produced by scripts/vendor_flutter_fallback_fonts.py), so missing-glyph
// fallbacks resolve locally and an offline session renders identically.
// The URL is relative to the document base, like every other asset
// (bundled assets are served from assets/assets/ — the manifest path gets
// an extra assets/ prefix, cf. the libghostty wasm URL in main.dart).
_flutter.loader.load({
  config: {
    canvasKitBaseUrl: "canvaskit/",
    fontFallbackBaseUrl: "assets/assets/fallback-fonts/",
  },
});
