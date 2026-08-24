"""Contract tests for the test-push task (#2727).

``scripts/test-push.sh`` (wired up as the ``test-push`` devenv task) is the
fast local pre-push gate: it diffs against the merge-base with the default
branch and runs only the suites whose area changed. These grep-style tests
pin the wiring so a future edit that silently drops an area (or the
devenv.nix delegation) is loud — in the spirit of
``test_podman_registries_conf.py``.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEVENV_NIX = _REPO_ROOT / "devenv.nix"
_SCRIPT = _REPO_ROOT / "scripts" / "test-push.sh"


def test_script_exists_and_is_executable():
    assert _SCRIPT.is_file(), "scripts/test-push.sh is missing"
    mode = _SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/test-push.sh must be executable"
    first = _SCRIPT.read_text().splitlines()[0]
    assert first.startswith("#!"), "scripts/test-push.sh needs a shebang"


def test_devenv_wires_the_task():
    """devenv.nix must define test-push delegating to the script."""
    nix = _DEVENV_NIX.read_text()
    assert "scripts.test-push.exec" in nix, (
        "devenv.nix no longer defines scripts.test-push.exec"
    )
    assert "scripts/test-push.sh" in nix, (
        "the test-push task must delegate to scripts/test-push.sh"
    )


def test_script_covers_every_area():
    """All four selection areas must stay classified.

    Dropping a ``case`` arm silently skips a whole suite area on push.
    """
    script = _SCRIPT.read_text()
    for pattern in (
        "src/klangk/*",
        "src/klangksidecar/*",
        "src/frontend/*",
        "scripts/* | src/containers/*",
    ):
        assert pattern in script, (
            f"test-push.sh no longer classifies {pattern!r} — its area "
            "would never be selected"
        )


def test_script_uses_merge_base():
    """Selection must be merge-base based, not a plain diff.

    A plain ``git diff origin/main`` from a stale branch counts main-side
    changes as branch changes (over-selection); a hard ref would miss
    uncommitted work. The merge-base + working-tree diff (plus untracked
    files) is the intended semantics.
    """
    script = _SCRIPT.read_text()
    assert "merge-base" in script
    assert "origin/main" in script
    assert "ls-files --others" in script, (
        "untracked files must count — a new uncommitted test file must "
        "still select its area"
    )
