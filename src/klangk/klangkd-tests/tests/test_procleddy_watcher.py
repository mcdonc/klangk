"""Adversarial tests for the procleddy C watcher (#2520).

The exploit pattern under test (#2520 review): attacker-manipulated
/proc-shaped content causing the watcher to crash (segfault), hang, or
run unbounded. The watcher is pointed at fake trees via ``--root`` and
fed hostile scope lines via stdin; the tests assert:

- the process exits cleanly on SIGTERM (never hangs),
- stdout is well-formed NDJSON (a burst of garbage never yields a
  partial line or a crash),
- hostile scope input can't corrupt the roots table,
- a corrupted ppid cycle terminates (bounded walk),
- pid above the cap is ignored, not an overflow,
- a fork-burst of new pids in one poll is fully classified (parent-after-
  child ordering — the directory-order bug class).

The binary is built once per session (devenv gcc); tests skip with a
visible reason when the toolchain is absent (e.g. mac runners).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
WATCHER_SRC = REPO / "scripts" / "procleddy" / "procleddy.c"
WATCHER_BIN_PATH = REPO / "scripts" / "procleddy" / "procleddy"


def _have_cc() -> bool:
    return shutil.which("cc") is not None or shutil.which("gcc") is not None


@pytest.fixture(scope="module")
def watcher_bin(tmp_path_factory):
    if not _have_cc():
        pytest.skip("no C toolchain in this environment")
    out = tmp_path_factory.mktemp("procleddy") / "procleddy"
    subprocess.run(
        [
            "cc",
            "-O1",
            "-g",
            "-fno-omit-frame-pointer",
            "-fsanitize=address",
            "-o",
            str(out),
            str(WATCHER_SRC),
        ],
        check=True,
        capture_output=True,
    )
    return out


class FakeProc:
    """Builder for hostile /proc-shaped trees."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def mk(
        self,
        pid: int,
        ppid: int,
        name="x",
        uid=1000,
        euid=1000,
        nspid=None,
        comm=None,
        cmdline=None,
        status_extra="",
    ):
        d = self.root / str(pid)
        d.mkdir(exist_ok=True)
        if comm is None:
            comm = (name + "\n").encode()
        if cmdline is None:
            cmdline = f"/usr/bin/{name}\0--f\0".encode()
        (d / "comm").write_bytes(comm)
        (d / "cmdline").write_bytes(cmdline)
        (d / "status").write_text(
            f"Name:\t{name}\nPPid:\t{ppid}\n"
            f"Uid:\t{uid} {euid} {uid} {uid}\n"
            + (f"NSpid:\t{nspid}\n" if nspid is not None else "")
            + status_extra
        )
        return d

    def rm(self, pid: int):
        shutil.rmtree(self.root / str(pid), ignore_errors=True)

    def rmdir_shell(self, pid: int):
        """Remove the directory but leave files (hostile: unreadable)."""
        d = self.root / str(pid)
        for f in d.iterdir():
            f.chmod(0o000)
        os.chmod(d, 0o000)


def run_watcher(
    bin_path,
    root,
    scope_lines=(),
    runtime=1.0,
    interval_ms=50,
    stdin_extra=b"",
    _remove_after=None,
):
    """_remove_after: (FakeProc, pid, delay) — remove a pid mid-run."""
    import threading

    remover = None
    if _remove_after is not None:
        fp_rm, pid_rm, delay = _remove_after

        def _rm():
            time.sleep(delay)
            fp_rm.rm(pid_rm)

        remover = threading.Thread(target=_rm, daemon=True)
        remover.start()
    """Run the watcher for ~runtime seconds; returns (events, rc, stderr)."""
    proc = subprocess.Popen(
        [
            str(bin_path),
            "--root",
            str(root),
            "--interval-ms",
            str(interval_ms),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        for line in scope_lines:
            proc.stdin.write(line + b"\n")
        proc.stdin.write(stdin_extra)
        proc.stdin.flush()
        time.sleep(runtime)
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        pytest.fail("watcher did not exit on SIGTERM within 5s (hang)")
    events = []
    for line in out.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        # NDJSON contract: every emitted line must parse
        events.append(json.loads(line))
    return events, proc.returncode, err.decode("utf-8", "replace")


def test_clean_tree_births_ancestry_exit(tmp_path, watcher_bin):
    fp = FakeProc(tmp_path / "proc")
    fp.mk(100, 1, "container-init")
    fp.mk(101, 100, "bash")
    fp.mk(102, 101, "pi-agent")
    fp.mk(200, 199, "host-proc")
    events, rc, _err = run_watcher(
        watcher_bin,
        fp.root,
        scope_lines=[b'{"type":"scope","roots":[100]}'],
        runtime=0.8,
    )
    assert rc == 0, "watcher must exit 0 on SIGTERM"
    births = {e["pid"]: e for e in events if e["type"] == "birth"}
    assert set(births) == {100, 101, 102}, "host proc 200 must be excluded"
    assert births[102]["ancestry"] == [102, 101, 100]
    assert births[100]["argv"].startswith("/usr/bin/container-init")


def test_exit_event_on_removal(tmp_path, watcher_bin):
    fp = FakeProc(tmp_path / "proc")
    fp.mk(100, 1, "init")
    fp.mk(103, 100, "worker")
    # Remove 103 mid-run of the SAME watcher instance (the watcher must
    # have seen it alive first, then notice its disappearance).
    events, rc, _ = run_watcher(
        watcher_bin,
        fp.root,
        scope_lines=[b'{"type":"scope","roots":[100]}'],
        runtime=1.2,
        _remove_after=(fp, 103, 0.5),
    )
    births = [e for e in events if e["type"] == "birth" and e["pid"] == 103]
    exits = [e for e in events if e["type"] == "exit" and e["pid"] == 103]
    assert births, "103 must be born before removal"
    assert exits, "removed pid must emit exit"


def test_hostile_scope_lines_do_not_crash(tmp_path, watcher_bin):
    fp = FakeProc(tmp_path / "proc")
    fp.mk(100, 1, "init")
    hostile = [
        b'{"type":"scope","roots":[' + b"9" * 10000 + b"]}",
        b'{"type":"scope","roots":[1,2,3',
        b"\x00\x01\x02\xff\xfe garbage",
        b'{"type":"scope","roots":["' + b"A" * 100000 + b'"]}',
        b'{"type":"scope","roots":[0,-1,99999999999999999999]}',
        b"",
        b'{"roots":[100]} extra trailing',
    ]
    events, rc, err = run_watcher(
        watcher_bin, fp.root, scope_lines=hostile, runtime=0.8
    )
    assert rc == 0
    # the last valid line wins: roots should be [100] (or the last parse
    # that produced a list) and births only for 100
    births = {e["pid"] for e in events if e["type"] == "birth"}
    assert births <= {100}, f"unexpected births under hostile scope: {births}"


def test_unreadable_dirs_degrade_not_crash(tmp_path, watcher_bin):
    fp = FakeProc(tmp_path / "proc")
    fp.mk(100, 1, "init")
    # a watched child whose files are unreadable (permission-hostile)
    fp.mk(101, 100, "locked", comm=b"locked\n")
    d = fp.root / "101"
    for f in d.iterdir():
        f.chmod(0o000)
    events, rc, _ = run_watcher(
        watcher_bin,
        fp.root,
        scope_lines=[b'{"type":"scope","roots":[100]}'],
        runtime=0.8,
    )
    assert rc == 0
    # 101 either emits a birth (parsed before chmod hit) or nothing — but
    # never a malformed line, and the watcher survives.
    for e in events:
        assert isinstance(e["type"], str)


def test_ppid_cycle_terminates(tmp_path, watcher_bin):
    fp = FakeProc(tmp_path / "proc")
    fp.mk(100, 1, "init")
    # 101 <-> 102 ppid cycle (corrupted-data simulation)
    fp.mk(101, 102, "a")
    fp.mk(102, 101, "b")
    fp.mk(103, 101, "child-of-cycle")
    events, rc, _ = run_watcher(
        watcher_bin,
        fp.root,
        scope_lines=[b'{"type":"scope","roots":[100]}'],
        runtime=1.0,
        interval_ms=50,
    )
    assert rc == 0, "ppid cycle must terminate, not hang"
    # cycle members can't reach root 100 via a clean walk: no births
    births = {e["pid"] for e in events if e["type"] == "birth"}
    assert births == {100}


def test_pid_above_cap_ignored(tmp_path, watcher_bin):
    fp = FakeProc(tmp_path / "proc")
    fp.mk(100, 1, "init")
    fp.mk(1 << 21, 100, "over-cap")  # > MAX_PIDS (1<<20)
    events, rc, _ = run_watcher(
        watcher_bin,
        fp.root,
        scope_lines=[b'{"type":"scope","roots":[100]}'],
        runtime=0.8,
    )
    assert rc == 0
    births = {e["pid"] for e in events if e["type"] == "birth"}
    assert 100 in births
    assert (1 << 21) not in births


def test_fork_burst_parent_after_child(tmp_path, watcher_bin):
    """Directory order (child listed before parent) must not lose the
    child — the two-phase poll stores before emitting."""
    fp = FakeProc(tmp_path / "proc")
    fp.mk(100, 1, "init")
    # create children BEFORE parents (reverse order); directory order in
    # tmpfs reflects creation order, so this reproduces the bug class
    for pid in (105, 104, 103, 102, 101):
        fp.mk(pid, pid - 1, f"proc{pid}")
    events, rc, _ = run_watcher(
        watcher_bin,
        fp.root,
        scope_lines=[b'{"type":"scope","roots":[100]}'],
        runtime=0.8,
    )
    births = {e["pid"]: e for e in events if e["type"] == "birth"}
    assert set(births) == {100, 101, 102, 103, 104, 105}
    assert births[105]["ancestry"] == [105, 104, 103, 102, 101, 100]


def test_garbage_status_fields_do_not_crash(tmp_path, watcher_bin):
    fp = FakeProc(tmp_path / "proc")
    fp.mk(100, 1, "init")
    # hostile status content: huge PPid, no Uid line, binary comm
    d = fp.mk(101, 100, "weird")
    (d / "status").write_text(
        "Name:\t\x00\x01\x02\n"
        "PPid:\t99999999999999999999\n"
        "garbage line without colon\n"
        "Uid:\tnot-a-number\n"
    )
    (d / "comm").write_bytes(b"\xff\xfe\x00binary\n")
    (d / "cmdline").write_bytes(b"\x00\x00\x00\x00")
    events, rc, _ = run_watcher(
        watcher_bin,
        fp.root,
        scope_lines=[b'{"type":"scope","roots":[100]}'],
        runtime=0.8,
    )
    assert rc == 0
    for e in events:
        if e["type"] == "birth" and e["pid"] == 101:
            # whatever it emitted must be valid JSON strings
            assert isinstance(e["comm"], str)
            assert isinstance(e["argv"], str)


def test_no_roots_emits_nothing(tmp_path, watcher_bin):
    fp = FakeProc(tmp_path / "proc")
    fp.mk(100, 1, "init")
    events, rc, _ = run_watcher(watcher_bin, fp.root, runtime=0.8)
    assert rc == 0
    births = [e for e in events if e["type"] == "birth"]
    assert births == [], "no scope => no births (safe direction)"


def test_heartbeat_present_and_bounded(tmp_path, watcher_bin):
    fp = FakeProc(tmp_path / "proc")
    fp.mk(100, 1, "init")
    events, rc, _ = run_watcher(
        watcher_bin,
        fp.root,
        scope_lines=[b'{"type":"scope","roots":[100]}'],
        runtime=2.2,
        interval_ms=50,
    )
    beats = [e for e in events if e["type"] == "heartbeat"]
    assert beats, "heartbeat must appear within ~1s"
    for b in beats:
        assert b["poll_ms"] < 1000.0, "a single poll must stay bounded"
