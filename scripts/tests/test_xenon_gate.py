"""Contract tests for the strict xenon gate wrapper (#3415).

xenon exits 0 when its parser cannot read a graded file: it logs a
"cannot parse" WARNING and silently drops the file from the complexity
gate. That bit when ruff 0.15's PEP 758 rewrite (``except A, B:`` on
the py3.14 target) met nixpkgs' python3.13-built xenon — converted
files left the gate with no signal anywhere. ``scripts/xenon-gate.sh``
(the single invocation behind both the pre-commit hook and the
``klangk:xenon`` devenv task) turns any such skip into a hard failure.
These tests pin the wiring and prove the loud behavior; the graded-set
run is the CI-side guard the issue asks for (backend-tests.yml runs
this suite on stock runners, where xenon comes from the ``test`` extra).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE = _REPO_ROOT / "scripts" / "xenon-gate.sh"
_DEVENV_NIX = _REPO_ROOT / "devenv.nix"

# A file no Python parser accepts: exercises the wrapper's loud path
# without depending on any specific syntax gap.
_UNPARSEABLE = "def (:\n"


def _run_gate(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(_GATE), *args],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )


def test_script_exists_and_is_executable():
    assert _GATE.is_file(), "scripts/xenon-gate.sh is missing"
    mode = _GATE.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/xenon-gate.sh must be executable"
    first = _GATE.read_text().splitlines()[0]
    assert first.startswith("#!"), "scripts/xenon-gate.sh needs a shebang"


def test_devenv_hook_and_task_delegate_to_the_wrapper():
    """devenv.nix must run the wrapper for both the hook and the task."""
    nix = _DEVENV_NIX.read_text()
    assert 'entry = "scripts/xenon-gate.sh"' in nix, (
        "the xenon pre-commit hook must run scripts/xenon-gate.sh"
    )
    assert '"$DEVENV_ROOT/scripts/xenon-gate.sh"' in nix, (
        "the klangk:xenon task must run scripts/xenon-gate.sh"
    )


def test_wrapper_defines_thresholds_and_graded_set():
    """One definition of the gate: the rank A thresholds and the graded
    git ls-files globs live in the wrapper script."""
    script = _GATE.read_text()
    for part in (
        "--max-absolute A",
        "--max-modules A",
        "--max-average A",
        "'src/klangk/klangk/*.py'",
        "'src/klangksidecar/klangksidecar/*.py'",
        "'scripts/*.py'",
    ):
        assert part in script, f"{part} is missing from the gate wrapper"


def test_devenv_carries_no_second_gate_definition():
    """devenv.nix must delegate, not duplicate: a second copy of the
    thresholds there could drift from the wrapper's."""
    nix = _DEVENV_NIX.read_text()
    assert "--max-absolute" not in nix, (
        "gate thresholds are defined in scripts/xenon-gate.sh; devenv.nix "
        "must not carry a second (drifting) copy"
    )


def test_gate_fails_loudly_on_unparseable_file(tmp_path):
    """The regression the wrapper exists for: plain xenon exits 0 here."""
    bad = tmp_path / "broken.py"
    bad.write_text(_UNPARSEABLE)
    done = _run_gate(str(bad))
    assert done.returncode != 0, (
        "xenon silently skips (exit 0) files it cannot parse; the "
        "wrapper must turn that into a failure"
    )
    assert "cannot parse" in done.stderr + done.stdout, (
        "the failure must name the skipped file"
    )


def test_gate_passes_the_graded_set():
    """The full graded tree passes the strict gate — CI's loud guard."""
    done = _run_gate()
    assert done.returncode == 0, done.stderr + done.stdout
