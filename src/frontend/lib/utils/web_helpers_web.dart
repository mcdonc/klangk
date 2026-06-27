import 'dart:async';
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

/// Force a full page reload.
void hardReload() {
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
