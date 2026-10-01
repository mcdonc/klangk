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


def _hook_module():
    """Load hatch_build_package_data.py standalone for behavior tests.

    hatchling itself is not a devenv dependency (uv resolves it into
    isolated build environments), so its BuildHookInterface import is
    stubbed — the functions under test never touch the interface.
    """
    import importlib.util
    import sys
    import types

    stub_pkg = types.ModuleType("hatchling")
    stub_builders = types.ModuleType("hatchling.builders")
    stub_hooks = types.ModuleType("hatchling.builders.hooks")
    stub_plugin = types.ModuleType("hatchling.builders.hooks.plugin")
    stub_iface = types.ModuleType("hatchling.builders.hooks.plugin.interface")
    stub_iface.BuildHookInterface = type("BuildHookInterface", (), {})
    stub_pkg.builders = stub_builders
    stub_builders.hooks = stub_hooks
    stub_hooks.plugin = stub_plugin
    stub_plugin.interface = stub_iface
    sys.modules.setdefault("hatchling", stub_pkg)
    sys.modules.setdefault(stub_builders.__name__, stub_builders)
    sys.modules.setdefault(stub_hooks.__name__, stub_hooks)
    sys.modules.setdefault(stub_plugin.__name__, stub_plugin)
    sys.modules.setdefault(stub_iface.__name__, stub_iface)

    spec = importlib.util.spec_from_file_location(
        "hatch_build_package_data",
        _KLANGK_DIR / "hatch_build_package_data.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_version_script(tmp_path, body: str) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "generate-version.sh").write_text(body)


def test_hook_materializes_version_from_git(tmp_path) -> None:
    """The wheel's packaged version.json comes from generate-version.sh
    output (#3517) — the same single definition the devenv seed and the
    host image's baked /home/klangk/version.json use."""
    import json

    mod = _hook_module()
    _stub_version_script(
        tmp_path,
        '#!/usr/bin/env bash\nprintf \'%s\' \'{"version":"9.9.9","commit":"abc"}\'\n',
    )
    path = mod.materialized_version_file(tmp_path)
    assert path is not None and path.is_file()
    assert json.loads(path.read_text())["version"] == "9.9.9"


def test_hook_skips_version_generation_when_script_absent(tmp_path) -> None:
    """A wheel built from an sdist extraction has no repo, no scripts/ —
    the hook yields None and the runtime falls back to the dev block
    (#3517)."""
    assert _hook_module().materialized_version_file(tmp_path) is None


def test_hook_skips_version_generation_when_script_fails(tmp_path) -> None:
    """A failing generate-version.sh yields None (lenient, #3517) — the
    build proceeds without the packaged copy."""
    mod = _hook_module()
    _stub_version_script(tmp_path, "#!/usr/bin/env bash\nexit 1\n")
    assert mod.materialized_version_file(tmp_path) is None
