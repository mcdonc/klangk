import 'popup_handle.dart';

/// Stub for non-web platforms (used during VM tests): no window to open,
/// so no handle comes back.
AuthFetchPopup? openUrl(String url) => null;
