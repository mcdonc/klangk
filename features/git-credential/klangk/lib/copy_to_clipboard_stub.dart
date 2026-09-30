/// Stub for non-web platforms (used during VM tests): no system clipboard
/// write is possible here, so signal "not handled" — the caller falls back
/// to Flutter's `Clipboard.setData`.
Future<bool> copyToClipboard(String text) async => false;
