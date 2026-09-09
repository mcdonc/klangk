/// Non-web stub for the popup↔opener messaging (see window_messaging.dart).

/// Authorization results delivered by the authorize popup. Always empty
/// off the web (the flow only exists in the browser).
Stream<Map<String, String>> gitAuthMessages() => const Stream.empty();

/// Deliver one authorization result to the window that opened this one.
/// The message is either a code or a provider error (e.g. the user
/// denied the application).
void sendGitAuthResultToOpener(String state, {String? code, String? error}) {}

/// Ask the browser to close this window (works for script-opened popups).
void closeCurrentWindow() {}
