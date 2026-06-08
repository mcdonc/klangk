import 'package:flutter/material.dart';
import '../theme/colors.dart';

/// Skeuomorphic tab widget used in the IDE layout and admin pages.
class SkeuoTab extends StatelessWidget {
  final String label;
  final IconData icon;
  final bool isSelected;
  final int? badge;
  final bool badgeHighlight;
  final VoidCallback onTap;

  const SkeuoTab({
    super.key,
    required this.label,
    required this.icon,
    required this.isSelected,
    this.badge,
    this.badgeHighlight = false,
    required this.onTap,
  });

  @override
  Widget build(BuildContext context) {
    return GestureDetector(
      onTap: onTap,
      child: ClipRRect(
        borderRadius: const BorderRadius.only(
          bottomLeft: Radius.circular(8),
          bottomRight: Radius.circular(8),
        ),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 16),
          color: isSelected ? KColors.bgCanvas : KColors.bgAppBar,
          child: Row(
            children: [
              Icon(
                icon,
                size: 14,
                color: KColors.textSecondary,
              ),
              const SizedBox(width: 6),
              Text(
                label,
                style: TextStyle(
                  fontSize: 12,
                  fontWeight: isSelected ? FontWeight.w700 : FontWeight.normal,
                  color: KColors.textSecondary,
                ),
              ),
              if (badge != null) ...[
                const SizedBox(width: 4),
                Container(
                  padding:
                      const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
                  decoration: BoxDecoration(
                    color: badgeHighlight
                        ? KColors.accentAmber
                        : KColors.accentRed,
                    borderRadius: BorderRadius.circular(8),
                  ),
                  child: Text(
                    badgeHighlight
                        ? '@${badge! > 99 ? '99+' : badge.toString()}'
                        : (badge! > 99 ? '99+' : badge.toString()),
                    style: const TextStyle(
                      color: Colors.white,
                      fontSize: 10,
                      fontWeight: FontWeight.bold,
                    ),
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
