import 'dart:convert';
import 'dart:math';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'dart:js_interop';

@JS('eval')
external JSAny? _eval(JSString code);

class BoingBallPlugin extends ToolPlugin with ChangeNotifier {
  bool _active = false;
  double _speed = 1.0;
  bool _configLoaded = false;

  @override
  Map<String, ToolHandler> get handlers => {'boing': _handle};

  Future<String> _handle(Map<String, dynamic> request) async {
    if (!_configLoaded) await _loadConfig();
    _active = true;
    notifyListeners();
    return 'Boing!';
  }

  Future<void> _loadConfig() async {
    _configLoaded = true;
    try {
      final resp = await http.get(Uri.parse('$baseUrl/api/config'));
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as Map<String, dynamic>;
        final speed = data['klangk_boing_speed'] as String?;
        if (speed != null && speed.isNotEmpty) {
          _speed = double.tryParse(speed) ?? 1.0;
        }
      }
    } catch (_) {}
  }

  @override
  Widget? buildOverlay(BuildContext context) {
    return _BoingOverlay(plugin: this);
  }
}

// ---------- Sound ----------

/// Pre-initialize the AudioContext on the first user interaction
/// so Chrome's autoplay policy is satisfied before any bounces.
void _ensureAudioContext() {
  final code = '''
    (function() {
      if (!window._boingCtx) {
        window._boingCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (window._boingCtx.state === 'suspended') {
        window._boingCtx.resume();
      }
    })()
  ''';
  _eval(code.toJS);
}

void _playBoingSound({required double panX, bool isFloor = true}) {
  final rate = isFloor ? 1.0 : 1.4;
  final vol = isFloor ? 0.6 : 0.35;
  // Play embedded MP3 of the original boing sample via Web Audio API.
  // Base64 MP3 decoded once and cached on window._boingBuf.
  final code =
      '''
    (function() {
      if (!window._boingCtx) {
        window._boingCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      var ctx = window._boingCtx;
      ctx.resume();
      function play(buf) {
        var src = ctx.createBufferSource();
        src.buffer = buf;
        src.playbackRate.value = $rate;
        var g = ctx.createGain();
        g.gain.value = $vol;
        src.connect(g);
        g.connect(ctx.destination);
        src.start();
      }
      if (window._boingBuf) { play(window._boingBuf); return; }
      var b64 = "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9C6dJTkvHaAZIakyLqGxuVXEkchSNI8Es0MmyaEwOiaSD9Qqd6zKVIflklIa//uYxO+CJLonNE1hicPhwGFVzDC5xuBMhrI4rVtrKZeenTbtvXHMH1iMiSQikdnkbtpnZmq5KxG7a0NP6Z3JmzMF3bfa7jy1qGBKhrH3bX54TKDT1UxBTUUzLjEwMFVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVVV";
      var bin = atob(b64);
      var arr = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      ctx.decodeAudioData(arr.buffer).then(function(buf) {
        window._boingBuf = buf;
        play(buf);
      });
    })()
  ''';
  _eval(code.toJS);
}

// ---------- overlay ----------

class _BoingOverlay extends StatefulWidget {
  final BoingBallPlugin plugin;
  const _BoingOverlay({required this.plugin});

  @override
  State<_BoingOverlay> createState() => _BoingOverlayState();
}

class _BoingOverlayState extends State<_BoingOverlay>
    with SingleTickerProviderStateMixin {
  late AnimationController _ctrl;
  bool _visible = false;

  double _x = 0.5;
  double _y = 0.1;
  double _vx = 0.004;
  double _vy = 0.0;
  double _phase = 0;
  int _spinDir = 1;
  double _aspectRatio = 1.5;
  int _lastBounceFrame = -100;
  static const double _gravity = 0.000075;
  static const double _damping = 0.92;
  static const double _maxVy = 0.012;
  static const double _ballFrac = 0.33;
  static const int _minBounceInterval = 8;
  static const _durationSec = 24;
  int _frame = 0;

  @override
  void initState() {
    super.initState();
    _ctrl = AnimationController(
      vsync: this,
      duration: Duration(seconds: _durationSec),
    );
    _ctrl.addListener(_tick);
    widget.plugin.addListener(_onUpdate);
    HardwareKeyboard.instance.addHandler(_onKey);
    // Pre-create AudioContext on first user click so Chrome allows audio.
    _eval(
      '''
      if (!window._boingCtxReady) {
        window._boingCtxReady = true;
        document.addEventListener('click', function _initBoing() {
          window._boingCtx = new (window.AudioContext || window.webkitAudioContext)();
          document.removeEventListener('click', _initBoing);
        }, {once: true});
      }
    '''
          .toJS,
    );
  }

  @override
  void dispose() {
    widget.plugin.removeListener(_onUpdate);
    HardwareKeyboard.instance.removeHandler(_onKey);
    _ctrl.removeListener(_tick);
    _ctrl.dispose();
    super.dispose();
  }

  void _onUpdate() {
    if (!mounted) return;
    if (widget.plugin._active) {
      widget.plugin._active = false;
      _startAnimation();
    }
  }

  void _startAnimation() async {
    // Pre-render ball frames at the current size (only once per radius)
    final size = MediaQuery.of(context).size;
    final overlayH = size.height * 0.75;
    final radius = (overlayH * _ballFrac).toInt();
    if (radius > 0) await _preRenderBallFrames(radius);
    if (!mounted) return;
    setState(() => _visible = true);
    _x = 0.15;
    _y = _ballFrac + 0.02;
    _vx = 0.004 * widget.plugin._speed;
    _vy = 0.0;
    _phase = 0;
    _spinDir = 1;
    _frame = 0;
    _lastBounceFrame = -100;
    _ctrl.reset();
    _ctrl.forward().then((_) {
      if (mounted) _dismiss();
    });
  }

  bool _onKey(KeyEvent event) {
    if (_visible &&
        event is KeyDownEvent &&
        event.logicalKey == LogicalKeyboardKey.escape) {
      _dismiss();
      return true;
    }
    return false;
  }

  void _dismiss() {
    _ctrl.stop();
    if (mounted) setState(() => _visible = false);
  }

  void _tick() {
    if (!mounted || !_visible) return;
    final speed = widget.plugin._speed;
    _frame++;
    setState(() {
      _vy += _gravity * speed;
      _vy = _vy.clamp(-_maxVy, _maxVy);
      _x += _vx * speed;
      _y += _vy * speed;

      const yMin = _ballFrac;
      const yMax = 1.0 - _ballFrac;
      final xPad = _ballFrac / _aspectRatio;
      final xMin = xPad;
      final xMax = 1.0 - xPad;

      final canBounce = (_frame - _lastBounceFrame) >= _minBounceInterval;

      if (_y >= yMax) {
        _y = yMax;
        _vy = -_vy.abs() * _damping;
        if (canBounce) {
          _playBoingSound(panX: _x, isFloor: true);
          _lastBounceFrame = _frame;
        }
      }
      if (_y <= yMin) {
        _y = yMin;
        _vy = _vy.abs() * _damping;
      }
      if (_x <= xMin) {
        _x = xMin;
        _vx = _vx.abs();
        _spinDir = 1;
        if (canBounce) {
          _playBoingSound(panX: _x, isFloor: false);
          _lastBounceFrame = _frame;
        }
      }
      if (_x >= xMax) {
        _x = xMax;
        _vx = -_vx.abs();
        _spinDir = -1;
        if (canBounce) {
          _playBoingSound(panX: _x, isFloor: false);
          _lastBounceFrame = _frame;
        }
      }

      _phase += 0.0675 * _spinDir * speed;
      if (_phase < 0) _phase += 14;
      if (_phase >= 14) _phase -= 14;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (!_visible) return const SizedBox.shrink();
    final size = MediaQuery.of(context).size;
    final insetX = size.width * 0.125;
    final insetY = size.height * 0.125;
    final overlayW = size.width - insetX * 2;
    final overlayH = size.height - insetY * 2;
    if (overlayH > 0) _aspectRatio = overlayW / overlayH;
    return Positioned.fill(
      child: Stack(
        children: [
          Positioned(
            left: insetX,
            top: insetY,
            width: overlayW,
            height: overlayH,
            child: ClipRRect(
              borderRadius: BorderRadius.circular(12),
              child: CustomPaint(
                painter: _BoingScenePainter(
                  ballX: _x,
                  ballY: _y,
                  phase: _phase,
                  ballFrac: _ballFrac,
                ),
                child: const SizedBox.expand(),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

// ---------- scene painter ----------

// Pre-rendered ball frame cache: 28 frames (14 cols * 2 for smooth rotation).
// Rendered once at the needed radius, then blitted each frame.
List<ui.Image>? _ballFrames;
int _ballFrameRadius = 0;

Future<void> _preRenderBallFrames(int radius) async {
  if (_ballFrames != null && _ballFrameRadius == radius) return;
  const cols = 14;
  const rows = 8;
  const nFrames = 56;
  const tilt = -17 * pi / 180; // tilt right
  final cosTilt = cos(tilt);
  final sinTilt = sin(tilt);
  final diam = radius * 2;
  // Capped resolution — good enough quality without hanging
  final res = min(diam, 120);

  final frames = <ui.Image>[];
  for (int frame = 0; frame < nFrames; frame++) {
    final rotAngle = frame / nFrames * 2 * pi;
    final cosRot = cos(rotAngle);
    final sinRot = sin(rotAngle);

    final recorder = ui.PictureRecorder();
    final canvas = Canvas(
      recorder,
      Rect.fromLTWH(0, 0, diam.toDouble(), diam.toDouble()),
    );

    final redPaint = Paint()..color = const Color(0xFFFF0000);
    final whitePaint = Paint()..color = const Color(0xFFFFFFFF);
    final step = diam / res;

    for (int py = 0; py < res; py++) {
      final ny = (py + 0.5) / res * 2 - 1;
      for (int px = 0; px < res; px++) {
        final nx = (px + 0.5) / res * 2 - 1;
        final r2 = nx * nx + ny * ny;
        if (r2 > 1) continue;

        final nz = sqrt(1 - r2);
        final tx = nx * cosTilt - ny * sinTilt;
        final ty = nx * sinTilt + ny * cosTilt;
        final rx = tx * cosRot + nz * sinRot;
        final rz = -tx * sinRot + nz * cosRot;

        final phi = atan2(rx, rz) + pi;
        final u = (phi / (2 * pi) * cols).floor();
        final v = ((ty + 1) / 2 * rows).clamp(0, rows - 1).floor();

        final isRed = (u + v) % 2 == 0;
        canvas.drawRect(
          Rect.fromLTWH(px * step, py * step, step + 0.5, step + 0.5),
          isRed ? redPaint : whitePaint,
        );
      }
    }

    // Specular highlight
    final c = diam / 2.0;
    canvas.drawCircle(
      Offset(c - radius * 0.3, c - radius * 0.3),
      radius * 0.22,
      Paint()
        ..color = Colors.white.withValues(alpha: 0.45)
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 16),
    );

    // Clip to circle
    final picture = recorder.endRecording();
    final raw = await picture.toImage(diam, diam);

    // Re-draw clipped to circle
    final clipRecorder = ui.PictureRecorder();
    final clipCanvas = Canvas(
      clipRecorder,
      Rect.fromLTWH(0, 0, diam.toDouble(), diam.toDouble()),
    );
    clipCanvas.clipPath(
      Path()..addOval(Rect.fromCircle(center: Offset(c, c), radius: c)),
    );
    clipCanvas.drawImage(raw, Offset.zero, Paint());
    clipCanvas.drawCircle(
      Offset(c, c),
      c,
      Paint()
        ..style = PaintingStyle.stroke
        ..color = Colors.black38
        ..strokeWidth = 2,
    );
    final clipPicture = clipRecorder.endRecording();
    frames.add(await clipPicture.toImage(diam, diam));
    raw.dispose();
  }

  _ballFrames = frames;
  _ballFrameRadius = radius;
}

class _BoingScenePainter extends CustomPainter {
  final double ballX, ballY, phase, ballFrac;

  static const _bgColor = Color(0xFFAAAAAA);
  static const _gridColor = Color(0xFFAA00AA);
  static const _gridDark = Color(0xFF660066);
  static const _shadowColor = Color(0x44000000);

  _BoingScenePainter({
    required this.ballX,
    required this.ballY,
    required this.phase,
    required this.ballFrac,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    final h = size.height;
    final radius = h * ballFrac;

    canvas.drawRect(Offset.zero & size, Paint()..color = _bgColor);
    _drawGrid(canvas, size);

    // Floor
    final floorTop = h * 0.75;
    canvas.drawRect(
      Rect.fromLTWH(0, floorTop, w, h - floorTop),
      Paint()..color = _gridDark.withValues(alpha: 0.25),
    );
    final floorPaint = Paint()
      ..color = _gridColor.withValues(alpha: 0.4)
      ..strokeWidth = 1;
    const floorRows = 5;
    for (int i = 0; i <= floorRows; i++) {
      final y = floorTop + (h - floorTop) * i / floorRows;
      canvas.drawLine(Offset(0, y), Offset(w, y), floorPaint);
    }
    const floorCols = 14;
    final vanishX = w / 2;
    for (int i = 0; i <= floorCols; i++) {
      final fx = w * i / floorCols;
      canvas.drawLine(
        Offset(vanishX + (fx - vanishX) * 0.6, floorTop),
        Offset(fx, h),
        floorPaint,
      );
    }

    final cx = ballX * w;
    final cy = ballY * h;

    // Shadow
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(cx + radius * 0.4, cy + radius * 0.1),
        width: radius * 2.0,
        height: radius * 2.0,
      ),
      Paint()..color = _shadowColor,
    );

    _drawBall(canvas, cx, cy, radius);
  }

  void _drawGrid(Canvas canvas, Size size) {
    final w = size.width;
    final h = size.height * 0.75;
    final paint = Paint()
      ..color = _gridColor
      ..strokeWidth = 1.5;

    const cols = 14;
    for (int i = 0; i <= cols; i++) {
      canvas.drawLine(Offset(w * i / cols, 0), Offset(w * i / cols, h), paint);
    }
    const rows = 10;
    for (int i = 0; i <= rows; i++) {
      canvas.drawLine(Offset(0, h * i / rows), Offset(w, h * i / rows), paint);
    }
  }

  void _drawBall(Canvas canvas, double cx, double cy, double radius) {
    final frames = _ballFrames;
    if (frames == null || frames.isEmpty) return;

    // Quantize phase to frame index
    const nFrames = 56;
    var frameIdx = (phase / 14 * nFrames).floor() % nFrames;
    if (frameIdx < 0) frameIdx += nFrames;

    final img = frames[frameIdx];
    final diam = radius * 2;
    canvas.drawImageRect(
      img,
      Rect.fromLTWH(0, 0, img.width.toDouble(), img.height.toDouble()),
      Rect.fromLTWH(cx - radius, cy - radius, diam, diam),
      Paint(),
    );
  }

  @override
  bool shouldRepaint(covariant _BoingScenePainter old) =>
      ballX != old.ballX || ballY != old.ballY || phase != old.phase;
}
