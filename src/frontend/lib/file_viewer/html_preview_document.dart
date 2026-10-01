/// Content-Security-Policy for previewed HTML: inline CSS and embedded
/// `data:` images render; every other fetch, `<base>` and form target is
/// refused. `base-uri` and `form-action` do not fall back to `default-src`,
/// so they are set explicitly.
const htmlPreviewCsp = "default-src 'none'; style-src 'unsafe-inline'; "
    "img-src data:; base-uri 'none'; form-action 'none'";

const _cspMeta =
    '<meta http-equiv="Content-Security-Policy" content="$htmlPreviewCsp">';

final _leadingDoctype = RegExp(r'^﻿?\s*<!doctype[^>]*>', caseSensitive: false);

/// [html] with [htmlPreviewCsp] declared ahead of all of its content.
///
/// A leading doctype stays first, so the page keeps standards mode.
String previewDocument(String html) {
  final doctype = _leadingDoctype.firstMatch(html);
  if (doctype == null) return '$_cspMeta$html';
  return '${doctype[0]}$_cspMeta${html.substring(doctype.end)}';
}
