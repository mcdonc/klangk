import 'package:flutter/widgets.dart';

// coverage:ignore-file
/// Stubs for browser-only operations — used in VM tests.

void openUrl(String url) {}

void navigateTo(String url) {}

void downloadBytes(List<int> bytes, String filename) {}

void suppressContextMenuBriefly() {}

/// Stub — no context menu suppression needed outside browser.
Widget buildSuppressor(Widget child) => child;

/// Stub — return empty hash in VM tests.
String getLocationHash() => '';

/// Stub — no DOM paste events outside the browser; returns a no-op disposer.
void Function() installPasteListener(bool Function(String text) onPaste) =>
    () {};

/// Stub — no PageUp/PageDown suppression outside the browser.
void Function() installPageKeyListener(bool Function() shouldSuppress) => () {};

/// Stub — no system clipboard outside the browser.
Future<String?> readClipboardText() async => null;
