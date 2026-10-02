"""Hatchling build hook: ship extra package data inside the wheel.

Two pieces of data live outside the ``klangk`` package dir but must be present
in an installed wheel:

- **the compiled Flutter web build** (``<repo>/src/frontend/build/web``) ->
  ``klangk/frontend/`` (#1600). It is gitignored, so it only exists at
  *release-wheel* build time (after ``scripts/flutterbuildweb.sh``). Included
  when present, and *required* for a non-editable wheel (so a release wheel
  can't silently ship UI-less). Editable builds proceed without it.
- **the nix-seed Dockerfile** (``<repo>/src/containers/nix-seed/Dockerfile``)
  -> ``klangk/nix-seed/Dockerfile`` (#2225). A committed source file, always
  present, so it is always included -- it lets ``klangk-build-nix-seed`` build
  a seed from a wheel install (no source tree, no devenv).
- **the build version file** (``<repo>/scripts/generate-version.sh`` output)
  -> ``klangk/version.json`` (#3517). Generated at build time from git state
  — the same script that seeds the devenv's version file and the host
  image's ``/home/klangk/version.json`` — so an installed wheel always
  carries its build identity even when the runtime's ``version_file``
  setting is unset (a deployed host whose operator ``klangkd.yaml`` mounts
  over the image's config and omits the key). Included when the script is
  present and yields a real version; an absent script, a missing
  interpreter, or a failing/unusable script yields nothing and the wheel
  ships without the packaged copy (the runtime falls back to the dev
  block).

For the **sdist** target the hook force-includes the repo-root ``README.md``
as a real file at ``README.md`` — the fallback source the metadata hook
(``hatch_readme_metadata.py``) reads when a wheel is built from an sdist
extraction with no surrounding repo (#3349).

A plain static ``force-include`` would be strict for every build mode (breaking
editable installs where the gitignored frontend is absent) and rejects paths
above the project root. This hook force-includes via absolute paths in
``build_data`` (bypassing both restrictions).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_FRONTEND_DEST = "klangk/frontend"
_NIX_SEED_DEST = "klangk/nix-seed"
_VERSION_DEST = "klangk/version.json"


def materialized_version_file(repo: Path) -> Path | None:
    """A temp file holding ``generate-version.sh`` output, or ``None``.

    Lenient on purpose (#3517): a missing script (an sdist extraction has
    no repo, no scripts/), a missing interpreter, or a
    failing/unusable script yields ``None`` and the wheel ships without
    the packaged copy; the runtime chain then falls back to the dev
    block, mirroring how editable builds proceed without the gitignored
    frontend artifact. ``generate-version.sh`` also emits ``unknown``
    when git metadata is absent — that output is rejected here so such
    a wheel reports ``dev`` instead of a misleading ``unknown``.
    """
    script = repo / "scripts" / "generate-version.sh"
    if not script.is_file():
        return None
    try:
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True
        )
    except OSError:
        return None
    payload = _usable_version_output(result)
    if payload is None:
        return None
    fd, name = tempfile.mkstemp(prefix="klangk-version-", suffix=".json")
    with os.fdopen(fd, "w") as f:
        f.write(payload)
    return Path(name)


def _usable_version_output(result) -> str | None:
    """The script's stdout when it names a real version, else ``None``."""
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(info, dict) or info.get("version") in (
        None,
        "",
        "unknown",
    ):
        return None
    return result.stdout


class PackageDataHook(BuildHookInterface):
    """Force-include the Flutter web build + the nix-seed Dockerfile (#1600,
    #2225)."""

    PLUGIN_NAME = "package-data"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        force = build_data.setdefault("force_include", {})
        # ``self.root`` is the project dir (``src/klangk``); two levels up is
        # the repo root.
        repo = Path(self.root).resolve().parent.parent

        if self.target_name == "sdist":
            # Ship the repo-root README as a real file at README.md so a wheel
            # built from an extracted sdist (what ``pip install <sdist>``
            # does) can find it — hatch_readme_metadata.py falls back to this
            # local copy when the surrounding repo is absent (#3349).
            # (``readme`` is dynamic, so hatchling adds no readme entry of its
            # own; this is the only one.)
            force[str(repo / "README.md")] = "README.md"
            return

        # Only the wheel ships the rest of this data.
        if self.target_name != "wheel":
            return

        # --- nix-seed Dockerfile: committed source file, always included. ---
        nix_seed_df = repo / "src" / "containers" / "nix-seed" / "Dockerfile"
        if nix_seed_df.is_file():
            force[str(nix_seed_df)] = f"{_NIX_SEED_DEST}/Dockerfile"

        # --- Build version file: generated from git state (#3517). ---
        # Skipped for editable builds — the copy would freeze at install
        # time and go stale as the checkout moves (the devenv seeds a
        # live version file through KLANGKD_VERSION_FILE instead).
        if version != "editable":
            version_src = materialized_version_file(repo)
            if version_src is not None:
                force[str(version_src)] = _VERSION_DEST
                self._version_tmp = str(version_src)

        # --- Flutter web build: gitignored, conditional + required for wheel.---
        frontend_src = repo / "src" / "frontend" / "build" / "web"
        if frontend_src.is_dir():
            force[str(frontend_src)] = _FRONTEND_DEST
            return
        # Artifact absent. Editable builds (dev/CI) are allowed to proceed
        # without it -- they serve the UI from the repo via
        # KLANGKD_FRONTEND_DIR. A regular wheel build must fail loudly so a
        # release wheel can't silently ship UI-less (#1600).
        if version == "editable":
            return
        raise FileNotFoundError(
            f"Frontend artifact not found at {frontend_src}. Run "
            "scripts/flutterbuildweb.sh before building the wheel "
            "(the release wheel must ship the compiled UI; #1600)."
        )

    def finalize(
        self, version: str, build_data: dict[str, Any], artifact: str
    ) -> None:
        """Remove the generated version temp file (best effort, #3517)."""
        path = getattr(self, "_version_tmp", None)
        if path is None:
            return
        try:
            os.unlink(path)
        except OSError:
            pass
        self._version_tmp = None
