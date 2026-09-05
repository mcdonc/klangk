"""Contract tests for the vendored engine fallback fonts (#3228).

The frontend must be fully self-contained: at runtime it never reaches out
to an external origin for fonts, JS, CSS, or images. The engine's web
renderers fetch Noto fallback fonts (plus a boot-time Roboto) from
``configuration.fontFallbackBaseUrl`` — by default fonts.gstatic.com.
klangk points that base at the same-origin vendored mirror instead, via
``src/frontend/web/flutter_bootstrap.js`` + the checked-in tree at
``src/frontend/assets/fallback-fonts/``.

These tests pin the three legs of that arrangement so a silent drift is
loud:

1. The vendored tree matches the engine's current URL set and the
   ``FALLBACK-FONTS.sha256`` manifest (re-run
   ``scripts/vendor_flutter_fallback_fonts.py`` after a Flutter/toolchain
   bump that changes the set).
2. Every ``family/version`` directory is listed in ``pubspec.yaml`` —
   Flutter directory assets are not recursive, so an unlisted directory's
   parts would silently never ship.
3. ``flutter_bootstrap.js`` points the engine at the same-origin mirror
   (the double ``assets/assets/`` prefix is load-bearing: bundled assets
   are served under ``build/web/assets/assets/``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND = _ROOT / "src/frontend"
_VENDOR_SCRIPT = _ROOT / "scripts/vendor_flutter_fallback_fonts.py"
_BOOTSTRAP = _FRONTEND / "web/flutter_bootstrap.js"
_PUBSPEC = _FRONTEND / "pubspec.yaml"


def _import_vendor_module():
    sys.path.insert(0, str(_VENDOR_SCRIPT.parent))
    import vendor_flutter_fallback_fonts as vendor

    return vendor


def test_vendored_tree_matches_engine_and_manifest():
    vendor = _import_vendor_module()
    urls = vendor.engine_font_urls(vendor.flutter_sdk("flutter"))
    hashes = vendor.read_manifest(vendor.TARGET_DIR / vendor.MANIFEST_NAME)
    problems = vendor.check_tree(urls, hashes)
    assert not problems, "\n".join(problems)


def vendored_dirs(target: Path) -> set[str]:
    """Every family/version directory pair on disk."""
    pairs = set()
    for family in target.iterdir():
        if not family.is_dir():
            continue
        for version in family.iterdir():
            if version.is_dir():
                pairs.add(f"{family.name}/{version.name}")
    return pairs


def listed_dirs() -> set[str]:
    """Every family/version directory pair listed in pubspec.yaml."""
    return set(
        re.findall(
            r"^\s*- assets/fallback-fonts/([^\s/]+/[^\s/]+)/\s*$",
            _PUBSPEC.read_text(),
            re.M,
        )
    )


def test_pubspec_lists_every_vendored_directory():
    target = _FRONTEND / "assets/fallback-fonts"
    on_disk = vendored_dirs(target)
    listed = listed_dirs()
    assert on_disk == listed, (
        f"pubspec/asset-tree drift: on disk only={sorted(on_disk - listed)}, "
        f"pubspec only={sorted(listed - on_disk)}"
    )


def test_bootstrap_points_engine_at_vendored_fonts():
    assigned = re.findall(
        r'fontFallbackBaseUrl:\s*"([^"]+)"',
        _BOOTSTRAP.read_text(),
    )
    assert assigned == ["assets/assets/fallback-fonts/"], (
        "flutter_bootstrap.js must keep fontFallbackBaseUrl on the vendored "
        "same-origin mirror (assets/assets/ double prefix is load-bearing)"
    )
