"""Contract tests for the fmtk harness (#2881).

``scripts/fmtk-up.sh`` (the ``fmtk-up`` devenv script) composes the scratch
backend, origin-splitting proxy, fixture seed, and debug flutter run that an
agent needs to drive the live frontend with fmtk; ``scripts/fmtk-chrome.sh``
is the CHROME_EXECUTABLE URL-rewriting wrapper; ``scripts/fmtk-seed.py``
(``fmtk-seed``) seeds the fixture; ``scripts/fmtk-down.sh`` (``fmtk-down``)
stops the services fmtk-up keeps alive. These grep-style tests pin the
wiring so a future edit that silently drops a piece is loud — in the spirit
of ``test_test_push_task.py``.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEVENV_NIX = _REPO_ROOT / "devenv.nix"
_UP = _REPO_ROOT / "scripts" / "fmtk-up.sh"
_DOWN = _REPO_ROOT / "scripts" / "fmtk-down.sh"
_CHROME = _REPO_ROOT / "scripts" / "fmtk-chrome.sh"
_SEED = _REPO_ROOT / "scripts" / "fmtk-seed.py"
_AGENTS = _REPO_ROOT / "AGENTS.md"


def test_shell_scripts_exist_and_are_executable():
    for script in (_UP, _DOWN, _CHROME):
        assert script.is_file(), f"{script} is missing"
        mode = script.stat().st_mode
        assert mode & stat.S_IXUSR, f"{script} must be executable"
        first = script.read_text().splitlines()[0]
        assert first.startswith("#!"), f"{script} needs a shebang"


def test_seed_script_exists():
    assert _SEED.is_file(), "scripts/fmtk-seed.py is missing"


def test_devenv_wires_the_scripts():
    nix = _DEVENV_NIX.read_text()
    for name, path in (
        ("fmtk-up", "fmtk-up.sh"),
        ("fmtk-down", "fmtk-down.sh"),
        ("fmtk-seed", "fmtk-seed.py"),
    ):
        assert f"scripts.{name}.exec" in nix, (
            f"devenv.nix no longer defines scripts.{name}.exec"
        )
        assert f"scripts/{path}" in nix, f"{name} must delegate to the script"


def assert_proxy_split(up: str) -> None:
    """same-origin split: /api and /ws to the backend, rest to the dev
    server."""
    assert "handle /api/*" in up and "handle /ws" in up, (
        "the proxy must route /api/* and /ws to the backend"
    )


def test_up_boots_all_pieces():
    """The harness must compose backend, proxy, seed, and flutter run."""
    up = _UP.read_text()
    assert "klangk.main --config" in up, "fmtk-up must boot a scratch klangkd"
    assert "caddy run" in up, "fmtk-up must start the origin-splitting proxy"
    assert "fmtk-seed.py" in up, "fmtk-up must run the fixture seed"
    assert "flutter run --debug -d chrome" in up, (
        "fmtk-up must start the debug flutter run"
    )
    assert_proxy_split(up)


def test_up_reuses_kept_services_for_fast_relaunch():
    """Launch speed (#2881): the harness must reuse a healthy kept
    backend + proxy instead of re-booting them, and must not re-run pub
    get when deps are already resolved."""
    up = _UP.read_text()
    assert "backend_is_ours_and_healthy" in up, (
        "fmtk-up must health-check OUR scratch backend before reusing it"
    )
    assert "reusing running scratch klangkd" in up, "backend reuse path"
    assert "reusing caddy proxy" in up, "proxy reuse path"
    assert "--no-pub" in up, "skip pub get when package_config is fresh"


def assert_no_debug_port_flag(chrome: str) -> None:
    """The wrapper must not add --remote-debugging-port (flutter's own flag
    wins; the port is discovered from the process list instead)."""
    active = [ln for ln in chrome.splitlines() if not ln.lstrip().startswith("#")]
    assert not any("--remote-debugging-port" in ln for ln in active)


def test_up_uses_the_chrome_wrapper():
    """The debug run must load the app through the proxy via the wrapper."""
    assert "fmtk-chrome.sh" in _UP.read_text(), (
        "fmtk-up must set CHROME_EXECUTABLE to the URL-rewriting wrapper"
    )
    chrome = _CHROME.read_text()
    assert "8124" in chrome, "the wrapper must rewrite to the proxy origin"
    assert_no_debug_port_flag(chrome)


def test_down_stops_the_right_processes():
    """fmtk-down must target only harness-owned processes (bracket-trick
    patterns, state-dir-scoped) and support --wipe."""
    down = _DOWN.read_text()
    for pattern in (
        "run --debug -d chrome",
        "[c]hrome.*127.0.0.1:8124",
        "klangk[.]main --config",
        "[c]addy run --config",
        "--wipe",
    ):
        assert pattern in down, f"fmtk-down must handle: {pattern}"


def assert_removed_fixtures_absent(seed: str) -> None:
    """The pre-#2881 fixture names are gone."""
    for removed in ("fmtk-sharer", "fmtk-acler", "fmtk-viewer"):
        assert removed not in seed, f"the {removed} fixture was removed"


def assert_role_buckets_seeded(seed: str) -> None:
    """One fixture member per role bucket."""
    for role in ("collaborators", "coders", "spectators"):
        assert f'"{role}"' in seed, f"a fixture must sit in {role}"


def test_seed_matrix_wiring():
    """Fixture wiring: admin-group owner plus one member per role bucket.

    The workspace must be created with fmtk-admin's token so it owns it
    (``GET /workspaces`` lists only owned workspaces). Every role group
    carries ``terminal``, so each member opens the workspace page (the WS
    ``workspace_connect`` gate requires it — #2881 pain point 2).
    """
    seed = _SEED.read_text()
    assert "fmtk-admin" in seed and '"admins"' in seed, (
        "fmtk-admin must exist and join the admins group"
    )
    assert_removed_fixtures_absent(seed)
    assert_role_buckets_seeded(seed)
    assert "owner_token" in seed, (
        "the workspace must be created with fmtk-admin's token (ownership)"
    )


def test_agents_documents_the_harness():
    agents = _AGENTS.read_text()
    assert "fmtk-up" in agents, "AGENTS.md must point at the fmtk-up harness"
    assert "fmtk-down" in agents, "AGENTS.md must document fmtk-down"
