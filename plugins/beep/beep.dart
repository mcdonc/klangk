import 'dart:js_interop';

@JS('eval')
external JSAny? _eval(JSString code);

/// Play a beep tone using the Web Audio API.
void playBeep({double frequency = 440, int durationMs = 600}) {
  final code =
      '''
    (function() {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = $frequency;
      gain.gain.value = 0.3;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      var end = ctx.currentTime + ${durationMs / 1000.0};
      gain.gain.exponentialRampToValueAtTime(0.001, end);
      osc.stop(end + 0.05);
    })()
  ''';
  _eval(code.toJS);
}
