"""Contract test: the update-features task declares its venv prerequisite.

``klangk:update-features`` execs through ``${venvPython}`` (the uv venv
at ``.devenv/state/venv``), so it cannot run before the venv exists. A
bare ``devenv tasks run`` — no shell entry, which is what a ``devenv
shell -- true`` prepare step would provide — runs no
``devenv:python:virtualenv`` / ``klangk:uv-sync`` on its own. On the
self-hosted nix runner (fuzz-daily.yml's Build images step after #3584,
fresh JIT VM, wiped workdir) the task wrapper died with::

    klangk-update-features: line 5: .devenv/state/venv/bin/python:
    No such file or directory

and both image builds failed dependency-failed before the fuzzer ever
started (#3591). The declared ``after = [ "klangk:uv-sync" ]`` pulls the
venv creation into any task graph containing update-features — uv-sync
itself runs ``after`` ``devenv:python:virtualenv``, so the chain is
complete.

Pure file-content contract on devenv.nix (the fmtk-harness test's
devenv.nix pinning pattern): if this fails, a devenv.nix edit dropped
the edge; fix the task graph, not the test.
"""

import re
from pathlib import Path

_DEVENV_NIX = Path(__file__).resolve().parents[2] / "devenv.nix"

_TASK_BLOCK = re.compile(r'"klangk:update-features"\s*=\s*\{(.*?)\n    \};', re.DOTALL)
# Structural, not exact-format: nixfmt --width=80 rewraps long attr
# lists (a grown `after` list must not false-red the pin) and a
# commented-out line must not false-pass it.
_VENV_PREREQ = re.compile(r'after\s*=\s*\[[^\]]*"klangk:uv-sync"')
_VENV_EXEC = re.compile(r"exec\s+\$\{venvPython\}")


def update_features_block() -> str:
    """The task's attr body, `#`-comment lines dropped — a commented-out
    edge is exactly the regression this contract exists to catch."""
    nix = _DEVENV_NIX.read_text()
    match = _TASK_BLOCK.search(nix)
    assert match, "devenv.nix no longer defines the klangk:update-features task"
    return "\n".join(
        line
        for line in match.group(1).splitlines()
        if not line.lstrip().startswith("#")
    )


def test_update_features_declares_uv_sync_prerequisite():
    block = update_features_block()
    assert _VENV_PREREQ.search(block), (
        "klangk:update-features execs ${venvPython} but does not run "
        "after klangk:uv-sync — a bare `devenv tasks run` (no shell "
        "entry) finds no venv and dies (#3591)"
    )


def test_update_features_execs_through_the_venv_python():
    """The prerequisite exists because the exec needs the venv — pin the
    exec too, so removing ${venvPython} from the task invalidates the
    prerequisite test's premise instead of silently passing both."""
    block = update_features_block()
    assert _VENV_EXEC.search(block), (
        "klangk:update-features no longer execs ${venvPython}; revisit "
        "the uv-sync prerequisite (#3591)"
    )
