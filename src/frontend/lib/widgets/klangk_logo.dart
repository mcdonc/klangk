import 'package:flutter/material.dart';
import '../branding.dart';
import '../theme/colors.dart';

/// Klangk logo widget — dark rounded square with robot icon and "klangk" text.
class KlangkLogo extends StatelessWidget {
  final double height;

  const KlangkLogo({super.key, this.height = 40});

  @override
  Widget build(BuildContext context) {
    final override = Branding.logoUrl;
    if (override.isNotEmpty) {
      // Deployer logo override (KLANGK_LOGO_URL via /config). Render the
      // image sized to [height]; fall back to the default widget on load
      // error so a broken/removed logo never leaves a blank square. #1152.
      return SizedBox(
        width: height,
        height: height,
        child: Image.network(
          override,
          fit: BoxFit.contain,
          errorBuilder: (context, error, stackTrace) => _defaultLogo(),
        ),
      );
    }
    return _defaultLogo();
  }

  Widget _defaultLogo() {
    final iconSize = height * 0.5;
    final fontSize = height * 0.2;
    final radius = height * 0.18;

    return Container(
      width: height,
      height: height,
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(radius),
        gradient: const LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [KColors.logoGradientStart, KColors.logoGradientEnd],
        ),
        border: Border.all(color: KColors.borderDefault, width: 1),
      ),
      child: FittedBox(
        fit: BoxFit.scaleDown,
        child: Column(
          mainAxisAlignment: MainAxisAlignment.center,
          mainAxisSize: MainAxisSize.min,
          children: [
            Icon(Icons.smart_toy_outlined,
                color: KColors.textPrimary, size: iconSize),
            Text(
              'klangk',
              style: TextStyle(
                fontSize: fontSize,
                fontWeight: FontWeight.w400,
                color: KColors.textPrimary,
                letterSpacing: 0.5,
                height: 1.1,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
