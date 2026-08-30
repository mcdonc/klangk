import 'package:flutter/material.dart';

/// Password-visibility ("eye") toggle for an [InputDecoration] suffixIcon.
///
/// Deliberately skipped by Tab traversal (#2893): tabbing through a form
/// moves only between input fields — the eye stays mouse-clickable and
/// reachable via directional/programmatic focus, it is just not a tab stop.
/// The skip must live on the [IconButton]'s own [FocusNode]: a skipping
/// ancestor does not exclude the button's internal node from traversal.
class KObscureToggle extends StatefulWidget {
  const KObscureToggle({
    super.key,
    required this.obscured,
    required this.onToggle,
  });

  /// Whether the field text is currently hidden (shows the "reveal" eye).
  final bool obscured;

  /// Invoked when the user clicks the toggle.
  final VoidCallback onToggle;

  @override
  State<KObscureToggle> createState() => _KObscureToggleState();
}

class _KObscureToggleState extends State<KObscureToggle> {
  final FocusNode _focusNode = FocusNode(skipTraversal: true);

  @override
  void dispose() {
    _focusNode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return IconButton(
      focusNode: _focusNode,
      icon: Icon(widget.obscured ? Icons.visibility_off : Icons.visibility),
      onPressed: widget.onToggle,
    );
  }
}
