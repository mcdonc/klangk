"""Contract tests for the published klangk wheel's core metadata (#3349).

The PyPI page for klangk 2.0a1 shipped two metadata defects: no long
description (the wheel carried no README — PyPI rendered the "author has
not provided" placeholder) and ``requires-python >= 3.12`` while the
project's toolchain is Python 3.14 — the wheel claimed compatibility with
Pythons the project neither builds nor tests against.

These tests pin the two invariants against the real files, without
building a wheel (the full wheel build needs the compiled Flutter
frontend artifact, gitignored and absent in CI):

- the long description is wired dynamically (``readme`` in ``dynamic``,
  no static field) through ``hatch_readme_metadata.py``, which reads the
  repo-root README and falls back to the sdist-shipped copy;
- the ``requires-python`` floor matches the devenv toolchain pin
  (``package = pkgs.python314`` in devenv.nix), so the two cannot drift
  apart again — a future toolchain bump without a floor bump fails here.

The floor↔toolchain cross-check is the exact bug class #3349 reported:
the floor was set to 3.12 in #1614 and survived three toolchain
generations unnoticed because nothing tied the two together.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_KLANGK_DIR = _ROOT / "src" / "klangk"

# The floor must be exactly the devenv toolchain's Python: the wheel is
# built and tested on that interpreter alone, so any looser floor is an
# untested compatibility claim (#3349). Bump both together.
_TOOLCHAIN_PIN_RE = re.compile(
    r"package = pkgs\.python(?P<major>\d)(?P<minor>\d{2})(?=\W)"
)

_FLOOR_RE = re.compile(r"^>=\s*(?P<major>\d+)\.(?P<minor>\d+)$")


def _load_project() -> dict:
    with (_KLANGK_DIR / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]


def _floor_version(project: dict) -> tuple[int, int]:
    spec = project["requires-python"]
    match = _FLOOR_RE.match(spec)
    assert match is not None, f"unsupported requires-python form: {spec!r}"
    return int(match["major"]), int(match["minor"])


def _toolchain_version() -> tuple[int, int]:
    nix = (_ROOT / "devenv.nix").read_text()
    match = _TOOLCHAIN_PIN_RE.search(nix)
    assert match is not None, "devenv.nix lost its `package = pkgs.pythonXYZ` pin"
    return int(match["major"]), int(match["minor"])


def test_requires_python_matches_toolchain() -> None:
    floor = _floor_version(_load_project())
    toolchain = _toolchain_version()
    assert floor == toolchain, (
        f"wheel requires-python floor {floor} != devenv toolchain "
        f"{toolchain}: the wheel must not claim compatibility with "
        "Pythons the project does not build or test against (#3349)"
    )


def test_long_description_wired_via_metadata_hook() -> None:
    project = _load_project()
    # Dynamic wiring: a static readme field would have to name a path inside
    # src/klangk, and hatchling refuses the ../../ escape the root README
    # needs (#3349).
    assert "readme" in project["dynamic"], (
        "`readme` must stay in project.dynamic — the long description is "
        "injected by hatch_readme_metadata.py (#3349)"
    )
    assert "readme" not in project, "`readme` cannot be both static and dynamic (#3349)"
    hook = _KLANGK_DIR / "hatch_readme_metadata.py"
    assert hook.is_file(), f"metadata hook missing: {hook}"


def test_metadata_hook_sources() -> None:
    hook_src = (_KLANGK_DIR / "hatch_readme_metadata.py").read_text()
    build_hook_src = (_KLANGK_DIR / "hatch_build_package_data.py").read_text()
    assert "parent.parent" in hook_src, (
        "hatch_readme_metadata.py must resolve the repo-root README via "
        "parent.parent (#3349)"
    )
    assert 'README.md"' in build_hook_src, (
        "hatch_build_package_data.py must ship the README in the sdist so "
        "the metadata hook's fallback can find it (#3349)"
    )
    assert (_ROOT / "README.md").is_file(), "repo-root README.md is missing"
