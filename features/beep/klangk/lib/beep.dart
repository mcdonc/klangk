import 'package:web/web.dart' as web;

/// Play a beep tone using the Web Audio API.
///
/// Typed interop only (no JS ``eval``) so the shipped CSP can omit
/// ``unsafe-eval`` (#3149).
void playBeep({double frequency = 440, int durationMs = 600}) {
  final ctx = web.AudioContext();
  final osc = ctx.createOscillator();
  final gain = ctx.createGain();
  osc.type = 'sine';
  osc.frequency.value = frequency;
  gain.gain.value = 0.3;
  osc.connect(gain);
  gain.connect(ctx.destination);
  osc.start();
  final end = ctx.currentTime + durationMs / 1000.0;
  gain.gain.exponentialRampToValueAtTime(0.001, end);
  osc.stop(end + 0.05);
}
