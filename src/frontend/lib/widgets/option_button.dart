import 'package:flutter/material.dart';

import '../theme/colors.dart';

/// One option button in a group of mutually-exclusive choices — e.g. the
/// pause windows on the Net Rules tab and the verdict durations on the
/// consent banner. Gives every such group the same look so it reads as one
/// intentional control instead of a loose row of default buttons (#2502):
/// pill-shaped, a shared minimum size (so the buttons align), the amber
/// fill marking the active choice, and optional leading icon + tooltip.
///
/// Deliberately built on [FilledButton] / [OutlinedButton] (rather than a
/// custom paint or [SegmentedButton]) so widget tests can keep asserting
/// the active/inactive button types via [buttonKey], and so a tap on the
/// already-active choice still fires [onPressed] (SegmentedButton swallows
/// it in single-select mode).
class KOptionButton extends StatelessWidget {
  const KOptionButton({
    required this.buttonKey,
    required this.label,
    required this.active,
    required this.onPressed,
    this.icon,
    this.tooltip,
  });

  /// Key placed on the inner Material button (not this wrapper) so tests
  /// can find and type-assert the FilledButton/OutlinedButton itself.
  final Key buttonKey;
  final String label;
  final bool active;
  final VoidCallback onPressed;

  /// Optional leading affordance (e.g. pause/play on the pause windows).
  final IconData? icon;

  /// Optional hover/long-press explanation of what the choice does.
  final String? tooltip;

  /// Shared metrics: equal minimum footprint + pill shape across a group.
  static const _minSize = Size(84, 34);
  static const _shape = StadiumBorder();
  static const _text = TextStyle(fontSize: 12);
  static const _pad = EdgeInsets.symmetric(horizontal: 14);

  Widget get _child {
    if (icon == null) return Text(label);
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(icon, size: 14),
        const SizedBox(width: 6),
        Text(label),
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    final button = active
        ? FilledButton(
            key: buttonKey,
            style: FilledButton.styleFrom(
              backgroundColor: KColors.accentAmber,
              foregroundColor: KColors.bgCanvas,
              minimumSize: _minSize,
              padding: _pad,
              textStyle: _text,
              visualDensity: VisualDensity.compact,
              shape: _shape,
            ),
            onPressed: onPressed,
            child: _child,
          )
        : OutlinedButton(
            key: buttonKey,
            style: OutlinedButton.styleFrom(
              foregroundColor: KColors.textSecondary,
              side: const BorderSide(color: KColors.borderDefault),
              minimumSize: _minSize,
              padding: _pad,
              textStyle: _text,
              visualDensity: VisualDensity.compact,
              shape: _shape,
            ),
            onPressed: onPressed,
            child: _child,
          );
    if (tooltip == null) return button;
    return Tooltip(message: tooltip!, child: button);
  }
}
