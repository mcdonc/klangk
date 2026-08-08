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

A plain static ``force-include`` would be strict for every build mode (breaking
editable installs where the gitignored frontend is absent) and rejects paths
above the project root. This hook force-includes via absolute paths in
``build_data`` (bypassing both restrictions).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

_FRONTEND_DEST = "klangk/frontend"
_NIX_SEED_DEST = "klangk/nix-seed"


class PackageDataHook(BuildHookInterface):
    """Force-include the Flutter web build + the nix-seed Dockerfile (#1600,
    #2225)."""

    PLUGIN_NAME = "package-data"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        # Only the wheel ships this data; sdist is source-only.
        if self.target_name != "wheel":
            return
        force = build_data.setdefault("force_include", {})
        # ``self.root`` is the project dir (``src/klangk``); two levels up is
        # the repo root.
        repo = Path(self.root).resolve().parent.parent

        # --- nix-seed Dockerfile: committed source file, always included. ---
        nix_seed_df = repo / "src" / "containers" / "nix-seed" / "Dockerfile"
        if nix_seed_df.is_file():
            force[str(nix_seed_df)] = f"{_NIX_SEED_DEST}/Dockerfile"

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
