import 'package:flutter/widgets.dart';

import 'web_helpers_stub.dart' if (dart.library.html) 'web_helpers_web.dart';

/// Suppresses the browser's native context menu within this widget's area.
class SuppressBrowserContextMenu extends StatelessWidget {
  final Widget child;

  const SuppressBrowserContextMenu({super.key, required this.child});

  @override
  Widget build(BuildContext context) {
    return buildSuppressor(child);
  }
}
