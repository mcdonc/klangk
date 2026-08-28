"""Classification-marking rendering helpers for the TUI (#2768).

The effective marking is the workspace's ``classification_banner`` when
set, else the deploy-wide default (``KLANGKD_CLASSIFICATION_BANNER``);
empty/None renders nothing and reserves no screen space. The color
convention mirrors the Flutter web banner (``marking_banner.dart``): the
label is matched case-insensitively against the common US marking words
and colored accordingly (orange = TOP SECRET, red = SECRET, blue =
CONFIDENTIAL/CUI, green = UNCLASSIFIED); anything else (a site-specific
free-text label) renders on a neutral amber background. This module is
deliberately duplicated rather than shared with the server — the CLI is an
isolated client (AGENTS.md "CLI subpackage isolation").
"""

from __future__ import annotations

import re

# (pattern, background) pairs checked in order against the marking; the
# first hit wins (TOP SECRET must precede SECRET). Word-boundary matching
# so a free-text label that merely *contains* a marking word (e.g.
# "NOT SECRETIVE") is not colored as that marking.
_MARKING_COLORS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(rf"\b{word}\b", re.IGNORECASE), color)
    for word, color in (
        ("TOP SECRET", "#E0A800"),
        ("SECRET", "#C01818"),
        ("CONFIDENTIAL", "#005EB8"),
        ("CUI", "#0076CE"),
        ("UNCLASSIFIED", "#007A33"),
    )
)
_FALLBACK_COLOR = "#8A6D00"  # neutral amber for free-text labels


def marking_background(marking: str) -> str:
    """The banner background color (#rrggbb) for a marking label."""
    for pattern, color in _MARKING_COLORS:
        if pattern.search(marking):
            return color
    return _FALLBACK_COLOR


def marking_style(marking: str) -> str:
    """A Rich style string for rendering a marking line (white on color)."""
    return f"bold white on {marking_background(marking)}"


def effective_marking(
    workspace_banner: str | None, deploy_default: str | None
) -> str:
    """Resolve the marking actually shown: workspace override, else deploy.

    Whitespace-only values count as unset; the result is stripped, and an
    empty resolution means "render nothing" (no reserved screen space).
    """
    own = (workspace_banner or "").strip()
    if own:
        return own
    return (deploy_default or "").strip()
