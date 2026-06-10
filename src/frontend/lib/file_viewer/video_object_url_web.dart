import 'dart:js_interop';
import 'dart:typed_data';

import 'package:web/web.dart' as web;

/// Wraps [bytes] in a Blob and returns a `blob:` object URL `<video>` can play.
///
/// The file API authenticates with a Bearer header, so the file's plain
/// `downloadUrl` can't be handed to a `<video src>` directly — the element
/// can't set that header. Reading the bytes (authenticated) and serving them
/// from an in-memory blob is the web-compatible path. [mimeType] is set on the
/// blob when known so the browser picks the right decoder.
String createVideoObjectUrl(Uint8List bytes, String? mimeType) {
  final parts = [bytes.toJS].toJS;
  final blob = (mimeType != null && mimeType.isNotEmpty)
      ? web.Blob(parts, web.BlobPropertyBag(type: mimeType))
      : web.Blob(parts);
  return web.URL.createObjectURL(blob);
}

/// Releases the blob URL created by [createVideoObjectUrl] (frees the bytes).
void revokeVideoObjectUrl(String url) => web.URL.revokeObjectURL(url);
