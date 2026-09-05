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

1. The vendored tree matches the ``FALLBACK-FONTS.sha256`` manifest —
   every listed part exists with its pinned hash, and nothing extra
   ships (runs everywhere, no Flutter needed).
2. The manifest's URL set matches the engine's current set — this needs
   a Flutter SDK with engine sources on PATH (the devenv/nix layout), so
   it is skipped with a loud reason in CI jobs that run ``scripts/tests``
   without Flutter; re-run ``scripts/vendor_flutter_fallback_fonts.py``
   after a Flutter/toolchain bump that changes the set.
3. Every ``family/version`` directory is listed in ``pubspec.yaml`` —
   Flutter directory assets are not recursive, so an unlisted directory's
   parts would silently never ship — and ``flutter_bootstrap.js`` points
   the engine at the same-origin mirror (the double ``assets/assets/``
   prefix is load-bearing: bundled assets are served under
   ``build/web/assets/assets/``).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FRONTEND = _ROOT / "src/frontend"
_VENDOR_SCRIPT = _ROOT / "scripts/vendor_flutter_fallback_fonts.py"
_BOOTSTRAP = _FRONTEND / "web/flutter_bootstrap.js"
_PUBSPEC = _FRONTEND / "pubspec.yaml"


def _import_vendor_module():
    sys.path.insert(0, str(_VENDOR_SCRIPT.parent))
    import vendor_flutter_fallback_fonts as vendor

    return vendor


def _manifest_hashes():
    vendor = _import_vendor_module()
    hashes = vendor.read_manifest(vendor.TARGET_DIR / vendor.MANIFEST_NAME)
    assert hashes, f"{vendor.MANIFEST_NAME} missing or empty — re-run the vendor script"
    return vendor, hashes


def test_tree_matches_manifest():
    """Every manifest-listed part is on disk with its pinned hash, and no
    unlisted woff2 ships (runs in CI: no Flutter SDK required)."""
    vendor, hashes = _manifest_hashes()
    problems = vendor.check_tree(sorted(hashes), hashes)
    assert not problems, "\n".join(problems)


def test_manifest_matches_engine_set():
    """The manifest pins exactly the URLs the engine can request.

    Needs a Flutter SDK carrying engine sources (the devenv/nix layout at
    ``<sdk>/engine/src/flutter/lib/web_ui``); the stock CI runners of
    ``scripts/tests`` have no Flutter, so those skip loudly — the drift
    this catches is introduced by toolchain bumps, which are cut from a
    devenv shell where the test runs.
    """
    vendor, hashes = _manifest_hashes()
    try:
        urls = set(vendor.engine_font_urls(vendor.flutter_sdk("flutter")))
    except SystemExit as exc:
        pytest.skip(f"no Flutter SDK with engine sources on PATH ({exc})")
    assert urls == set(hashes), (
        f"engine set drifted from the vendored manifest: "
        f"missing={sorted(urls - set(hashes))[:5]}, "
        f"extra={sorted(set(hashes) - urls)[:5]} — re-run the vendor script"
    )


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
    text = _BOOTSTRAP.read_text()
    # The {{flutter_build_config}}/{{flutter_js}} placeholders must stay on
    # their exact single-line form — the flutter tool substitutes by exact
    # match, and a reformatted file (e.g. by prettier) ships the raw
    # placeholder and breaks app boot. The file is in .prettierignore for
    # the same reason; this pins it.
    assert text.startswith("{{flutter_build_config}}\n{{flutter_js}}\n"), (
        "bootstrap template placeholders must stay on their exact lines"
    )
    assigned = re.findall(
        r'fontFallbackBaseUrl:\s*"([^"]+)"',
        text,
    )
    assert assigned == ["assets/assets/fallback-fonts/"], (
        "flutter_bootstrap.js must keep fontFallbackBaseUrl on the vendored "
        "same-origin mirror (assets/assets/ double prefix is load-bearing)"
    )
