import 'dart:convert';
import 'dart:math';

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

void _playBoingSound({required double panX, bool isFloor = true}) {
  final pan = (panX * 2 - 1).clamp(-1.0, 1.0);
  final vol = isFloor ? 0.6 : 0.35;
  final baseF = isFloor ? 80 : 120;
  final endF = isFloor ? 35 : 50;
  final code =
      '''
    (function() {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      var now = ctx.currentTime;
      var dest = ctx.destination;
      var pan = ctx.createStereoPanner();
      pan.pan.value = $pan;
      pan.connect(dest);
      var g = ctx.createGain();
      g.gain.setValueAtTime($vol, now);
      g.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
      g.connect(pan);
      var o = ctx.createOscillator();
      o.frequency.setValueAtTime($baseF, now);
      o.frequency.exponentialRampToValueAtTime($endF, now + 0.25);
      o.connect(g);
      o.start(now);
      o.stop(now + 0.4);
      var o2 = ctx.createOscillator();
      o2.type = 'triangle';
      o2.frequency.setValueAtTime(${baseF * 3}, now);
      o2.frequency.exponentialRampToValueAtTime(${endF * 2}, now + 0.15);
      var g2 = ctx.createGain();
      g2.gain.setValueAtTime(0.15, now);
      g2.gain.exponentialRampToValueAtTime(0.001, now + 0.2);
      o2.connect(g2);
      g2.connect(g);
      o2.start(now);
      o2.stop(now + 0.25);
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
  static const double _gravity = 0.00015;
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

  void _startAnimation() {
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

      _phase += 0.09 * _spinDir * speed;
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
      child: GestureDetector(
        onTap: _dismiss,
        behavior: HitTestBehavior.opaque,
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
      ),
    );
  }
}

// ---------- scene painter ----------

class _BoingScenePainter extends CustomPainter {
  final double ballX, ballY, phase, ballFrac;

  static const _bgColor = Color(0xFFAAAAAA);
  static const _gridColor = Color(0xFFAA00AA);
  static const _gridDark = Color(0xFF660066);
  static const _ballRed = Color(0xFFFF0000);
  static const _ballWhite = Color(0xFFFFFFFF);
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
    // Boing Ball checkerboard: 8 latitude rows x 14 longitude stripes.
    // Odd rows are offset by half a stripe width so red corners touch
    // red corners diagonally (and same for white).

    canvas.save();
    canvas.clipPath(
      Path()..addOval(Rect.fromCircle(center: Offset(cx, cy), radius: radius)),
    );

    // White base
    canvas.drawCircle(Offset(cx, cy), radius, Paint()..color = _ballWhite);

    const latRows = 8;
    const lonStripes = 14;
    const subDiv = 6; // subdivisions per edge for smooth curvature
    final rotAngle = phase / lonStripes * 2 * pi;
    final redPaint = Paint()..color = _ballRed;

    for (int row = 0; row < latRows; row++) {
      final theta0 = pi * row / latRows;
      final theta1 = pi * (row + 1) / latRows;
      final y0 = cy - cos(theta0) * radius;
      final y1 = cy - cos(theta1) * radius;
      final rSlice0 = sin(theta0) * radius;
      final rSlice1 = sin(theta1) * radius;

      // Half-stripe offset on odd rows creates the diagonal pattern
      final offset = (row % 2 == 0) ? 0.0 : 0.5;

      for (int col = 0; col < lonStripes; col++) {
        // Only draw red squares (checkerboard)
        if (col % 2 != 0) continue;

        final phi0 = 2 * pi * (col + offset) / lonStripes + rotAngle;
        final phi1 = 2 * pi * (col + 1 + offset) / lonStripes + rotAngle;

        // Skip back-facing
        if (cos((phi0 + phi1) / 2) < -0.2) continue;

        // Build curved quad: top edge L→R, bottom edge R→L
        final path = Path();

        // Top edge with subdivisions
        for (int s = 0; s <= subDiv; s++) {
          final t = s / subDiv;
          final phi = phi0 + (phi1 - phi0) * t;
          final x = cx + sin(phi) * rSlice0;
          if (s == 0) {
            path.moveTo(x, y0);
          } else {
            path.lineTo(x, y0);
          }
        }
        // Bottom edge with subdivisions (reverse)
        for (int s = subDiv; s >= 0; s--) {
          final t = s / subDiv;
          final phi = phi0 + (phi1 - phi0) * t;
          path.lineTo(cx + sin(phi) * rSlice1, y1);
        }
        path.close();
        canvas.drawPath(path, redPaint);
      }
    }

    canvas.restore();

    // Specular highlight
    canvas.drawCircle(
      Offset(cx - radius * 0.3, cy - radius * 0.3),
      radius * 0.22,
      Paint()
        ..color = Colors.white.withValues(alpha: 0.45)
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 16),
    );

    // Outline
    canvas.drawCircle(
      Offset(cx, cy),
      radius,
      Paint()
        ..style = PaintingStyle.stroke
        ..color = Colors.black38
        ..strokeWidth = 2,
    );
  }

  @override
  bool shouldRepaint(covariant _BoingScenePainter old) =>
      ballX != old.ballX || ballY != old.ballY || phase != old.phase;
}
