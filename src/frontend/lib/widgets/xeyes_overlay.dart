import 'dart:math' as math;
import 'package:flutter/material.dart';

/// An `xeyes`-style overlay: a pair of semi-transparent googly eyes painted on
/// top of [child], whose pupils track the mouse cursor. The eye-pair can be
/// dragged around with the mouse.
///
/// Input model: only the small box around the eyes is interactive (drag to
/// move) — everywhere else pointer events fall straight through to the terminal
/// below. Pupil tracking uses a non-opaque [MouseRegion] so the terminal's own
/// hover/cursor regions keep working, and the cursor position lives in a
/// [ValueNotifier] so mouse-move repaints only the eyes, never [child].
class XeyesOverlay extends StatefulWidget {
  final Widget child;

  /// Whether the eyes are shown. Toggleable so it can be turned off.
  final bool enabled;

  const XeyesOverlay({super.key, required this.child, this.enabled = true});

  @override
  State<XeyesOverlay> createState() => _XeyesOverlayState();
}

class _XeyesOverlayState extends State<XeyesOverlay> {
  /// Mouse position in the overlay's local coordinates, or null when the
  /// cursor is outside (eyes look straight ahead).
  final ValueNotifier<Offset?> _mouse = ValueNotifier<Offset?>(null);

  /// Centre of the eye-pair in overlay-local coords. Null until first layout,
  /// then seeded near the top-centre; moved by dragging.
  Offset? _center;

  static const double _sclera = 26;
  static const double _pupil = 10;
  static const double _gap = 14;
  static const double _pad = 4;

  double get _boxW => 4 * _sclera + _gap + 2 * _pad;
  double get _boxH => 2 * _sclera + 2 * _pad;

  @override
  void dispose() {
    _mouse.dispose();
    super.dispose();
  }

  Offset _clampCenter(Offset c, Size size) {
    final hw = _boxW / 2, hh = _boxH / 2;
    return Offset(
      c.dx.clamp(hw, math.max(hw, size.width - hw)),
      c.dy.clamp(hh, math.max(hh, size.height - hh)),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (!widget.enabled) return widget.child;
    return LayoutBuilder(
      builder: (context, constraints) {
        final size = constraints.biggest;
        _center ??= Offset(size.width / 2, math.min(_boxH, size.height / 2));
        final center = _clampCenter(_center!, size);
        final topLeft = center - Offset(_boxW / 2, _boxH / 2);

        return MouseRegion(
          opaque: false,
          onHover: (e) => _mouse.value = e.localPosition,
          onExit: (_) => _mouse.value = null,
          child: Stack(
            children: [
              widget.child,
              Positioned(
                left: topLeft.dx,
                top: topLeft.dy,
                width: _boxW,
                height: _boxH,
                child: MouseRegion(
                  opaque: false,
                  cursor: SystemMouseCursors.grab,
                  child: GestureDetector(
                    behavior: HitTestBehavior.opaque,
                    onPanUpdate: (d) {
                      setState(() {
                        _center = _clampCenter(center + d.delta, size);
                      });
                    },
                    child: RepaintBoundary(
                      child: CustomPaint(
                        size: Size(_boxW, _boxH),
                        painter: _EyesPainter(
                          mouse: _mouse,
                          boxTopLeft: topLeft,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        );
      },
    );
  }
}

class _EyesPainter extends CustomPainter {
  /// Mouse position in overlay-local coords (same space as [boxTopLeft]).
  final ValueNotifier<Offset?> mouse;

  /// Top-left of this painter's box in overlay-local coords, used to convert
  /// the mouse position into the painter's local space.
  final Offset boxTopLeft;

  _EyesPainter({required this.mouse, required this.boxTopLeft})
      : super(repaint: mouse);

  static const double _scleraR = _XeyesOverlayState._sclera;
  static const double _pupilR = _XeyesOverlayState._pupil;
  static const double _gap = _XeyesOverlayState._gap;
  static const double _pad = _XeyesOverlayState._pad;

  @override
  void paint(Canvas canvas, Size size) {
    final left = Offset(_pad + _scleraR, _pad + _scleraR);
    final right = Offset(_pad + 3 * _scleraR + _gap, _pad + _scleraR);

    final sclera = Paint()..color = Colors.white.withValues(alpha: 0.45);
    final rim = Paint()
      ..color = Colors.black.withValues(alpha: 0.35)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2;
    final pupil = Paint()
      ..color = const Color(0xFF1B1B1B).withValues(alpha: 0.65);

    // Mouse converted into this box's local coordinate space.
    final localMouse =
        mouse.value == null ? null : mouse.value! - boxTopLeft;

    for (final eye in [left, right]) {
      canvas.drawCircle(eye, _scleraR, sclera);
      canvas.drawCircle(eye, _scleraR, rim);
      canvas.drawCircle(_pupilOffset(eye, localMouse), _pupilR, pupil);
    }
  }

  Offset _pupilOffset(Offset eye, Offset? target) {
    if (target == null) return eye;
    final dir = target - eye;
    final dist = dir.distance;
    if (dist == 0) return eye;
    final maxTravel = _scleraR - _pupilR;
    return eye + dir / dist * math.min(dist, maxTravel);
  }

  @override
  bool shouldRepaint(covariant _EyesPainter old) =>
      old.boxTopLeft != boxTopLeft;
}
