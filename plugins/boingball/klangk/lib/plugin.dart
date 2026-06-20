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
  final freq = isFloor ? 120.0 : 180.0;
  // Reuse a single AudioContext to avoid exhausting browser resources.
  final code =
      '''
    (function() {
      if (!window._boingCtx) {
        window._boingCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      var ctx = window._boingCtx;
      ctx.resume();
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.type = 'sine';
      osc.frequency.value = $freq;
      gain.gain.value = 0.4;
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start();
      var end = ctx.currentTime + 0.3;
      osc.frequency.exponentialRampToValueAtTime(${freq * 0.3}, end);
      gain.gain.exponentialRampToValueAtTime(0.001, end);
      osc.stop(end + 0.05);
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
    // Per-pixel sphere rendering with screen-space latitude bands.
    // 9 lat bands x 14 lon stripes, half-offset on odd rows.
    // Rendered at 1/3 resolution for performance.
    final diam = (radius * 2).toInt();
    if (diam <= 0) return;

    final res = max(60, diam ~/ 3);
    final step = diam / res;
    const latBands = 9;
    const lonStripes = 14;
    final rotAngle = phase / lonStripes * 2 * pi;
    final cosRot = cos(rotAngle);
    final sinRot = sin(rotAngle);
    final redPaint = Paint()..color = _ballRed;
    final whitePaint = Paint()..color = _ballWhite;

    for (int py = 0; py < res; py++) {
      final ny = (py + 0.5) / res * 2 - 1;
      for (int px = 0; px < res; px++) {
        final nx = (px + 0.5) / res * 2 - 1;
        final r2 = nx * nx + ny * ny;
        if (r2 > 1) continue;

        final nz = sqrt(1 - r2);
        // Y-axis rotation
        final rx = nx * cosRot + nz * sinRot;
        final ry = ny;
        final rz = -nx * sinRot + nz * cosRot;

        // Screen-space latitude (linear in Y for equal-height bands)
        var band = ((ry + 1) / 2 * latBands).floor();
        if (band >= latBands) band = latBands - 1;

        // Longitude
        final phi = atan2(rx, rz) + pi;
        final offset = (band % 2 == 1) ? 0.5 : 0.0;
        var lon = ((phi / (2 * pi) * lonStripes + offset) % lonStripes).floor();

        final isRed = lon % 2 == 0;
        final screenX = cx - radius + px * step;
        final screenY = cy - radius + py * step;
        canvas.drawRect(
          Rect.fromLTWH(screenX, screenY, step + 0.5, step + 0.5),
          isRed ? redPaint : whitePaint,
        );
      }
    }

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
