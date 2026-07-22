"""Unit tests for the binary-integrity pre-commit guard (#1734).

The guard detects UTF-8-lossy rewrites of binary files by diffing the staged
blob against HEAD and flagging any increase in the U+FFFD (``EF BF BD``) byte
sequence. These tests drive the real ``git`` plumbing against throwaway repos
so the detection logic is covered independent of this repository's own HEAD
(which, at the time the guard was added, still held the corrupt blob from the
"plugin"->"feature" sweep — the very bug the guard exists to catch).
"""

import os
import runpy
import subprocess
import sys

import pytest

# Make the scripts directory importable (mirrors test_build_pipeline.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import check_binary_integrity as cbi  # noqa: E402

REPL = bytes.fromhex("efbfbd")  # U+FFFD in UTF-8


def _git(args, cwd):
    subprocess.run(["git", *args], check=True, cwd=cwd)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """An empty git repo; CWD is chdir'd into it so the guard's bare ``git``
    subprocess calls operate on it. pytest restores CWD on teardown."""
    monkeypatch.chdir(tmp_path)
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    _git(["config", "commit.gpgsign", "false"], tmp_path)
    return tmp_path


def _commit(repo, msg="init"):
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", msg], repo)


def _stage(repo, name):
    _git(["add", name], repo)


def test_clean_binary_edit_passes(repo):
    """A legit byte-exact edit of a binary (no U+FFFD gained) is allowed."""
    p = repo / "asset.wasm"
    p.write_bytes(b"\x00asm" + b"\xff\xfe\xfd" * 5)  # non-UTF-8, no EF BF BD
    _commit(repo)
    p.write_bytes(b"\x00asm" + b"\xff\xfe\xfd" * 10)  # different, still clean
    _stage(repo, "asset.wasm")
    assert cbi.find_violations() == []


def test_lossy_rewrite_of_binary_flagged(repo):
    """The #1734 mechanism: a text-mode round-trip injects thousands of
    replacement chars into a binary — flagged as binary, any increase."""
    p = repo / "asset.wasm"
    p.write_bytes(b"\x00asm" + b"\xff\xfe\xfd" * 50)
    _commit(repo)
    # Reproduce the exact corruption: decode(lossy) -> encode.
    mangled = p.read_bytes().decode("utf-8", errors="replace").encode("utf-8")
    assert mangled.count(REPL) > 50  # sanity: the round-trip inflated REPLs
    p.write_bytes(mangled)
    _stage(repo, "asset.wasm")
    v = cbi.find_violations()
    assert len(v) == 1
    path, old, new, is_bin = v[0]
    assert path == "asset.wasm"
    assert is_bin is True
    assert old == 0  # clean HEAD baseline had no replacement chars
    assert new > 0  # the lossy rewrite injected them


def test_new_corrupt_binary_flagged(repo):
    """A brand-new (no HEAD) binary that was lossy-rewritten is flagged."""
    raw = b"\x00asm" + b"\xff\xfe\xfd" * 100
    (repo / "new.wasm").write_bytes(
        raw.decode("utf-8", errors="replace").encode("utf-8")
    )
    _stage(repo, "new.wasm")
    v = cbi.find_violations()
    assert len(v) == 1
    path, old, new, is_bin = v[0]
    assert path == "new.wasm"
    assert is_bin is True
    assert old == 0  # no prior version
    assert new > cbi.TEXT_THRESHOLD


def test_text_small_increase_not_flagged(repo):
    """Text gaining a few stray replacement chars (below threshold) is allowed
    — we only threshold-guard text, not binaries."""
    p = repo / "code.py"
    p.write_bytes(b"print('hi')\n")
    _commit(repo)
    p.write_bytes(b"print('hi')\n" + REPL * (cbi.TEXT_THRESHOLD - 1))
    _stage(repo, "code.py")
    assert cbi.find_violations() == []


def test_text_large_increase_flagged(repo):
    """Text gaining many replacement chars is flagged (lossy rewrite of a
    non-UTF-8 text blob, e.g. a lockfile with raw bytes)."""
    p = repo / "code.py"
    p.write_bytes(b"print('hi')\n")
    _commit(repo)
    p.write_bytes(b"print('hi')\n" + REPL * (cbi.TEXT_THRESHOLD + 50))
    _stage(repo, "code.py")
    v = cbi.find_violations()
    assert len(v) == 1
    assert v[0][0] == "code.py"
    assert v[0][3] is False  # text, not binary


def test_main_clean(monkeypatch):
    monkeypatch.setattr(cbi, "find_violations", lambda: [])
    assert cbi.main() == 0


def test_script_entrypoint(repo):
    """Exercise the ``__main__`` entry point in-process via runpy so coverage
    observes it. Nothing staged -> main() returns 0 -> sys.exit(0)."""
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(
            os.path.join(os.path.dirname(__file__), "..", "check_binary_integrity.py"),
            run_name="__main__",
        )
    assert exc.value.code == 0


def test_main_reports_and_fails(monkeypatch, capsys):
    monkeypatch.setattr(cbi, "find_violations", lambda: [("asset.wasm", 0, 1000, True)])
    assert cbi.main() == 1
    err = capsys.readouterr().err
    assert "EF BF BD" in err
    assert "#1734" in err
    assert "asset.wasm" in err
    assert "checkout HEAD" in err  # remediation hint present
