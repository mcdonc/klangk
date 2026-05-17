import 'package:flutter/material.dart';
import 'package:bark_frontend/tools/tool_plugin.dart';
import 'confetti.dart';

class CelebratePlugin extends ToolPlugin with ChangeNotifier {
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
    return _CelebrateOverlay(plugin: this);
  }
}

class _CelebrateOverlay extends StatefulWidget {
  final CelebratePlugin plugin;
  const _CelebrateOverlay({required this.plugin});

  @override
  State<_CelebrateOverlay> createState() => _CelebrateOverlayState();
}

class _CelebrateOverlayState extends State<_CelebrateOverlay> {
  @override
  void initState() {
    super.initState();
    widget.plugin.addListener(_onUpdate);
  }

  @override
  void dispose() {
    widget.plugin.removeListener(_onUpdate);
    super.dispose();
  }

  void _onUpdate() {
    if (mounted) setState(() {});
  }

  @override
  Widget build(BuildContext context) {
    if (!widget.plugin._showConfetti) return const SizedBox.shrink();
    return Positioned.fill(
      child: ConfettiOverlay(
        onComplete: () {
          widget.plugin._showConfetti = false;
          widget.plugin.notifyListeners();
        },
      ),
    );
  }
}
