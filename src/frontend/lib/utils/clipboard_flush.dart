/// Deferred system-clipboard write for the browser helper (#3516).
///
/// Firefox and Safari allow `navigator.clipboard.writeText` only inside the
/// task that carries the user activation, so a clipboard write delivered over
/// the WebSocket bridge — the terminal-selection copy path, where tmux's
/// `copy-pipe` reaches the tab through the backend — is rejected: the write
/// always lands in a WebSocket message handler, outside any gesture task.
///
/// A [ClipboardFlushController] holds the rejected text and redelivers it on
/// the next input event. Input-event listeners run inside a gesture task, so
/// the same browsers permit the write there. The controller owns only the
/// policy (what is pending, when it clears); the web helper supplies the
/// write function and wires [flush] to the DOM listeners, which keeps this
/// class testable on the VM.
class ClipboardFlushController {
  ClipboardFlushController(this._write);

  /// Performs one clipboard write attempt. Returns whether it succeeded.
  final Future<bool> Function(String text) _write;
  String? _pending;

  /// True while a rejected write waits for the next input event.
  bool get isArmed => _pending != null;

  /// Records [text] for delivery on the next input event. A later arm
  /// replaces an earlier pending text: the newest selection wins.
  void arm(String text) => _pending = text;

  /// Delivers the pending write; call from an input-event listener. The
  /// pending text stays armed after a failed attempt, so the next input
  /// event retries it.
  Future<void> flush() async {
    final text = _pending;
    if (text == null) return;
    if (await _write(text)) _pending = null;
  }
}
