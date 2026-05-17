import 'dart:html' as html;
import 'package:flutter/widgets.dart';

/// Open a URL in a new browser tab.
void openUrl(String url) {
  html.window.open(url, '_blank');
}

/// Download bytes as a file via a temporary blob URL.
void downloadBytes(List<int> bytes, String filename) {
  final blob = html.Blob([bytes]);
  final blobUrl = html.Url.createObjectUrlFromBlob(blob);
  final anchor = html.AnchorElement(href: blobUrl)..download = filename;
  anchor.click();
  html.Url.revokeObjectUrl(blobUrl);
}

/// Briefly suppress the browser context menu (for right-click handling).
void suppressContextMenuBriefly() {
  void suppress(html.Event e) {
    e.preventDefault();
  }

  html.document.addEventListener('contextmenu', suppress);
  Future.delayed(const Duration(milliseconds: 100), () {
    html.document.removeEventListener('contextmenu', suppress);
  });
}

/// Get the browser's location hash fragment.
String getLocationHash() => html.window.location.hash;

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
