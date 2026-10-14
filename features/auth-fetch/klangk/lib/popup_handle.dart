/// A closable handle to the sign-in popup window. ``null`` when the
/// platform cannot open or control one (VM tests). The feature closes
/// the popup itself when the flow completes — the target page is the
/// site's own static content and never closes itself.
abstract interface class AuthFetchPopup {
  void close();
}
