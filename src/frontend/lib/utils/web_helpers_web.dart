import 'dart:async';
import 'dart:convert';
import 'dart:js_interop';
import 'dart:typed_data';
import 'package:flutter/widgets.dart';
import 'package:web/web.dart' as web;

/// Open a URL in a new browser tab.
void openUrl(String url) {
  web.window.open(url, '_blank');
}

/// Navigate the current page to a URL (full page redirect).
void navigateTo(String url) {
  web.window.location.href = url;
}

/// Force a reload that bypasses any installed service worker.
///
/// `location.reload()` always goes through the active service worker. A
/// leftover SW (from an older build that registered one) will serve a cached
/// `index.html` whose `?v=` script tags still reference the old bundle, so
/// the new build never loads — exactly the "soft reload fails, shift-reload
/// works" symptom, since shift-reload bypasses the SW.
///
/// The shipped `index.html` already tries to unregister stale SWs, but that
/// code only runs *after* a fresh load — useless when the SW is itself serving
/// the stale page. So the Reload button unregisters all SWs first (awaiting
/// completion), then reloads. The next navigation then hits the network for a
/// fresh `index.html` (which the server marks `no-store`).
Future<void> hardReload() async {
  try {
    final regs =
        await web.window.navigator.serviceWorker.getRegistrations().toDart;
    await Future.wait(regs.toDart.map((r) => r.unregister().toDart));
  } catch (_) {
    // Best-effort: service-worker access may be unavailable (e.g. HTTP,
    // insecure context, older browser); fall through to a plain reload.
  }
  web.window.location.reload();
}

/// Download bytes as a file via a temporary blob URL.
void downloadBytes(List<int> bytes, String filename) {
  final parts = [Uint8List.fromList(bytes).toJS].toJS;
  final blob = web.Blob(parts);
  final blobUrl = web.URL.createObjectURL(blob);
  final anchor = web.HTMLAnchorElement()
    ..href = blobUrl
    ..download = filename;
  anchor.click();
  web.URL.revokeObjectURL(blobUrl);
}

/// Briefly suppress the browser context menu (for right-click handling).
void suppressContextMenuBriefly() {
  final handler = ((web.Event e) {
    e.preventDefault();
  }).toJS;
  web.document.addEventListener('contextmenu', handler);
  Future.delayed(const Duration(milliseconds: 100), () {
    web.document.removeEventListener('contextmenu', handler);
  });
}

/// Get the browser's location hash fragment.
String getLocationHash() => web.window.location.hash;

/// Get the browser's location query string (e.g. '?token=xxx').
String getLocationSearch() => web.window.location.search;

/// The browser's User-Agent string. Used to detect Firefox (whose
/// FailDelayManager can throttle WebSocket reconnects) so the HTTP pre-check
/// in [WsClient._waitForServer] only runs there.
String getUserAgent() => web.window.navigator.userAgent;

/// Query params captured from the page URL at startup, before GoRouter
/// navigation clears them. Plugin callback routes read from this.
Map<String, String> capturedPageQuery = {};

/// Call once from main() to snapshot the page-level query params.
void capturePageQuery() {
  final search = web.window.location.search;
  if (search.length > 1) {
    capturedPageQuery = Uri.splitQueryString(search.substring(1));
  }
}

/// Return a stable browser tab ID from sessionStorage.
///
/// Survives page refresh (same tab) but is unique per tab.
/// Used to route bridge requests to the correct browser tab.
/// The key is scoped by [instanceId] so multiple Klangk instances
/// on the same domain don't collide.
String getBrowserId(String instanceId) {
  final key = 'klangk.$instanceId.browser_id';
  var id = web.window.sessionStorage.getItem(key);
  if (id == null || id.isEmpty) {
    id = web.window.crypto.randomUUID();
    web.window.sessionStorage.setItem(key, id);
  }
  return id;
}

/// Routes the browser's native `paste` ClipboardEvent text to [onPaste].
///
/// Why this exists: Flutter's `Clipboard.getData` — and flterm's built-in
/// Ctrl/Cmd+V handler that calls it — read the clipboard via
/// `navigator.clipboard.readText()`. On Firefox that path yields nothing for
/// externally-copied text, so paste silently fails (Chrome/WebKit are fine).
/// The native `paste` event carries the payload in `clipboardData` with no
/// permission prompt on any browser, so we read it there instead.
///
/// [onPaste] returns whether it consumed the text. When it does, the event's
/// default is prevented so the text isn't also inserted into Flutter's hidden
/// text-input (which would double-paste); when it doesn't (e.g. the terminal
/// isn't focused), the event is left alone so other inputs paste normally.
/// Runs in the capture phase. Returns a disposer that removes the listener.
void Function() installPasteListener(bool Function(String text) onPaste) {
  final handler = ((web.Event event) {
    final data = (event as web.ClipboardEvent).clipboardData;
    final text = data?.getData('text/plain') ?? '';
    if (text.isEmpty) return;
    if (onPaste(text)) event.preventDefault();
  }).toJS;
  web.document.addEventListener('paste', handler, true.toJS);
  return () => web.document.removeEventListener('paste', handler, true.toJS);
}

/// Prevents the browser from handling PageUp / PageDown when the terminal
/// is focused.  Firefox dispatches these keys for native page scrolling
/// before Flutter's event system sees them, so the terminal never receives
/// the key.  This capture-phase listener calls `preventDefault` when
/// [shouldSuppress] returns true (i.e. the terminal has focus), letting
/// Flutter forward the key to the PTY instead.
/// Returns a disposer that removes the listener.
void Function() installPageKeyListener(bool Function() shouldSuppress) {
  final handler = ((web.Event event) {
    final ke = event as web.KeyboardEvent;
    if ((ke.key == 'PageUp' || ke.key == 'PageDown') && shouldSuppress()) {
      event.preventDefault();
    }
  }).toJS;
  web.document.addEventListener('keydown', handler, true.toJS);
  return () => web.document.removeEventListener('keydown', handler, true.toJS);
}

/// Reads the system clipboard as plain text via the async Clipboard API.
///
/// Used only by the right-click "Paste" menu item: a synthetic button click is
/// not a native paste gesture, so no `paste` event fires and [installPasteListener]
/// can't cover it. On Firefox this may surface the browser's paste-confirmation
/// UI; the keyboard path stays prompt-free via the native event. Returns null
/// if the clipboard is empty or the read is denied.
Future<String?> readClipboardText() async {
  try {
    final text = await web.window.navigator.clipboard.readText().toDart;
    return text.toDart;
  } catch (e) {
    debugPrint('[WebHelpers] clipboard read failed: $e');
    return null;
  }
}

/// Register a callback to run when the page is about to unload (reload,
/// close tab, navigate away). Used to send a clean WebSocket close frame
/// so Firefox's FailDelayManager doesn't throttle the next connection.
void Function() onBeforeUnload(void Function() callback) {
  final handler = ((web.Event _) {
    callback();
  }).toJS;
  web.window.addEventListener('beforeunload', handler);
  return () => web.window.removeEventListener('beforeunload', handler);
}

/// Open a file picker and return the selected file's bytes.
/// Returns null if the user cancels.
Future<List<int>?> pickFileBytes({String accept = ''}) async {
  final completer = Completer<List<int>?>();
  final input = web.HTMLInputElement()
    ..type = 'file'
    ..accept = accept;
  input.addEventListener(
    'change',
    ((web.Event _) {
      final files = input.files;
      if (files == null || files.length == 0) {
        completer.complete(null);
        return;
      }
      final reader = web.FileReader();
      reader.addEventListener(
        'load',
        ((web.Event _) {
          final result = reader.result;
          if (result != null) {
            final arrayBuf = result as JSArrayBuffer;
            completer.complete(arrayBuf.toDart.asUint8List());
          } else {
            completer.complete(null);
          }
        }).toJS,
      );
      reader.readAsArrayBuffer(files.item(0)!);
    }).toJS,
  );
  input.click();
  return completer.future;
}

/// Read the build hash from the <meta name="klangk-build-hash"> tag.
/// Returns empty string if not found (e.g. dev server without build script).
String getBuildHash() {
  final meta = web.document.querySelector('meta[name="klangk-build-hash"]');
  return meta?.getAttribute('content') ?? '';
}

/// Web implementation — suppresses native browser context menu on right-click.
Widget buildSuppressor(Widget child) {
  return Listener(
    onPointerDown: (event) {
      if (event.buttons == 2) {
        suppressContextMenuBriefly();
      }
    },
    child: child,
  );
}

// --- Streaming download via the File System Access API ---------------------
//
// `downloadBytes` buffers the whole response in memory (it builds a Blob),
// which for a large workspace export can be hundreds of MB of RAM. Where the
// browser supports the File System Access API (Chrome, Edge, Opera, and most
// desktop Chromium derivatives), we instead `fetch()` the URL with the auth
// header and stream the response body straight to a user-chosen file on disk,
// one chunk at a time — so memory use stays flat regardless of archive size.
//
// Firefox and Safari don't implement `showSaveFilePicker`; there the function
// returns `false` so the caller can fall back to the buffered path. This is
// the reason `downloadStreamedUrl` is `Future<bool>` rather than `void`.

@JS()
extension type _SaveFilePickerOptions._(JSObject _) implements JSObject {
  external factory _SaveFilePickerOptions({JSString suggestedName});
}

@JS()
extension type _FileSystemFileHandle._(JSObject _) implements JSObject {
  external JSPromise createWritable();
}

@JS()
extension type _FileSystemWritableFileStream._(JSObject _) implements JSObject {
  external JSPromise write(JSUint8Array chunk);
  external JSPromise close();
}

@JS()
extension type _FetchOptions._(JSObject _) implements JSObject {
  external factory _FetchOptions({JSString method, JSObject headers});
}

@JS()
extension type _FetchResponse._(JSObject _) implements JSObject {
  external int get status;
  external JSString get statusText;
  external _ReadableStream? get body;
}

@JS()
extension type _ReadableStream._(JSObject _) implements JSObject {
  // getReader() is SYNCHRONOUS in the standard (returns the reader directly,
  // not a Promise) — declaring it as a promise and awaiting it would call
  // .then on a non-thenable and reject (#700 e2e caught this).
  external _StreamReader getReader();
}

@JS()
extension type _StreamReader._(JSObject _) implements JSObject {
  external JSPromise read();
  external void releaseLock();
}

@JS()
extension type _ReadResult._(JSObject _) implements JSObject {
  external bool get done;
  external JSUint8Array? get value;
}

@JS('fetch')
external JSPromise _streamFetch(JSString url, _FetchOptions options);

@JS()
extension type _WindowWithSavePicker._(JSObject _) implements JSObject {
  external JSPromise showSaveFilePicker(_SaveFilePickerOptions options);
}

/// Download [url] (issuing [headers], e.g. `Authorization`) by streaming the
/// response body straight to a file on disk via the File System Access API.
///
/// Returns `true` if the download was handled (streamed to disk). Returns
/// `false` if the browser lacks `showSaveFilePicker` (Firefox/Safari) — the
/// caller should then fall back to a buffered download. Throws on a non-200
/// response or on an I/O error mid-stream.
Future<bool> downloadStreamedUrl(
  String url, {
  required String filename,
  Map<String, String>? headers,
}) async {
  // Feature-detect the File System Access API. Absent in Firefox/Safari.
  // `Reflect.has` is a plain JS global; `package:web`'s Window doesn't
  // expose an index operator in this version, so we probe via Reflect.
  if (!_reflectHas(web.window, 'showSaveFilePicker'.toJS)) {
    return false;
  }

  // Issue the request first (without reading the body). The body stays a
  // pending stream, so nothing is buffered yet; the backend's tar process is
  // simply held open by backpressure until we start reading.
  final fetchOpts = _FetchOptions(
    method: 'GET'.toJS,
    headers: _headersToJS(headers),
  );
  final response =
      (await _streamFetch(url.toJS, fetchOpts).toDart) as _FetchResponse;
  if (response.status != 200) {
    throw StreamedDownloadException(
      response.status,
      response.statusText.toDart,
    );
  }
  final body = response.body;
  if (body == null) {
    throw StreamedDownloadException(response.status, 'response had no body');
  }

  // Now prompt for the destination. Doing this after the fetch lets us bail
  // (no empty file created) on auth/server errors.
  final handle = (await (web.window as _WindowWithSavePicker)
      .showSaveFilePicker(
        _SaveFilePickerOptions(suggestedName: filename.toJS),
      )
      .toDart) as _FileSystemFileHandle;
  final writable =
      (await handle.createWritable().toDart) as _FileSystemWritableFileStream;
  final reader = body.getReader();
  try {
    while (true) {
      final result = (await reader.read().toDart) as _ReadResult;
      if (result.done) break;
      final chunk = result.value;
      if (chunk != null && chunk.toDart.length > 0) {
        await writable.write(chunk).toDart;
      }
    }
    await writable.close().toDart;
  } catch (e) {
    // Best-effort close so we don't leave a dangling writable; the partial
    // file remains on disk, which is better than a locked handle.
    try {
      await writable.close().toDart;
    } catch (_) {}
    rethrow;
  } finally {
    reader.releaseLock();
  }
  return true;
}

/// Build a plain JS headers object from a Dart map, via `Object.fromEntries`.
/// (`package:web` 1.1 has no JSObject index setter; this avoids needing it.)
@JS('Object.fromEntries')
external JSObject _objectFromEntries(JSArray entries);

/// `Reflect.has(target, key)` — property probe that works on any JS object.
@JS('Reflect.has')
external bool _reflectHas(JSObject target, JSString key);

JSObject _headersToJS(Map<String, String>? headers) {
  if (headers == null || headers.isEmpty) {
    return _objectFromEntries([] as JSArray);
  }
  // Build [[k, v], ...] as JSArray<JSArray<JSString>>.
  final entries = <JSArray>[];
  headers.forEach((k, v) {
    entries.add([k.toJS, v.toJS].toJS);
  });
  return _objectFromEntries(entries.toJS);
}

/// Thrown by [downloadStreamedUrl] when the response isn't 200.
class StreamedDownloadException implements Exception {
  final int status;
  final String statusText;
  StreamedDownloadException(this.status, this.statusText);
  @override
  String toString() => 'StreamedDownloadException: $status $statusText';
}

/// Fetch + parse the sibling `features.json` (next to index.html) — the
/// runtime feature manifest emitted by the build (#1655). Returns null when
/// the file is absent (pre-build, or a wheel that didn't include it) so the
/// caller can degrade: no manifest means no defaults list, so the active set
/// is whatever /api/config's features_enable says verbatim, or — when that's
/// also unset — every compiled-in feature stays active (no filtering).
Future<Map<String, dynamic>?> fetchFeaturesManifest() async {
  try {
    final response = await web.window.fetch('features.json'.toJS).toDart;
    if (response.status != 200) return null;
    final text = (await (await response.blob().toDart).text().toDart).toDart;
    final decoded = jsonDecode(text);
    if (decoded is Map<String, dynamic>) return decoded;
    return null;
  } catch (_) {
    return null;
  }
}
