// coverage:ignore-file
import 'dart:typed_data';

/// Stubs for the browser-only object-URL operations the video renderer needs.
///
/// Blob URLs are a browser concept; outside the web there is nothing to create.
/// VM tests fake the video platform, so the returned value is never actually
/// fetched — a placeholder keeps [Uri.parse] happy.
String createVideoObjectUrl(Uint8List bytes, String? mimeType) => 'about:blank';

/// Stub — no object URL to release outside the browser.
void revokeVideoObjectUrl(String url) {}
