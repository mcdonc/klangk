"""Hatchling build hook: ship the Flutter web build inside the wheel (#1600).

The compiled frontend lives at ``<repo>/src/frontend/build/web`` and is
gitignored, so it only exists at *release-wheel* build time (after
``scripts/flutterbuildweb.sh``). This hook conditionally force-includes it
into the wheel at ``klangk/frontend/`` so a ``pip install klangk`` deployment
serves the UI out of the box.

A plain ``force-include`` in ``pyproject.toml`` would be strict for *every*
build mode, including editable installs — but editable installs (devenv, CI)
run against a source tree where the artifact is often absent (CI never builds
the frontend) and they don't need it in the wheel anyway (they point
``KLANGK_FRONTEND_DIR`` at the repo build, see ``devenv.nix`` /
the host ``Dockerfile``). So instead this hook:

- includes the artifact when present (release wheel built after
  ``flutterbuildweb.sh``), and
- *requires* it only for a non-editable wheel build, failing loudly so a
  release wheel can't silently ship UI-less.

Editable builds (``pip install -e``) proceed without the artifact.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hatchling.builders.hooks.feature.interface import BuildHookInterface

# The built UI lands at <repo>/src/frontend/build/web. ``self.root`` is the
# project directory (``src/klangk``), so two levels up is the repo root.
_WHEEL_DEST = "klangk/frontend"


class FrontendArtifactHook(BuildHookInterface):
    """Force-include the Flutter web build into the wheel (#1600)."""

    FEATURE_NAME = "frontend-artifact"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        # Only the wheel ships the frontend; sdist is source-only.
        if self.target_name != "wheel":
            return
        frontend_src = (
            Path(self.root).resolve().parent.parent
            / "src"
            / "frontend"
            / "build"
            / "web"
        )
        if frontend_src.is_dir():
            build_data.setdefault("force_include", {})[str(frontend_src)] = (
                _WHEEL_DEST
            )
            return
        # Artifact absent. Editable builds (dev/CI) are allowed to proceed
        # without it — they serve the UI from the repo via
        # KLANGK_FRONTEND_DIR. A regular wheel build must fail loudly so a
        # release wheel can't silently ship UI-less (#1600).
        if version == "editable":
            return
        raise FileNotFoundError(
            f"Frontend artifact not found at {frontend_src}. Run "
            "scripts/flutterbuildweb.sh before building the wheel "
            "(the release wheel must ship the compiled UI; #1600)."
        )
