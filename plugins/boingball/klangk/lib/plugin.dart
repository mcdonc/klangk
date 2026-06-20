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

/// Synthesize a metallic boing via Web Audio API eval (same pattern as beep plugin).
void _playBoingSound({required double panX, bool isFloor = true}) {
  final pan = (panX * 2 - 1).clamp(-1.0, 1.0);
  final vol = isFloor ? 0.5 : 0.3;
  final rate = isFloor ? 1.0 : 1.3;
  // Three detuned oscillators for metallic character + stereo pan
  final code =
      '''
    (function() {
      var ctx = new (window.AudioContext || window.webkitAudioContext)();
      var now = ctx.currentTime;
      var pan = ctx.createStereoPanner();
      pan.pan.value = $pan;
      pan.connect(ctx.destination);
      var master = ctx.createGain();
      master.gain.setValueAtTime($vol, now);
      master.gain.exponentialRampToValueAtTime(0.001, now + 0.25);
      master.connect(pan);
      [${180 * rate}, ${340 * rate}, ${720 * rate}].forEach(function(f) {
        var osc = ctx.createOscillator();
        var g = ctx.createGain();
        osc.frequency.setValueAtTime(f, now);
        osc.frequency.exponentialRampToValueAtTime(f * 0.35, now + 0.15);
        g.gain.setValueAtTime(0.3, now);
        g.gain.exponentialRampToValueAtTime(0.001, now + 0.2);
        osc.connect(g);
        g.connect(master);
        osc.start(now);
        osc.stop(now + 0.25);
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
  static const double _gravity = 0.00025;
  static const double _damping = 0.97;
  static const double _ballFrac = 0.33;
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
    if (widget.plugin._active) {
      widget.plugin._active = false;
      _startAnimation();
    }
  }

  void _startAnimation() {
    setState(() => _visible = true);
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
    setState(() {
      _vy += _gravity * speed;
      _x += _vx * speed;
      _y += _vy * speed;

      if (_y >= 0.85) {
        _y = 0.85;
        _vy = -_vy.abs() * _damping;
        _playBoingSound(panX: _x, isFloor: true);
      }
      if (_y <= 0.05) {
        _y = 0.05;
        _vy = _vy.abs();
      }
      if (_x <= 0.08) {
        _x = 0.08;
        _vx = _vx.abs();
        _spinDir = 1;
        _playBoingSound(panX: _x, isFloor: false);
      }
      if (_x >= 0.92) {
        _x = 0.92;
        _vx = -_vx.abs();
        _spinDir = -1;
        _playBoingSound(panX: _x, isFloor: false);
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
    return Positioned(
      left: insetX,
      top: insetY,
      width: size.width - insetX * 2,
      height: size.height - insetY * 2,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(12),
        child: GestureDetector(
          onTap: _dismiss,
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
    // Use height so the ball is always proportional to the scene
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
    canvas.save();
    canvas.clipPath(
      Path()..addOval(Rect.fromCircle(center: Offset(cx, cy), radius: radius)),
    );

    const latBands = 8;
    const lonStripes = 14;
    final rotAngle = phase / lonStripes * 2 * pi;

    for (int lat = 0; lat < latBands; lat++) {
      final theta0 = pi * (lat + 0.5) / (latBands + 1);
      final theta1 = pi * (lat + 1.5) / (latBands + 1);
      final screenY0 = cy - cos(theta0) * radius;
      final screenY1 = cy - cos(theta1) * radius;
      final rSlice0 = sin(theta0) * radius;
      final rSlice1 = sin(theta1) * radius;

      for (int lon = 0; lon < lonStripes; lon++) {
        final offset = (lat % 2 == 0) ? 0.0 : 0.5;
        final color = (lon % 2 == 0) ? _ballRed : _ballWhite;

        final phi0 = 2 * pi * (lon + offset) / lonStripes + rotAngle;
        final phi1 = 2 * pi * (lon + 1 + offset) / lonStripes + rotAngle;

        if (cos((phi0 + phi1) / 2) < -0.1) continue;

        canvas.drawPath(
          Path()
            ..moveTo(cx + sin(phi0) * rSlice0, screenY0)
            ..lineTo(cx + sin(phi1) * rSlice0, screenY0)
            ..lineTo(cx + sin(phi1) * rSlice1, screenY1)
            ..lineTo(cx + sin(phi0) * rSlice1, screenY1)
            ..close(),
          Paint()..color = color,
        );
      }
    }

    canvas.restore();

    // Specular highlight
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
      ballX != old.ballX || ballY != old.ballY || phase != old.phase;
}
