import 'dart:math';
import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

class BobDobbsFeature extends ToolPlugin with ChangeNotifier {
  bool _show = false;

  @override
  Map<String, ToolHandler> get handlers => {'bobdobbs': _handle};

  Future<String> _handle(Map<String, dynamic> request) async {
    _show = true;
    notifyListeners();
    return '"Bob" says: Slack off! ${request['reason'] ?? ''}';
  }

  @override
  Widget? buildOverlay(BuildContext context) {
    return _BobDobbsOverlay(feature: this);
  }
}

class _BobDobbsOverlay extends StatefulWidget {
  final BobDobbsFeature feature;
  const _BobDobbsOverlay({required this.feature});

  @override
  State<_BobDobbsOverlay> createState() => _BobDobbsOverlayState();
}

class _BobDobbsOverlayState extends State<_BobDobbsOverlay>
    with SingleTickerProviderStateMixin {
  late final AnimationController _controller;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(
      vsync: this,
      duration: const Duration(seconds: 4),
    );
    widget.feature.addListener(_onUpdate);
  }

  @override
  void dispose() {
    widget.feature.removeListener(_onUpdate);
    _controller.dispose();
    super.dispose();
  }

  void _onUpdate() {
    if (widget.feature._show && !_controller.isAnimating) {
      _controller.forward(from: 0);
    }
    if (mounted) setState(() {});
  }

  void _dismiss() {
    widget.feature._show = false;
    widget.feature.notifyListeners();
    _controller.stop();
  }

  @override
  Widget build(BuildContext context) {
    if (!widget.feature._show) return const SizedBox.shrink();
    return Positioned.fill(
      child: GestureDetector(
        onTap: _dismiss,
        child: Container(
          color: Colors.black54,
          child: Center(
            child: AnimatedBuilder(
              animation: _controller,
              builder: (context, child) {
                final angle = _controller.value * 2 * pi;
                return Transform.rotate(angle: angle, child: child);
              },
              child: Image.asset(
                'assets/bobdobbs.png',
                package: 'klangk_feature_bobdobbs',
                width: 200,
                height: 200,
              ),
            ),
          ),
        ),
      ),
    );
  }
}
