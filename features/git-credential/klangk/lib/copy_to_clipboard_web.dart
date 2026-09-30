import 'dart:js_interop';

import 'package:flutter/foundation.dart';
import 'package:web/web.dart' as web;

/// Copy [text] to the system clipboard (browser).
///
/// Mirrors the frontend's `web_helpers_web.setClipboardText` (the feature
/// package cannot import the frontend's helpers): prefers the async
/// Clipboard API, which is secure-context-only (HTTPS / `localhost`).
/// Over plain HTTP `navigator.clipboard` is `undefined`, so the access
/// below throws and we fall back to `document.execCommand('copy')`
/// against a transient `<textarea>` — the only clipboard-write path in an
/// insecure context (#2166 class). Returns whether the copy succeeded;
/// the caller falls back to `Clipboard.setData` on `false`.
Future<bool> copyToClipboard(String text) async {
  try {
    await web.window.navigator.clipboard.writeText(text).toDart;
    return true;
  } catch (e) {
    debugPrint('[git-credential] clipboard.writeText failed: $e');
  }
  return _execCommandCopy(text);
}

// `document.execCommand('copy')` is deprecated but works in all contexts;
// it needs the document focused and (modern browsers) a user gesture —
// the copy button click provides both.
bool _execCommandCopy(String text) {
  final doc = web.document;
  final ta = doc.createElement('textarea') as web.HTMLTextAreaElement;
  ta.value = text;
  ta.style
    ..position = 'fixed'
    ..top = '-9999px'
    ..left = '-9999px';
  doc.body?.appendChild(ta);
  ta.select();
  final ok = doc.execCommand('copy');
  ta.remove();
  return ok;
}
