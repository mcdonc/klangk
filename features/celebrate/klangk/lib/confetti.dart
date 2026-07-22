import 'dart:math';
import 'package:flutter/material.dart';

/// A confetti overlay that animates colorful particles falling down.
class ConfettiOverlay extends StatefulWidget {
  final VoidCallback onComplete;

  const ConfettiOverlay({super.key, required this.onComplete});

  @override
  State<ConfettiOverlay> createState() => _ConfettiOverlayState();
}

class _ConfettiOverlayState extends State<ConfettiOverlay>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;
  late final List<_Particle> _particles;
  final _random = Random();

  static const _colors = [
    Colors.red,
    Colors.blue,
    Colors.green,
    Colors.orange,
    Colors.purple,
    Colors.pink,
    Colors.yellow,
    Colors.teal,
    Colors.cyan,
  ];

  @override
  void initState() {
    super.initState();
    _particles = List.generate(80, (_) => _Particle(_random));
    _controller =
        AnimationController(vsync: this, duration: const Duration(seconds: 3))
          ..addListener(() => setState(() {}))
          ..addStatusListener((status) {
            if (status == AnimationStatus.completed) {
              widget.onComplete();
            }
          })
          ..forward();
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return IgnorePointer(
      child: CustomPaint(
        painter: _ConfettiPainter(
          particles: _particles,
          progress: _controller.value,
        ),
        size: Size.infinite,
      ),
    );
  }
}

class _Particle {
  final double x; // 0-1 horizontal position
  final double startY; // starting Y offset (-0.2 to 0)
  final double speed; // fall speed multiplier
  final double wobbleSpeed;
  final double wobbleAmount;
  final double size;
  final Color color;
  final double rotation;

  _Particle(Random r)
    : x = r.nextDouble(),
      startY = -r.nextDouble() * 0.3,
      speed = 0.5 + r.nextDouble() * 0.8,
      wobbleSpeed = 2 + r.nextDouble() * 6,
      wobbleAmount = 10 + r.nextDouble() * 20,
      size = 4 + r.nextDouble() * 8,
      color = _ConfettiOverlayState
          ._colors[r.nextInt(_ConfettiOverlayState._colors.length)],
      rotation = r.nextDouble() * pi * 2;
}

class _ConfettiPainter extends CustomPainter {
  final List<_Particle> particles;
  final double progress;

  _ConfettiPainter({required this.particles, required this.progress});

  @override
  void paint(Canvas canvas, Size size) {
    final opacity = progress < 0.7 ? 1.0 : (1.0 - progress) / 0.3;

    for (final p in particles) {
      final y = p.startY + progress * p.speed * 1.3;
      if (y > 1.2) continue;

      final wobble = sin(progress * p.wobbleSpeed * pi * 2) * p.wobbleAmount;
      final px = p.x * size.width + wobble;
      final py = y * size.height;

      final paint = Paint()
        ..color = p.color.withOpacity(opacity.clamp(0.0, 1.0))
        ..style = PaintingStyle.fill;

      canvas.save();
      canvas.translate(px, py);
      canvas.rotate(p.rotation + progress * 5);
      canvas.drawRect(
        Rect.fromCenter(
          center: Offset.zero,
          width: p.size,
          height: p.size * 0.6,
        ),
        paint,
      );
      canvas.restore();
    }
  }

  @override
  bool shouldRepaint(covariant _ConfettiPainter old) =>
      old.progress != progress;
}
