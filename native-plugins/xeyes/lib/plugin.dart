import 'package:flutter/material.dart';
import 'package:klangk_plugin_api/klangk_plugin_api.dart';

import 'xeyes_overlay.dart';

const xeyesPluginVersion = '2026-06-04-native';

/// Toggleable xeyes overlay plugin.
///
/// The `xeyes` browser action — sent by the Pi `/xeyes` command or the `xeyes`
/// tool (see extension.ts) through the browser-delegate bridge — flips the
/// eyes on/off (or sets an explicit state via `{"on": true|false}`).
/// [buildOverlay] mounts the draggable eye layer over the workspace while
/// enabled.
class XeyesPlugin extends ToolPlugin with ChangeNotifier {
  // Default on so the overlay is visible out of the box; `xeyes` toggles it.
  bool _enabled = true;

  bool get enabled => _enabled;

  @override
  Map<String, ToolHandler> get handlers => {
        'xeyes': _toggle,
      };

  Future<String> _toggle(Map<String, dynamic> request) async {
    final explicit = request['on'];
    _enabled = explicit is bool ? explicit : !_enabled;
    notifyListeners();
    return _enabled ? 'xeyes: eyes on' : 'xeyes: eyes off';
  }

  @override
  Widget? buildOverlay(BuildContext context) {
    return Positioned.fill(
      child: ListenableBuilder(
        listenable: this,
        builder: (_, __) =>
            _enabled ? const XeyesLayer() : const SizedBox.shrink(),
      ),
    );
  }

  @override
  void dispose() {
    // Mixed-in ChangeNotifier cleanup.
    super.dispose();
  }
}
