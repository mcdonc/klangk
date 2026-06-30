import 'package:flutter/widgets.dart';

// coverage:ignore-file
/// Stubs for browser-only operations — used in VM tests.

void openUrl(String url) {}

void navigateTo(String url) {}

void hardReload() {}

void downloadBytes(List<int> bytes, String filename) {}

void suppressContextMenuBriefly() {}

/// Stub — no context menu suppression needed outside browser.
Widget buildSuppressor(Widget child) => child;

/// Stub — return empty hash in VM tests.
String getLocationHash() => '';

/// Stub — return empty query string in VM tests.
String getLocationSearch() => '';

/// Stub — no User-Agent outside the browser.
String getUserAgent() => '';

/// Query params captured at startup (empty in VM tests).
Map<String, String> capturedPageQuery = {};

/// Stub — no-op in VM tests.
void capturePageQuery() {}

/// Stub — return empty browser ID in VM tests.
String getBrowserId(String instanceId) => '';

/// Stub — no DOM paste events outside the browser; returns a no-op disposer.
void Function() installPasteListener(bool Function(String text) onPaste) =>
    () {};

/// Stub — no PageUp/PageDown suppression outside the browser.
void Function() installPageKeyListener(bool Function() shouldSuppress) => () {};

/// Stub — no system clipboard outside the browser.
Future<String?> readClipboardText() async => null;

/// Stub — no beforeunload outside the browser.
void Function() onBeforeUnload(void Function() callback) => () {};

/// Stub — no build hash outside the browser.
String getBuildHash() => '';

/// Stub — no file picker outside the browser.
Future<List<int>?> pickFileBytes({String accept = ''}) async => null;

/// Stub — File System Access API is browser-only; signal "not handled" so the
/// caller falls back to a buffered download (or no-op in VM tests).
Future<bool> downloadStreamedUrl(
  String url, {
  required String filename,
  Map<String, String>? headers,
}) async =>
    false;
