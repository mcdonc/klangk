import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';
import 'confetti.dart';

class CelebrateFeature extends ToolPlugin with ChangeNotifier {
  bool _showConfetti = false;

  @override
  Map<String, ToolHandler> get handlers => {'celebrate': _handle};

  Future<String> _handle(Map<String, dynamic> request) async {
    _showConfetti = true;
    notifyListeners();
    return 'Celebration triggered! ${request['reason'] ?? ''}';
  }

  @override
  Widget? buildOverlay(BuildContext context) {
    return _CelebrateOverlay(feature: this);
  }
}

class _CelebrateOverlay extends StatefulWidget {
  final CelebrateFeature feature;
  const _CelebrateOverlay({required this.feature});

  @override
  State<_CelebrateOverlay> createState() => _CelebrateOverlayState();
}

class _CelebrateOverlayState extends State<_CelebrateOverlay> {
  @override
  void initState() {
    super.initState();
    widget.feature.addListener(_onUpdate);
  }

  @override
  void dispose() {
    widget.feature.removeListener(_onUpdate);
    super.dispose();
  }

  void _onUpdate() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    if (!widget.feature._showConfetti) return const SizedBox.shrink();
    return Positioned.fill(
      child: ConfettiOverlay(
        onComplete: () {
          widget.feature._showConfetti = false;
          widget.feature.notifyListeners();
        },
      ),
    );
  }
}
