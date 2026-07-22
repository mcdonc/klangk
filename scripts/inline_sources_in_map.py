#!/usr/bin/env python3
"""Inline `sourcesContent` into Flutter web source maps.

dart2wasm and dart2js emit source maps whose `sources` array uses
`org-dartlang-sdk:///` (Dart SDK / engine sources) and `file:///` (nix-store
Flutter framework) URIs that browsers can't fetch. Without `sourcesContent`,
Firefox/Chrome devtools fail to resolve those frames and you get a wall of
"unsupported protocol for sourcemap request" errors.

This rewrites the .map files to embed the actual source text inline. After
this runs, devtools resolve traces with no network fetches.

Usage:
    inline_sources_in_map.py <flutter-sdk> <map-file> [<map-file> ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def resolve(uri: str, flutter_sdk: Path, map_dir: Path) -> Path | None:
    """Map a sourcemap URI to an on-disk path, or None if unresolvable."""
    if uri.startswith("file:///"):
        return Path(uri[len("file://") :])
    if uri.startswith("org-dartlang-sdk:///dart-sdk/"):
        rel = uri[len("org-dartlang-sdk:///dart-sdk/") :]
        return flutter_sdk / "bin" / "cache" / "dart-sdk" / rel
    if uri.startswith("org-dartlang-sdk:///lib/"):
        rel = uri[len("org-dartlang-sdk:///lib/") :]
        return flutter_sdk / "bin" / "cache" / "flutter_web_sdk" / "lib" / rel
    if "://" not in uri:
        # dart2js emits paths relative to where it was invoked, but with one
        # too many `../` for the actual map-file directory; the relative form
        # treats `web/main.dart.js.map` as if it were nested an extra level
        # deeper. Try map-dir-relative first; if that misses, strip the leading
        # `../` segments and try project-root- and $HOME-relative as fallbacks.
        direct = (map_dir / uri).resolve()
        if direct.is_file():
            return direct
        # Strip leading `../` parts to get the inner path (e.g.
        # `.pub-cache/hosted/pub.dev/...` or `lib/auth/...`).
        stripped = uri
        while stripped.startswith("../"):
            stripped = stripped[3:]
        # Try every directory between map_dir and `/`, plus $HOME. dart2js
        # emits paths with a `../` depth that doesn't quite match the map
        # file's directory (app `lib/` paths are off by one, pub-cache paths
        # by several), so walk up and try each candidate base — the first
        # one where stripped resolves is the source.
        bases: list[Path] = []
        cur = map_dir
        while True:
            bases.append(cur)
            if cur.parent == cur:
                break
            cur = cur.parent
        if Path.home() not in bases:
            bases.append(Path.home())
        for base in bases:
            cand = (base / stripped).resolve()
            if cand.is_file():
                return cand
        # Bare filename fallback: dart2js emits `main.dart` and
        # `web_feature_registrant.dart` without a directory prefix. Search the
        # Flutter project (map_dir's grandparent) for the first match.
        if "/" not in stripped:
            project = map_dir.parent.parent
            for candidate in project.rglob(stripped):
                if candidate.is_file():
                    return candidate
    return None


def inline_map(map_path: Path, flutter_sdk: Path) -> tuple[int, int]:
    """Returns (resolved, total) source counts."""
    data = json.loads(map_path.read_text())
    sources = data.get("sources", [])
    map_dir = map_path.parent.resolve()
    contents: list[str | None] = []
    resolved = 0
    for uri in sources:
        path = resolve(uri, flutter_sdk, map_dir)
        if path is not None and path.is_file():
            contents.append(path.read_text(errors="replace"))
            resolved += 1
        else:
            contents.append(None)
    data["sourcesContent"] = contents
    map_path.write_text(json.dumps(data, separators=(",", ":")))
    return resolved, len(sources)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    flutter_sdk = Path(sys.argv[1]).resolve()
    if not flutter_sdk.is_dir():
        print(f"flutter SDK not found: {flutter_sdk}", file=sys.stderr)
        return 1
    for arg in sys.argv[2:]:
        map_path = Path(arg)
        if not map_path.is_file():
            print(f"skipping (missing): {map_path}", file=sys.stderr)
            continue
        resolved, total = inline_map(map_path, flutter_sdk)
        print(f"{map_path.name}: inlined {resolved}/{total} sources")
    return 0


if __name__ == "__main__":
    sys.exit(main())
