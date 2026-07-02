import 'package:flutter/material.dart';
import '../branding.dart';
import '../utils/web_helpers_stub.dart'
    if (dart.library.js_interop) '../utils/web_helpers_web.dart';

/// Renders the deployer-configured legal and support links.
///
/// Fed by [Branding] (populated from `/api/v1/config`). Everything is hidden
/// when unset, so an unconfigured deployment shows nothing -- matching the
/// logo/product-name white-label knobs. Legal links (Terms / Privacy / AUP)
/// are most prominent on the auth screens; the support link is useful app-wide
/// (#1177).
///
/// [showLegal] and [showSupport] toggle the two groups independently so a
/// caller can place legal links in an auth footer and a support link in the
/// app bar without coupling them.
class LegalLinks extends StatelessWidget {
  final bool showLegal;
  final bool showSupport;
  final bool dense;

  const LegalLinks({
    super.key,
    this.showLegal = true,
    this.showSupport = true,
    this.dense = false,
  });

  @override
  Widget build(BuildContext context) {
    final links = <MapEntry<String, String>>[
      if (showLegal) ...Branding.legalLinks,
      if (showSupport && Branding.supportHref.isNotEmpty)
        MapEntry('Support', Branding.supportHref),
    ];
    if (links.isEmpty) return const SizedBox.shrink();
    final style = Theme.of(context).textTheme.bodySmall;
    return Wrap(
      alignment: WrapAlignment.center,
      spacing: dense ? 8 : 12,
      runSpacing: 4,
      children: [
        for (final link in links)
          MouseRegion(
            cursor: SystemMouseCursors.click,
            child: GestureDetector(
              onTap: () => openUrl(link.value),
              child: Text(
                link.key,
                style: style?.copyWith(
                  decoration: TextDecoration.underline,
                ),
              ),
            ),
          ),
      ],
    );
  }
}
