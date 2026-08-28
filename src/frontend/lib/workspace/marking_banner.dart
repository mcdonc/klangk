/// Classification marking banner (#2768).
///
/// A persistent, always-rendered classification banner pinned at the top
/// (and bottom) of the workspace page — the "mark sensitive/classified
/// output when required" Application Security and Development STIG control
/// ("markings are required at a minimum at the top and the bottom of
/// screens"). The effective marking is the workspace's
/// `classification_banner` when set, else the deploy-wide default
/// (`KLANGKD_CLASSIFICATION_BANNER` via `/api/v1/config`'s
/// `default_classification_banner`).
///
/// **No marking configured anywhere renders nothing** — no banner strip,
/// no reserved screen space (#2768 clarification).
///
/// The banner is color-coded by marking convention (case-insensitive
/// match against the common US marking words): orange = TOP SECRET,
/// red = SECRET, blue = CONFIDENTIAL/CUI, green = UNCLASSIFIED; any other
/// free-text label renders on a neutral amber background.

library;

import 'package:flutter/material.dart';

/// Maximum marking length. Must match the server's
/// CLASSIFICATION_BANNER_MAX_LEN (klangk/model/workspaces.py) — used for
/// client-side field caps so an oversize label is caught in the field
/// instead of on the server round-trip.
const int classificationBannerMaxLength = 120;

/// Marking word → banner background color, in priority order (TOP SECRET
/// before SECRET). Word-boundary matched (case-insensitive) so a
/// free-text label that merely *contains* a marking word (e.g. "NOT
/// SECRETIVE") is not colored as that marking.
const Map<String, Color> _markingPalette = {
  'TOP SECRET': Color(0xFFE0A800),
  'SECRET': Color(0xFFC01818),
  'CONFIDENTIAL': Color(0xFF005EB8),
  'CUI': Color(0xFF0076CE),
  'UNCLASSIFIED': Color(0xFF007A33),
};

/// The banner background color for a marking label.
///
/// First case-insensitive word-boundary hit wins (TOP SECRET is checked
/// before SECRET). Exported for tests.
Color markingColor(String marking) {
  for (final entry in _markingPalette.entries) {
    if (RegExp('\\b${entry.key}\\b', caseSensitive: false).hasMatch(marking)) {
      return entry.value;
    }
  }
  return const Color(0xFF8A6D00);
}

/// Resolves the effective marking: the workspace override, else the deploy
/// default. Whitespace-only values count as unset; an empty result means
/// "render nothing". Exported for tests and reuse.
String effectiveMarking(String? workspaceBanner, String? deployDefault) {
  final own = (workspaceBanner ?? '').trim();
  if (own.isNotEmpty) return own;
  return (deployDefault ?? '').trim();
}

/// The one-line marking strip. Renders [SizedBox.shrink] when [text] is
/// empty — never displacing content or reserving screen space.
///
/// Not focusable and not interactive: the marking is a label, and its
/// position above/below the page body is what makes it persistent
/// (transient banners — consent, server schedule — stack below it inside
/// the page body).
class MarkingBanner extends StatelessWidget {
  // Non-const on purpose: a const constructor's declaration line never
  // executes (canonical const instance), which makes the 100% coverage
  // gate flag it nondeterministically across toolchains.
  MarkingBanner({super.key, required this.text});

  /// The marking to render; empty renders nothing.
  final String text;

  @override
  Widget build(BuildContext context) {
    final marking = text.trim();
    if (marking.isEmpty) return const SizedBox.shrink();
    final color = markingColor(marking);
    return Material(
      elevation: 0,
      color: color,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
        child: SizedBox(
          width: double.infinity,
          // #2768 review: scale down instead of ellipsize — a marking
          // clipped to "SECRET//…" is not a marking under the STIG
          // control, so the full label always stays legible (narrow
          // viewports shrink the type rather than dropping text).
          child: FittedBox(
            fit: BoxFit.scaleDown,
            child: Text(
              marking,
              textAlign: TextAlign.center,
              style: const TextStyle(
                color: Colors.white,
                fontWeight: FontWeight.w700,
                letterSpacing: 0.5,
              ),
            ),
          ),
        ),
      ),
    );
  }
}
