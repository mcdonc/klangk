import 'package:flutter/widgets.dart';

/// Stubs for browser-only operations — used in VM tests.

void openUrl(String url) {}

void downloadBytes(List<int> bytes, String filename) {}

void suppressContextMenuBriefly() {}

/// Stub — no context menu suppression needed outside browser.
Widget buildSuppressor(Widget child) => child;

/// Stub — return empty hash in VM tests.
String getLocationHash() => '';
