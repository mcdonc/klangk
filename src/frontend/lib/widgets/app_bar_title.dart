import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';
import '../theme/colors.dart';
import 'klangk_logo.dart';

/// Shared app bar title: clickable logo + page title.
class AppBarTitle extends StatelessWidget {
  final String title;

  const AppBarTitle({super.key, required this.title});

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        MouseRegion(
          cursor: SystemMouseCursors.click,
          child: GestureDetector(
            onTap: () => context.go('/'),
            child: const KlangkLogo(height: 36),
          ),
        ),
        Container(
          height: 20,
          width: 1,
          margin: const EdgeInsets.symmetric(horizontal: 12),
          color: KColors.borderDefault,
        ),
        Text(
          title,
          style: const TextStyle(
            fontSize: 15,
            fontWeight: FontWeight.w700,
            letterSpacing: 0.3,
            color: KColors.textSecondary,
          ),
        ),
      ],
    );
  }
}
