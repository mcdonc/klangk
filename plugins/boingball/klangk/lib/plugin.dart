import 'dart:convert';
import 'dart:math';
import 'dart:ui' as ui;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

class BoingBallPlugin extends ToolPlugin with ChangeNotifier {
  String? _text;
  String _defaultText = 'Klangk';
  double _speed = 1.0;
  bool _configLoaded = false;

  @override
  Map<String, ToolHandler> get handlers => {'boing': _handle};

  Future<String> _handle(Map<String, dynamic> request) async {
    if (!_configLoaded) await _loadConfig();
    _text = (request['text'] as String?) ?? _defaultText;
    notifyListeners();
    return 'Boing ball triggered: $_text';
  }

  Future<void> _loadConfig() async {
    _configLoaded = true;
    try {
      final resp = await http.get(Uri.parse('$baseUrl/api/config'));
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as Map<String, dynamic>;
        final text = data['klangk_boing_text'] as String?;
        if (text != null && text.isNotEmpty) _defaultText = text;
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
  String? _text;

  // Ball physics (normalised 0..1 coordinate space)
  double _x = 0.5;
  double _y = 0.1;
  double _vx = 0.004;
  double _vy = 0.0;
  double _phase = 0; // rotation phase 0..14
  int _spinDir = 1; // +1 or -1
  static const double _gravity = 0.00025;
  static const double _damping = 0.97;
  static const double _ballFrac = 0.18; // ball radius as fraction of height

  static const _durationSec = 12;

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
    setState(() => _text = widget.plugin._text);
    if (_text != null) _startAnimation();
  }

  void _startAnimation() {
    _x = 0.15;
    _y = 0.15;
    _vx = 0.005 * widget.plugin._speed;
    _vy = 0.0;
    _phase = 0;
    _spinDir = 1;
    _ctrl.reset();
    _ctrl.forward().then((_) {
      if (mounted) _dismiss();
    });
  }

  bool _onKey(KeyEvent event) {
    if (_text != null &&
        event is KeyDownEvent &&
        event.logicalKey == LogicalKeyboardKey.escape) {
      _dismiss();
      return true;
    }
    return false;
  }

  void _dismiss() {
    _ctrl.stop();
    if (mounted) {
      setState(() {
        _text = null;
        widget.plugin._text = null;
      });
    }
  }

  void _tick() {
    if (!mounted || _text == null) return;
    final speed = widget.plugin._speed;
    setState(() {
      // Gravity
      _vy += _gravity * speed;
      _x += _vx * speed;
      _y += _vy * speed;

      // Floor bounce
      if (_y >= 1.0 - _ballFrac) {
        _y = 1.0 - _ballFrac;
        _vy = -_vy.abs() * _damping;
      }
      // Ceiling
      if (_y <= _ballFrac * 0.3) {
        _y = _ballFrac * 0.3;
        _vy = _vy.abs();
      }
      // Wall bounces
      if (_x <= _ballFrac * 0.5) {
        _x = _ballFrac * 0.5;
        _vx = _vx.abs();
        _spinDir = 1;
      }
      if (_x >= 1.0 - _ballFrac * 0.5) {
        _x = 1.0 - _ballFrac * 0.5;
        _vx = -_vx.abs();
        _spinDir = -1;
      }

      // Rotation phase (14 stripes, wraps)
      _phase += 0.35 * _spinDir * speed;
      if (_phase < 0) _phase += 14;
      if (_phase >= 14) _phase -= 14;
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_text == null) return const SizedBox.shrink();
    return Positioned.fill(
      child: GestureDetector(
        onTap: _dismiss,
        child: CustomPaint(
          painter: _BoingScenePainter(
            ballX: _x,
            ballY: _y,
            phase: _phase,
            ballFrac: _ballFrac,
            text: _text!,
          ),
          child: const SizedBox.expand(),
        ),
      ),
    );
  }
}

// ---------- scene painter ----------

class _BoingScenePainter extends CustomPainter {
  final double ballX, ballY, phase, ballFrac;
  final String text;

  // Faithful palette
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
    required this.text,
  });

  @override
  void paint(Canvas canvas, Size size) {
    final w = size.width;
    final h = size.height;
    final radius = h * ballFrac;

    // Background
    canvas.drawRect(Offset.zero & size, Paint()..color = _bgColor);

    // Back wall grid
    _drawGrid(canvas, size);

    // Floor (darker region at bottom)
    final floorTop = h * 0.75;
    canvas.drawRect(
      Rect.fromLTWH(0, floorTop, w, h - floorTop),
      Paint()..color = _gridDark.withValues(alpha: 0.25),
    );
    // Floor grid lines
    final floorPaint = Paint()
      ..color = _gridColor.withValues(alpha: 0.4)
      ..strokeWidth = 1;
    const floorRows = 5;
    for (int i = 0; i <= floorRows; i++) {
      final t = i / floorRows;
      final y = floorTop + (h - floorTop) * t;
      canvas.drawLine(Offset(0, y), Offset(w, y), floorPaint);
    }
    const floorCols = 14;
    final vanishX = w / 2;
    for (int i = 0; i <= floorCols; i++) {
      final fx = w * i / floorCols;
      // Perspective: lines converge toward vanishing point at the back
      canvas.drawLine(
        Offset(vanishX + (fx - vanishX) * 0.6, floorTop),
        Offset(fx, h),
        floorPaint,
      );
    }

    // Shadow on back wall
    final cx = ballX * w;
    final cy = ballY * h;
    final shadowX = cx + radius * 0.4;
    final shadowY = cy + radius * 0.1;
    canvas.drawOval(
      Rect.fromCenter(
        center: Offset(shadowX, shadowY),
        width: radius * 2.0,
        height: radius * 2.0,
      ),
      Paint()..color = _shadowColor,
    );

    // Ball
    _drawBall(canvas, cx, cy, radius);

    // Text label
    final textPainter = TextPainter(
      text: TextSpan(
        text: text,
        style: TextStyle(
          color: _ballWhite,
          fontSize: radius * 0.35,
          fontWeight: FontWeight.w900,
          letterSpacing: 3,
          shadows: const [
            Shadow(color: Colors.black54, blurRadius: 4, offset: Offset(2, 2)),
          ],
        ),
      ),
      textDirection: TextDirection.ltr,
    )..layout();
    textPainter.paint(
      canvas,
      Offset(cx - textPainter.width / 2, cy + radius + radius * 0.15),
    );
  }

  void _drawGrid(Canvas canvas, Size size) {
    final w = size.width;
    final h = size.height * 0.75; // back wall only
    final paint = Paint()
      ..color = _gridColor
      ..strokeWidth = 1.5;

    // Vertical lines
    const cols = 14;
    for (int i = 0; i <= cols; i++) {
      final x = w * i / cols;
      canvas.drawLine(Offset(x, 0), Offset(x, h), paint);
    }
    // Horizontal lines
    const rows = 10;
    for (int i = 0; i <= rows; i++) {
      final y = h * i / rows;
      canvas.drawLine(Offset(0, y), Offset(w, y), paint);
    }
  }

  void _drawBall(Canvas canvas, double cx, double cy, double radius) {
    // Per-pixel sphere rendering with checker UV mapping
    final ballRect = Rect.fromCircle(center: Offset(cx, cy), radius: radius);

    // Clip to circle
    canvas.save();
    canvas.clipPath(Path()..addOval(ballRect));

    // Render checker sphere
    // For performance, render at reduced resolution and scale up
    final imgSize = (radius * 2).ceil();
    if (imgSize <= 0) {
      canvas.restore();
      return;
    }

    const latBands = 8;
    const lonStripes = 14;
    final rotAngle = phase / lonStripes * 2 * pi;
    // Axis tilt ~17 degrees
    const tilt = 17 * pi / 180;

    // Draw checker quads as arcs for each latitude band
    for (int lat = 0; lat < latBands; lat++) {
      final theta0 = pi * (lat + 0.5) / (latBands + 1);
      final theta1 = pi * (lat + 1.5) / (latBands + 1);
      final y0 = cos(theta0);
      final y1 = cos(theta1);
      final screenY0 = cy - y0 * radius;
      final screenY1 = cy - y1 * radius;

      // Radius of the circle at this latitude
      final sinT0 = sin(theta0);
      final sinT1 = sin(theta1);
      final rSlice0 = sinT0 * radius;
      final rSlice1 = sinT1 * radius;

      for (int lon = 0; lon < lonStripes; lon++) {
        // Half-stripe offset for diagonal pattern
        final offset = (lat % 2 == 0) ? 0.0 : 0.5;
        final isRed = (lon % 2 == 0);
        final color = isRed ? _ballRed : _ballWhite;

        final phi0 = 2 * pi * (lon + offset) / lonStripes + rotAngle;
        final phi1 = 2 * pi * (lon + 1 + offset) / lonStripes + rotAngle;

        // Only draw front-facing segments
        final midPhi = (phi0 + phi1) / 2;
        if (cos(midPhi) < -0.1) continue; // back face

        // Project to screen
        final x0t = cx + sin(phi0) * rSlice0;
        final x1t = cx + sin(phi1) * rSlice0;
        final x0b = cx + sin(phi0) * rSlice1;
        final x1b = cx + sin(phi1) * rSlice1;

        final path = Path()
          ..moveTo(x0t, screenY0)
          ..lineTo(x1t, screenY0)
          ..lineTo(x1b, screenY1)
          ..lineTo(x0b, screenY1)
          ..close();

        canvas.drawPath(path, Paint()..color = color);
      }
    }

    canvas.restore();

    // Highlight (specular)
    canvas.drawCircle(
      Offset(cx - radius * 0.3, cy - radius * 0.3),
      radius * 0.18,
      Paint()
        ..color = Colors.white.withValues(alpha: 0.5)
        ..maskFilter = const MaskFilter.blur(BlurStyle.normal, 12),
    );

    // Outline
    canvas.drawCircle(
      Offset(cx, cy),
      radius,
      Paint()
        ..style = PaintingStyle.stroke
        ..color = Colors.black26
        ..strokeWidth = 1.5,
    );
  }

  @override
  bool shouldRepaint(covariant _BoingScenePainter old) =>
      ballX != old.ballX ||
      ballY != old.ballY ||
      phase != old.phase ||
      text != old.text;
}
