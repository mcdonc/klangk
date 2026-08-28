"""Residual-path coverage for process_ledger helpers (#2520).

Small direct tests for error/race branches the integration tests can't
reach deterministically: unreadable files, malformed status lines, list
failures, watcher spawn/pipe failures, kill-on-timeout, and the
direct-parent attribution method.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from klangk import process_ledger as pl

WS = "ws-cov-0001"


# ------------------------------------------------------------------ helpers


def test_read_helpers_missing_pid(tmp_path):
    assert pl.read_comm(str(tmp_path), 1) is None
    assert pl.read_argv(str(tmp_path), 1) is None
    assert pl.read_ppid_uid(str(tmp_path), 1) is None


def test_read_ppid_uid_malformed(tmp_path):
    d = tmp_path / "1"
    d.mkdir()
    # status exists but lines are malformed / values not ints
    (d / "status").write_text("PPid:\tnot-an-int\nUid:\talso-bad\n")
    assert pl.read_ppid_uid(str(tmp_path), 1) is None
    # Uid line missing entirely
    (d / "status").write_text("PPid:\t2\n")
    assert pl.read_ppid_uid(str(tmp_path), 1) is None
    # good PPid, bad Uid value (Uid-parse failure path)
    (d / "status").write_text("PPid:\t2\nUid:\tbad bad bad\n")
    assert pl.read_ppid_uid(str(tmp_path), 1) is None


def test_read_ppid_uid_partial_then_good(tmp_path):
    d = tmp_path / "1"
    d.mkdir()
    (d / "status").write_text("PPid:\tx\nUid:\t5 5 5 5\nPPid:\t7\n")
    # first PPid fails parse -> returns None (conservative)
    assert pl.read_ppid_uid(str(tmp_path), 1) is None
    (d / "status").write_text("Uid:\t5 5 5 5\nPPid:\t7\n")
    assert pl.read_ppid_uid(str(tmp_path), 1) == (7, 5)


def test_snapshot_scan_list_failure(tmp_path):
    snap = pl._ProcSnapshot(str(tmp_path / "gone")).scan()
    assert snap.entries == {}


def test_read_argv_null_join(tmp_path):
    d = tmp_path / "1"
    d.mkdir()
    (d / "cmdline").write_bytes(b"a\0b\0\0")
    assert pl.read_argv(str(tmp_path), 1) == "a b"


# ------------------------------------------------------------------ ledger


def _app(tmp_path):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(
        process_ledger_enabled=True,
        process_ledger_interval_ms=80,
        process_ledger_fallback_interval_s=0.05,
        process_ledger_retention_seconds=1e9,
        process_ledger_retention_rows=5,
        process_ledger_watcher=str(tmp_path / "absent"),
    )
    app.state.model = types.SimpleNamespace(
        process_launch=types.SimpleNamespace()
    )
    app.state.container_registry = types.SimpleNamespace(states={})
    app.state.podman = types.SimpleNamespace(inspect_container=None)
    return app


def test_set_anchor_rejects_nonpositive(tmp_path):
    led = pl.ProcessLedger(_app(tmp_path))
    led.set_anchor(0, "agent", WS)
    led.set_anchor(-3, "agent", WS)
    assert not led._canchors


def test_resolve_anchors_skips_missing_root(tmp_path):
    led = pl.ProcessLedger(_app(tmp_path))
    led.set_anchor(1, "agent", WS)  # no root registered for WS
    led.resolve_anchors()
    assert not led._anchors


def test_resolve_anchors_nspid_none(tmp_path, monkeypatch):
    led = pl.ProcessLedger(_app(tmp_path))
    led.set_root(WS, 900)
    led.set_anchor(5, "agent", WS)

    class _Snap:
        root = "/does/not/exist"  # _read_nspid_tail fails -> continue

        def scan(self):
            # 900 -> root; NSpid read fails for it
            self.entries = {900: (899, 0)}
            return self

    monkeypatch.setattr(pl, "_ProcSnapshot", lambda root="/proc": _Snap())
    led.resolve_anchors()
    assert not led._anchors


def test_attribute_direct_parent_method(tmp_path):
    led = pl.ProcessLedger(_app(tmp_path))
    led._anchors[101] = ("agent", WS)
    assert led.attribute(102, 101, WS, 1.0) == ("agent", "anchor")
    # anchor for a different workspace
    led._anchors[202] = ("user:x", "other")
    assert led.attribute(203, 202, WS, 1.0) == ("", None)
    # no anchor at all
    assert led.attribute(1, 2, WS, 1.0) == ("", None)


@pytest.mark.asyncio
async def test_start_watcher_spawn_oserror(tmp_path):
    # A path that exists but cannot be executed (a directory)
    app = _app(tmp_path)
    app.state.settings.process_ledger_watcher = str(tmp_path)
    led = pl.ProcessLedger(app)
    ok = await led._start_watcher()
    assert ok is False


@pytest.mark.asyncio
async def test_push_scope_broken_pipe(tmp_path):
    led = pl.ProcessLedger(_app(tmp_path))
    led.set_root(WS, 5)

    class _Stdin:
        def is_closing(self):
            return False

        def write(self, b):  # pragma: no cover - not reached
            raise BrokenPipeError

        async def drain(self):
            raise BrokenPipeError

    class _P:
        stdin = _Stdin()
        returncode = None

    led._watcher_proc = _P()
    # must not raise
    await led._push_scope()


@pytest.mark.asyncio
async def test_stop_watcher_kills_on_timeout(tmp_path):
    led = pl.ProcessLedger(_app(tmp_path))
    killed = []

    class _P:
        returncode = None

        def send_signal(self, sig):
            pass

        def kill(self):
            killed.append(1)

        async def wait(self):
            await asyncio.sleep(60)  # never exits -> timeout path

    led._watcher_proc = _P()
    await led._stop_watcher()
    assert killed, "timeout must fall back to kill"


@pytest.mark.asyncio
async def test_fallback_poll_skips_unanchored_pid(tmp_path, monkeypatch):
    app = _app(tmp_path)

    class _Rec:
        def __init__(self):
            self.rows = []

        async def record_launch(self, **kw):
            self.rows.append(kw)

    rec = _Rec()
    app.state.model.process_launch = rec
    led = pl.ProcessLedger(app)
    led.set_root(WS, 900)

    class _Snap:
        root = "/procx"

        def __init__(self, entries):
            self.entries = entries

        def scan(self):
            return self

    # prev: root only. next: root + a HOST pid that doesn't walk to root
    replay = _SeqSnap(
        [_Snap({900: (899, 0)}), _Snap({900: (899, 0), 4242: (1, 0)})]
    )
    monkeypatch.setattr(pl, "_ProcSnapshot", lambda root="/proc": replay)
    # baseline = root only, so the diff pass sees the new host pid 4242
    led._prev_seen = {900}
    await led._fallback_poll_once()
    assert rec.rows == [], "pid 4242 (host, ppid 1) must not be recorded"


class _SeqSnap:
    def __init__(self, snaps):
        self._snaps = snaps
        self._i = 0

    def __call__(self, root="/proc"):
        return self

    def scan(self):
        s = self._snaps[min(self._i, len(self._snaps) - 1)]
        self._i += 1
        return s


@pytest.mark.asyncio
async def test_fallback_poll_unanchored_workspace_pid(tmp_path, monkeypatch):
    """New pid walks to the root but no anchor matches -> unknown/fallback."""
    app = _app(tmp_path)

    class _Rec:
        def __init__(self):
            self.rows = []

        async def record_launch(self, **kw):
            self.rows.append(kw)

    rec = _Rec()
    app.state.model.process_launch = rec
    led = pl.ProcessLedger(app)
    led.set_root(WS, 900)

    class _Snap:
        root = "/procx"

        def __init__(self, entries):
            self.entries = entries

        def scan(self):
            return self

    snaps = [_Snap({900: (899, 0)}), _Snap({900: (899, 0), 901: (900, 0)})]
    replay = _SeqSnap(snaps)  # ONE instance: the index must persist
    monkeypatch.setattr(pl, "_ProcSnapshot", lambda root="/proc": replay)
    # baseline already contains only the root; the refresh pass consumes
    # snap 1 (stale-anchor prune), the diff pass consumes snap 2
    led._prev_seen = {900}
    await led._fallback_poll_once()
    assert len(rec.rows) == 1
    assert rec.rows[0]["principal"] == "unknown"
    assert rec.rows[0]["attribution_method"] == "fallback"


@pytest.mark.asyncio
async def test_workspace_for_ancestry_pid_is_root(tmp_path):
    led = pl.ProcessLedger(_app(tmp_path))
    led.set_root(WS, 900)
    assert led._workspace_for_ancestry(900, []) == (WS, [])
    assert led._workspace_for_ancestry(1, []) == (None, [])


def test_nspid_tail_bad_values(tmp_path):
    d = tmp_path / "1"
    d.mkdir()
    (d / "status").write_text("NSpid:\tnot-an-int\n")
    assert pl._read_nspid_tail(1, str(tmp_path)) is None
    (d / "status").write_text("Name:\tno-nspid\n")
    assert pl._read_nspid_tail(1, str(tmp_path)) is None
    assert pl._read_nspid_tail(2, str(tmp_path)) is None  # missing file
    (d / "status").write_text("NSpid:\t7 5\n")
    assert pl._read_nspid_tail(1, str(tmp_path)) == 5
