import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:url_launcher/url_launcher.dart';

// coverage:ignore-file
/// Non-web implementation of the helpers that are browser-native on web.
/// Used by the desktop builds (macOS / Linux / Windows) and by VM tests.
///
/// The web counterpart lives in `web_helpers_web.dart`; the importing files
/// pick between them with `if (dart.library.js_interop)`.

/// Open [url] in the platform's default browser.
void openUrl(String url) {
  final uri = Uri.tryParse(url);
  if (uri == null) return;
  // Fire-and-forget; launchUrl is async but call sites are synchronous.
  launchUrl(uri, mode: LaunchMode.externalApplication);
}

/// Prompt for a save location and write [bytes] there. No-op if the user
/// cancels the dialog.
void downloadBytes(List<int> bytes, String filename) {
  FilePicker.platform
      .saveFile(fileName: filename, bytes: Uint8List.fromList(bytes))
      .then((path) {
    // On desktop saveFile returns the chosen path but (depending on the
    // platform) may not write the bytes itself — write them to be sure.
    if (path != null && !File(path).existsSync()) {
      File(path).writeAsBytesSync(bytes);
    }
  });
}

/// No browser context menu to suppress outside the browser.
void suppressContextMenuBriefly() {}

/// No context-menu suppression needed outside the browser.
Widget buildSuppressor(Widget child) => child;

/// No URL fragment routing outside the browser.
String getLocationHash() => '';

/// No DOM paste events outside the browser; keyboard paste is handled by
/// flterm's built-in Clipboard.getData path (re-enabled on desktop). Returns
/// a no-op disposer.
void Function() installPasteListener(bool Function(String text) onPaste) =>
    () {};

/// Read the system clipboard (used by the right-click "Paste" menu item).
Future<String?> readClipboardText() async {
  final data = await Clipboard.getData(Clipboard.kTextPlain);
  return data?.text;
}
