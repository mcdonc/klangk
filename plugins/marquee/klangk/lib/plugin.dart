import 'dart:convert';
import 'dart:math';

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

class MarqueePlugin extends ToolPlugin with ChangeNotifier {
  String? _text;
  String _defaultText = 'Hello from Klangk!';
  double _speed = 50;
  bool _configLoaded = false;

  @override
  Map<String, ToolHandler> get handlers => {'marquee': _handle};

  Future<String> _handle(Map<String, dynamic> request) async {
    if (!_configLoaded) await _loadConfig();
    _text = (request['text'] as String?) ?? _defaultText;
    notifyListeners();
    return 'Marquee triggered: $_text';
  }

  Future<void> _loadConfig() async {
    _configLoaded = true;
    try {
      final resp = await http.get(Uri.parse('${baseUrl}/api/config'));
      if (resp.statusCode == 200) {
        final data = jsonDecode(resp.body) as Map<String, dynamic>;
        final text = data['klangk_marquee_text'] as String?;
        if (text != null && text.isNotEmpty) _defaultText = text;
        final speed = data['klangk_marquee_speed'] as String?;
        if (speed != null && speed.isNotEmpty) {
          _speed = double.tryParse(speed) ?? 50;
        }
      }
    } catch (_) {
      // Config unavailable — use defaults
    }
  }

  @override
  Widget? buildOverlay(BuildContext context) {
    return _MarqueeOverlay(plugin: this);
  }
}

class _MarqueeOverlay extends StatefulWidget {
  final MarqueePlugin plugin;
  const _MarqueeOverlay({required this.plugin});

  @override
  State<_MarqueeOverlay> createState() => _MarqueeOverlayState();
}

class _MarqueeOverlayState extends State<_MarqueeOverlay>
    with TickerProviderStateMixin {
  late AnimationController _scrollCtrl;
  late AnimationController _rainbowCtrl;
  late AnimationController _pulseCtrl;
  late AnimationController _fadeCtrl;
  late AnimationController _sparkleCtrl;
  String? _text;
  final _rng = Random();
  List<_Sparkle> _sparkles = [];

  @override
  void initState() {
    super.initState();

    _scrollCtrl = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 8),
    );
    _rainbowCtrl = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 2),
    )..repeat();
    _pulseCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 800),
    )..repeat(reverse: true);
    _fadeCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 500),
      value: 1.0,
    );
    _sparkleCtrl = AnimationController(
      vsync: this,
      duration: const Duration(milliseconds: 100),
    )..repeat();
    _sparkleCtrl.addListener(_updateSparkles);

    widget.plugin.addListener(_onUpdate);
  }

  @override
  void dispose() {
    widget.plugin.removeListener(_onUpdate);
    _scrollCtrl.dispose();
    _rainbowCtrl.dispose();
    _pulseCtrl.dispose();
    _fadeCtrl.dispose();
    _sparkleCtrl.removeListener(_updateSparkles);
    _sparkleCtrl.dispose();
    super.dispose();
  }

  void _onUpdate() {
    if (!mounted) return;
    setState(() {
      _text = widget.plugin._text;
    });
    if (_text != null) _startAnimation();
  }

  void _startAnimation() {
    _sparkles = List.generate(30, (_) => _Sparkle.random(_rng));
    _fadeCtrl.value = 1.0;
    _scrollCtrl.reset();

    final textLen = (_text?.length ?? 10) * 24.0;
    final screenWidth = MediaQuery.of(context).size.width;
    final totalDistance = screenWidth + textLen + 200;
    final durationMs = (totalDistance / widget.plugin._speed * 1000).round();
    _scrollCtrl.duration = Duration(milliseconds: max(durationMs, 2000));

    _scrollCtrl.forward().then((_) {
      _fadeCtrl.reverse().then((_) {
        if (mounted) {
          setState(() {
            _text = null;
            widget.plugin._text = null;
          });
        }
      });
    });
  }

  void _updateSparkles() {
    if (!mounted || _text == null) return;
    setState(() {
      for (final s in _sparkles) {
        s.life -= 0.02;
        s.y += s.vy;
        s.x += s.vx;
        if (s.life <= 0) {
          final ns = _Sparkle.random(_rng);
          s.x = ns.x;
          s.y = ns.y;
          s.vx = ns.vx;
          s.vy = ns.vy;
          s.life = ns.life;
          s.hue = ns.hue;
          s.size = ns.size;
        }
      }
    });
  }

  @override
  Widget build(BuildContext context) {
    if (_text == null) return const SizedBox.shrink();
    return Positioned(
      top: 0,
      left: 0,
      right: 0,
      child: FadeTransition(
        opacity: _fadeCtrl,
        child: AnimatedBuilder(
          animation: Listenable.merge([
            _scrollCtrl,
            _rainbowCtrl,
            _pulseCtrl,
            _sparkleCtrl,
          ]),
          builder: (context, _) => _buildBanner(context),
        ),
      ),
    );
  }

  Widget _buildBanner(BuildContext context) {
    final width = MediaQuery.of(context).size.width;
    final textLen = (_text?.length ?? 10) * 24.0;
    final xPos = width - _scrollCtrl.value * (width + textLen + 200);
    final pulse = 1.0 + _pulseCtrl.value * 0.15;
    final hueShift = _rainbowCtrl.value * 360;

    return ClipRect(
      child: SizedBox(
        height: 80,
        child: Stack(
          children: [
            // Animated gradient background
            Positioned.fill(
              child: DecoratedBox(
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    colors: [
                      HSLColor.fromAHSL(
                        0.9,
                        hueShift % 360,
                        0.8,
                        0.15,
                      ).toColor(),
                      HSLColor.fromAHSL(
                        0.9,
                        (hueShift + 120) % 360,
                        0.8,
                        0.1,
                      ).toColor(),
                      HSLColor.fromAHSL(
                        0.9,
                        (hueShift + 240) % 360,
                        0.8,
                        0.15,
                      ).toColor(),
                    ],
                  ),
                ),
              ),
            ),
            // Sparkle particles
            for (final s in _sparkles)
              if (s.life > 0)
                Positioned(
                  left: s.x * width,
                  top: s.y * 80,
                  child: Opacity(
                    opacity: (s.life).clamp(0.0, 1.0),
                    child: Text(
                      '*',
                      style: TextStyle(
                        fontSize: s.size,
                        color: HSLColor.fromAHSL(1, s.hue, 1, 0.7).toColor(),
                        fontWeight: FontWeight.bold,
                      ),
                    ),
                  ),
                ),
            // Scrolling rainbow text with glow
            Positioned(
              left: xPos,
              top: 0,
              bottom: 0,
              child: Center(
                child: Transform.scale(
                  scale: pulse,
                  child: _RainbowText(text: _text!, hueOffset: hueShift),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _RainbowText extends StatelessWidget {
  final String text;
  final double hueOffset;
  const _RainbowText({required this.text, required this.hueOffset});

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        for (int i = 0; i < text.length; i++)
          Text(
            text[i],
            style: TextStyle(
              fontSize: 36,
              fontWeight: FontWeight.w900,
              color: HSLColor.fromAHSL(
                1,
                (hueOffset + i * 25) % 360,
                1,
                0.6,
              ).toColor(),
              shadows: [
                Shadow(
                  color: HSLColor.fromAHSL(
                    0.8,
                    (hueOffset + i * 25) % 360,
                    1,
                    0.5,
                  ).toColor(),
                  blurRadius: 20,
                ),
                Shadow(
                  color: HSLColor.fromAHSL(
                    0.4,
                    (hueOffset + i * 25 + 180) % 360,
                    1,
                    0.5,
                  ).toColor(),
                  blurRadius: 40,
                ),
              ],
            ),
          ),
      ],
    );
  }
}

class _Sparkle {
  double x, y, vx, vy, life, hue, size;
  _Sparkle({
    required this.x,
    required this.y,
    required this.vx,
    required this.vy,
    required this.life,
    required this.hue,
    required this.size,
  });

  factory _Sparkle.random(Random rng) => _Sparkle(
    x: rng.nextDouble(),
    y: rng.nextDouble(),
    vx: (rng.nextDouble() - 0.5) * 0.01,
    vy: (rng.nextDouble() - 0.5) * 0.01,
    life: 0.5 + rng.nextDouble() * 0.5,
    hue: rng.nextDouble() * 360,
    size: 8 + rng.nextDouble() * 16,
  );
}
