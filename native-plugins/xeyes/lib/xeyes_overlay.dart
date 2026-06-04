import 'dart:math' as math;
import 'package:flutter/material.dart';

// Eye geometry.
const double _scleraR = 26;
const double _pupilR = 10;
const double _gap = 14;
const double _pad = 4;
const double _boxW = 4 * _scleraR + _gap + 2 * _pad;
const double _boxH = 2 * _scleraR + 2 * _pad;

/// A standalone `xeyes` layer that fills its parent: draggable, semi-transparent
/// googly eyes whose pupils track the mouse. Mount it inside a [Positioned.fill]
/// over the workspace.
///
/// Input model: only the small eye box is interactive (drag to move, grab
/// cursor) — everywhere else pointer events fall through to the widgets below.
/// The [MouseRegion] is non-opaque so the terminal's own hover/cursor regions
/// keep working, and the cursor position lives in a [ValueNotifier] so
/// mouse-move repaints only the eyes.
class XeyesLayer extends StatefulWidget {
  const XeyesLayer({super.key});

  @override
  State<XeyesLayer> createState() => _XeyesLayerState();
}

class _XeyesLayerState extends State<XeyesLayer> {
  final ValueNotifier<Offset?> _mouse = ValueNotifier<Offset?>(null);

  /// Centre of the eye-pair in local coords; null until first layout, then
  /// seeded near the top-centre and moved by dragging.
  Offset? _center;

  @override
  void dispose() {
    _mouse.dispose();
    super.dispose();
  }

  Offset _clamp(Offset c, Size size) {
    const hw = _boxW / 2, hh = _boxH / 2;
    return Offset(
      c.dx.clamp(hw, math.max(hw, size.width - hw)),
      c.dy.clamp(hh, math.max(hh, size.height - hh)),
    );
  }

  @override
  Widget build(BuildContext context) {
    return MouseRegion(
      opaque: false,
      onHover: (e) => _mouse.value = e.localPosition,
      onExit: (_) => _mouse.value = null,
      child: LayoutBuilder(
        builder: (context, constraints) {
          final size = constraints.biggest;
          _center ??= Offset(size.width / 2, math.min(_boxH, size.height / 2));
          final center = _clamp(_center!, size);
          final topLeft = center - const Offset(_boxW / 2, _boxH / 2);

          return Stack(
            children: [
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
                      setState(() => _center = _clamp(center + d.delta, size));
                    },
                    child: RepaintBoundary(
                      child: CustomPaint(
                        size: const Size(_boxW, _boxH),
                        painter: _EyesPainter(mouse: _mouse, boxTopLeft: topLeft),
                      ),
                    ),
                  ),
                ),
              ),
            ],
          );
        },
      ),
    );
  }
}

class _EyesPainter extends CustomPainter {
  /// Mouse position in the layer's local coords (same space as [boxTopLeft]).
  final ValueNotifier<Offset?> mouse;

  /// Top-left of this painter's box in the layer's local coords, used to map
  /// the mouse position into the painter's local space.
  final Offset boxTopLeft;

  _EyesPainter({required this.mouse, required this.boxTopLeft})
      : super(repaint: mouse);

  @override
  void paint(Canvas canvas, Size size) {
    final left = const Offset(_pad + _scleraR, _pad + _scleraR);
    final right = const Offset(_pad + 3 * _scleraR + _gap, _pad + _scleraR);

    final sclera = Paint()..color = Colors.white.withValues(alpha: 0.45);
    final rim = Paint()
      ..color = Colors.black.withValues(alpha: 0.35)
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2;
    final pupil = Paint()
      ..color = const Color(0xFF1B1B1B).withValues(alpha: 0.65);

    final localMouse = mouse.value == null ? null : mouse.value! - boxTopLeft;

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
    const maxTravel = _scleraR - _pupilR;
    return eye + dir / dist * math.min(dist, maxTravel);
  }

  @override
  bool shouldRepaint(covariant _EyesPainter old) =>
      old.boxTopLeft != boxTopLeft;
}
