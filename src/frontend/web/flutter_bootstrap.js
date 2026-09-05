{
  {
    flutter_build_config;
  }
}
{
  {
    flutter_js;
  }
}

// First-party fallback fonts (#3228). The web engine's font fallback
// service lazily fetches Noto script/emoji fallback fonts (and the
// boot-time Roboto) from configuration.fontFallbackBaseUrl — by default
// https://fonts.gstatic.com/s/, which the served CSP blocks. Point it at
// the vendored same-origin mirror instead (the exact gstatic URL layout,
// produced by scripts/vendor_flutter_fallback_fonts.py), so missing-glyph
// fallbacks resolve locally and an offline session renders identically.
// The URL is relative to the document base, like every other asset
// (bundled assets are served from assets/assets/ — the manifest path gets
// an extra assets/ prefix, cf. the libghostty wasm URL in main.dart).
_flutter.loader.load({
  config: {
    fontFallbackBaseUrl: "assets/assets/fallback-fonts/",
  },
});
