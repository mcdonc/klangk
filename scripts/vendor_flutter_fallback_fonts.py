"""Vendor the Flutter engine's web fallback-font set into the frontend (#3228).

The CanvasKit/skwasm renderers lazily fetch Noto fallback fonts whenever a
frame rasterizes a codepoint none of the app's bundled fonts cover (CJK
terminal output, the ``…`` in status text, emoji, …). The engine resolves
each part against ``configuration.fontFallbackBaseUrl`` (default
``https://fonts.gstatic.com/s/``). The served CSP is first-party-only, so
those fetches are blocked and the codepoints render as tofu.

The fix: check the whole set in at
``src/frontend/assets/fallback-fonts/`` (mirroring the exact gstatic URL
layout the engine requests — family/version/part paths are load-bearing),
and point ``fontFallbackBaseUrl`` at that same-origin directory from
``src/frontend/web/flutter_bootstrap.js``. This script produces and
refreshes the vendored tree from the engine sources of the *local* Flutter
SDK, so a toolchain bump that changes the URL set is caught by re-running
it (and by the contract test in ``scripts/tests/``).

Usage (from the repo root, inside the devenv shell):

    python scripts/vendor_flutter_fallback_fonts.py            # fetch/verify
    python scripts/vendor_flutter_fallback_fonts.py --check    # verify only
    python scripts/vendor_flutter_fallback_fonts.py --print-pubspec

``--check`` compares the on-disk tree against ``FALLBACK-FONTS.sha256``
and against the engine's current URL set; it makes no network calls.
``--print-pubspec`` prints the ``flutter.assets`` entries the tree needs
(Flutter directory assets are not recursive, so every ``family/version``
directory must be listed explicitly in ``pubspec.yaml``).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

GSTATIC_BASE = "https://fonts.gstatic.com/s/"
_ROOT = Path(__file__).resolve().parents[1]
TARGET_DIR = _ROOT / "src/frontend/assets/fallback-fonts"
MANIFEST_NAME = "FALLBACK-FONTS.sha256"

_NOTO_URL_RE = re.compile(r"NotoFont\(\s*'[^']+',\s*'([^']+)'")
_ROBOTO_URL_RE = re.compile(r"fontFallbackBaseUrl\}([^']+)'")
_MAX_WORKERS = 16
_RETRIES = 3


def flutter_sdk(flutter_bin: str) -> Path:
    """Resolve the on-disk Flutter SDK root for ``flutter_bin``."""
    resolved = shutil.which(flutter_bin)
    if resolved is None:
        raise SystemExit(f"flutter binary not found: {flutter_bin}")
    real = Path(
        subprocess.check_output(["readlink", "-f", resolved], text=True).strip()
    )
    sdk = real.parent.parent
    if not (sdk / "bin" / "cache").is_dir():
        raise SystemExit(f"does not look like a Flutter SDK root: {sdk}")
    return sdk


def engine_file(sdk: Path, *parts: str) -> Path:
    """Locate a web_ui engine source file under the SDK."""
    path = sdk / "engine/src/flutter/lib/web_ui/lib/src/engine" / Path(*parts)
    if not path.is_file():
        raise SystemExit(f"engine source not found (SDK layout change?): {path}")
    return path


def engine_font_urls(sdk: Path) -> list[str]:
    """Every gstatic-relative font URL the engine can request at runtime."""
    data = engine_file(sdk, "font_fallback_data.dart").read_text()
    urls = _NOTO_URL_RE.findall(data)
    # The boot-time Roboto download (canvaskit/fonts.dart) is separate from
    # the fallback list: SkiaFontCollection always fetches it as the default
    # fallback font unless the app bundles a "Roboto" family.
    roboto_line = engine_file(sdk, "canvaskit", "fonts.dart").read_text()
    roboto = _ROBOTO_URL_RE.search(roboto_line)
    if roboto is None:
        raise SystemExit(
            "boot-time Roboto URL not found in canvaskit/fonts.dart "
            "(engine layout change) — update _ROBOTO_URL_RE"
        )
    urls.append(roboto.group(1))
    return sorted(set(urls))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(path: Path) -> dict[str, str]:
    """Parse ``sha256  url`` lines into {url: hash}."""
    if not path.is_file():
        return {}
    hashes = {}
    for line in path.read_text().splitlines():
        digest, url = line.split("  ", 1)
        hashes[url] = digest
    return hashes


def write_manifest(path: Path, hashes: dict[str, str]) -> None:
    lines = [f"{hashes[url]}  {url}" for url in sorted(hashes)]
    path.write_text("\n".join(lines) + "\n")


def fetch_bytes(url: str) -> bytes:
    """One download attempt (no retry logic)."""
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def store_part(url: str, data: bytes, hashes: dict[str, str]) -> None:
    """Write one part to its final path atomically and record its hash."""
    dest = TARGET_DIR / url
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=dest.parent, delete=False)
    try:
        tmp.write(data)
    finally:
        tmp.close()
    Path(tmp.name).replace(dest)
    hashes[url] = hashlib.sha256(data).hexdigest()


def part_is_current(url: str, hashes: dict[str, str]) -> bool:
    """Whether the on-disk copy already matches the manifest hash."""
    dest = TARGET_DIR / url
    return dest.is_file() and sha256_file(dest) == hashes.get(url)


def try_fetch(url: str, full_url: str, hashes: dict[str, str], last: bool) -> bool:
    """One download attempt; reports success, raises on a final failure."""
    try:
        store_part(url, fetch_bytes(full_url), hashes)
        return True
    except OSError as exc:
        if last:
            raise SystemExit(f"failed to fetch {full_url}: {exc}") from exc
        print(f"retrying {full_url} after: {exc}", file=sys.stderr)
        return False


def fetch_one(url: str, hashes: dict[str, str]) -> None:
    """Download one font part unless the on-disk copy already matches."""
    if part_is_current(url, hashes):
        return
    full = GSTATIC_BASE + url
    for attempt in range(_RETRIES):
        if try_fetch(url, full, hashes, attempt == _RETRIES - 1):
            return


def pubspec_block(urls: list[str]) -> str:
    """The pubspec asset entries covering every vendored directory."""
    dirs = sorted({str(Path(url).parent) for url in urls})
    entries = [f"    - assets/fallback-fonts/{d}/" for d in dirs]
    return "\n".join(entries)


def expected_problems(urls: list[str], hashes: dict[str, str]) -> list[str]:
    """Problems with the files the engine set expects on disk."""
    problems = []
    for url in urls:
        dest = TARGET_DIR / url
        if not dest.is_file():
            problems.append(f"missing: {url}")
        elif hashes.get(url) != sha256_file(dest):
            problems.append(f"hash mismatch: {url}")
    return problems


def extra_woff2_files(urls: list[str]) -> set[str]:
    """Vendored woff2 files absent from the reference URL set."""
    on_disk = {
        str(p.relative_to(TARGET_DIR))
        for p in TARGET_DIR.rglob("*")
        if p.is_file() and p.suffix == ".woff2"
    }
    return on_disk - set(urls)


def check_tree(urls: list[str], hashes: dict[str, str]) -> list[str]:
    """Verify the vendored tree against the URL set and the manifest."""
    problems = expected_problems(urls, hashes)
    problems.extend(
        f"not in reference set: {url}" for url in sorted(extra_woff2_files(urls))
    )
    return problems


def report_problems(problems: list[str]) -> None:
    """Print problems and exit non-zero when the tree drifted."""
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        raise SystemExit(f"{len(problems)} problem(s); re-run the vendor script")


def run_check(urls: list[str], hashes: dict[str, str]) -> None:
    report_problems(check_tree(urls, hashes))
    print(f"OK: {len(urls)} fallback font parts verified against the engine set")


def run_vendor(urls: list[str], hashes: dict[str, str], manifest: Path) -> None:
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(_MAX_WORKERS) as pool:
        for _ in pool.map(lambda url: fetch_one(url, hashes), urls):
            pass
    write_manifest(manifest, hashes)
    print(f"vendored {len(urls)} font parts into {TARGET_DIR}")
    print("pubspec.yaml asset entries needed (pubspec is edited by hand):")
    print(pubspec_block(urls))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--flutter",
        default="flutter",
        help="flutter binary to read the engine set from",
    )
    parser.add_argument(
        "--check", action="store_true", help="verify the tree only; no network"
    )
    parser.add_argument(
        "--print-pubspec",
        action="store_true",
        help="print the pubspec asset entries and exit",
    )
    args = parser.parse_args()

    urls = engine_font_urls(flutter_sdk(args.flutter))
    if args.print_pubspec:
        print(pubspec_block(urls))
        return

    manifest = TARGET_DIR / MANIFEST_NAME
    hashes = read_manifest(manifest)
    if args.check:
        run_check(urls, hashes)
    else:
        run_vendor(urls, hashes, manifest)


if __name__ == "__main__":
    main()
