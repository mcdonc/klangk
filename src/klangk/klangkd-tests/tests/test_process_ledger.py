"""Unit tests for the process-launch ledger Python side (#2520).

Covers the ProcessLedger subsystem (attribution, anchors, fallback
poller against a fake /proc tree, retention) and the
ProcessLaunchModel CRUD against the per-test DB.
"""

from __future__ import annotations

import asyncio
import time
import types
from pathlib import Path

import pytest

from klangk import process_ledger as pl

WS = "ws-ledger-test-0001"


class _ModelStub:
    def __init__(self):
        self.launches = []

    async def record_launch(self, **kw):
        self.launches.append(kw)
        return {"id": "x", **kw}


class _ModelWrap:
    def __init__(self, inner):
        self.process_launch = inner


def _app(tmp_path, *, enabled=True, watcher="/nonexistent/procleddy"):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(
        process_ledger_enabled=enabled,
        process_ledger_interval_ms=80,
        process_ledger_fallback_interval_s=0.05,
        process_ledger_retention_seconds=1.0,
        process_ledger_retention_rows=100,
        process_ledger_watcher=watcher,
    )
    model_stub = _ModelStub()
    app.state.model = _ModelWrap(model_stub)
    app.state.container_registry = types.SimpleNamespace(states={})
    app.state.podman = types.SimpleNamespace(
        inspect_container=asyncio.coroutine(lambda cid: None)
        if False
        else _InspectNone()
    )
    return app, model_stub


class _InspectNone:
    async def __call__(self, cid):  # noqa: ARG002
        return None


# ---------------------------------------------------------------- lib


def test_input_hint_freshness():
    app, _ = _app("/tmp")
    led = pl.ProcessLedger(app)
    led.note_input(WS, "alice")
    assert led.input_hint(WS, time.time()) == "alice"
    assert led.input_hint(WS, time.time() + 31.0) is None


def test_attribute_with_ancestry():
    app, _ = _app("/tmp")
    led = pl.ProcessLedger(app)
    led._anchors[55] = ("agent", WS)
    led._anchors[77] = ("user:bob", "other-ws")
    assert led.attribute_with_ancestry([1, 2, 55], WS) == ("agent", "anchor")
    # anchor from a different workspace must not match
    assert led.attribute_with_ancestry([77], WS) == ("", None)


def test_drop_root_clears_state():
    app, _ = _app("/tmp")
    led = pl.ProcessLedger(app)
    led.set_root(WS, 100)
    led._anchors[101] = ("agent", WS)
    led.note_input(WS, "alice")
    led.drop_root(WS)
    assert WS not in led._roots
    assert not led._anchors
    assert WS not in led._last_input


def test_workspace_for_pid_walk():
    app, _ = _app("/tmp")
    led = pl.ProcessLedger(app)
    led.set_root(WS, 100)
    entries = {102: (101, 1000), 101: (100, 1000), 100: (1, 0)}
    ws, chain = led.workspace_for_pid(102, entries)
    assert ws == WS
    assert chain == [102, 101, 100]  # root-inclusive chain
    assert led.workspace_for_pid(500, entries)[0] is None


# ------------------------------------------------------ fallback poller


class FakeTree:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._next = 500

    def mk(self, ppid, name="proc"):
        pid = self._next
        self._next += 1
        d = self.root / str(pid)
        d.mkdir(exist_ok=True)
        (d / "comm").write_text(name + "\n")
        (d / "cmdline").write_bytes(f"/usr/bin/{name}\0".encode())
        (d / "status").write_text(
            f"Name:\t{name}\nPPid:\t{ppid}\nUid:\t1000 1000 1000 1000\n"
        )
        return pid


@pytest.mark.asyncio
async def test_fallback_poller_records_births(tmp_path, monkeypatch):
    app, model = _app(tmp_path)
    led = pl.ProcessLedger(app)
    tree = FakeTree(tmp_path / "proc")
    root_pid = tree.mk(1, "container-init")
    led.set_root(WS, root_pid)
    snap = pl._ProcSnapshot(str(tree.root)).scan()
    led._prev_seen = set(snap.entries)
    child = tree.mk(root_pid, "agent-proc")
    # anchor the root as agent so the child attributes
    led._anchors[root_pid] = ("agent", WS)
    monkeypatch.setattr(pl, "_ProcSnapshot", lambda root="/proc": snap)
    # second scan picks up the new child only
    snap2 = pl._ProcSnapshot(str(tree.root)).scan()
    replay = _Seq([snap, snap2])  # ONE instance: index persists
    monkeypatch.setattr(pl, "_ProcSnapshot", lambda root="/proc": replay)
    await led._fallback_poll_once()
    assert len(model.launches) == 1
    row = model.launches[0]
    assert row["pid"] == child
    assert row["principal"] == "agent"
    assert row["event_kind"] == "birth"
    assert row["comm"] == "agent-proc"
    assert row["argv"] == "/usr/bin/agent-proc"


class _Seq:
    """Snapshot factory replaying a fixed scan sequence."""

    def __init__(self, snaps):
        self._snaps = list(snaps)
        self._i = 0

    def __call__(self, root="/proc"):
        return self

    def scan(self):
        s = self._snaps[min(self._i, len(self._snaps) - 1)]
        self._i += 1
        return s


@pytest.mark.asyncio
async def test_watcher_event_birth_attributed(tmp_path):
    app, model = _app(tmp_path)
    led = pl.ProcessLedger(app)
    led.set_root(WS, 100)
    led._anchors[101] = ("agent", WS)
    await led._handle_event(
        {
            "type": "birth",
            "pid": 102,
            "ppid": 101,
            "uid": 1000,
            "comm": "pi",
            "argv": "/usr/bin/pi -m rpc",
            "ancestry": [102, 101, 100],
            "ts_realtime": 123.456,
        }
    )
    assert len(model.launches) == 1
    row = model.launches[0]
    assert row["principal"] == "agent"
    assert row["attribution_method"] == "anchor"
    assert row["pid"] == 102
    assert row["argv"] == "/usr/bin/pi -m rpc"
    assert row["started_at"] == 123.456


@pytest.mark.asyncio
async def test_watcher_event_euid_change_logged_not_row(tmp_path):
    app, model = _app(tmp_path)
    led = pl.ProcessLedger(app)
    led.set_root(WS, 100)
    await led._handle_event(
        {
            "type": "euid_change",
            "pid": 102,
            "old_euid": 1000,
            "new_euid": 0,
            "ancestry": [102, 100],
        }
    )
    assert model.launches == [], "euid_change is alarm-surface, not a row"


@pytest.mark.asyncio
async def test_heartbeat_updates_effective_interval(tmp_path):
    app, _ = _app(tmp_path)
    led = pl.ProcessLedger(app)
    await led._handle_event({"type": "heartbeat", "interval_ms": 82.5})
    assert led.effective_interval_ms == 82.5
    st = led.status()
    assert st["effective_interval_ms"] == 82.5


def test_status_shape(tmp_path):
    app, _ = _app(tmp_path)
    led = pl.ProcessLedger(app)
    st = led.status()
    assert set(st) == {
        "enabled",
        "backend",
        "effective_interval_ms",
        "started_at",
        "roots",
        "anchors",
        "gaps",
    }


@pytest.mark.asyncio
async def test_anchor_translation_via_nspid(tmp_path, monkeypatch):
    """set_anchor stores container pids; resolve_anchors translates via
    NSpid tails within the workspace subtree."""
    app, model = _app(tmp_path)
    led = pl.ProcessLedger(app)
    led.set_root(WS, 900)
    led.set_anchor(5, "agent", WS)  # container pid 5
    # fake /proc with two processes: host 900 (root, nspid tail 1) and
    # host 901 (nspid tail 5 = the pane shell)
    proot = tmp_path / "proc"
    proot.mkdir()
    for pid, ppid, tail in ((900, 899, 1), (901, 900, 5)):
        d = proot / str(pid)
        d.mkdir()
        (d / "comm").write_text("sh\n")
        (d / "cmdline").write_bytes(b"/bin/sh\0")
        (d / "status").write_text(
            f"Name:\tsh\nPPid:\t{ppid}\nUid:\t1000 1000 1000 1000\n"
            f"NSpid:\t{pid} {tail}\n"
        )
    snap = pl._ProcSnapshot(str(proot)).scan()
    monkeypatch.setattr(pl, "_ProcSnapshot", lambda root="/proc": _Once(snap))
    led.resolve_anchors()
    assert led._anchors.get(901) == ("agent", WS)

    # a birth whose ancestry includes host 901 now attributes
    await led._handle_event(
        {
            "type": "birth",
            "pid": 902,
            "ppid": 901,
            "uid": 1000,
            "comm": "worker",
            "argv": "/usr/bin/worker",
            "ancestry": [902, 901, 900],
            "ts_realtime": 1.0,
        }
    )
    assert model.launches[-1]["principal"] == "agent"


class _Once:
    def __init__(self, snap):
        self._snap = snap

    def __call__(self, root="/proc"):
        return self

    def scan(self):
        return self._snap


# ------------------------------------------------------------ model


@pytest.mark.asyncio
async def test_model_crud_and_prune(app_state, db, user):  # noqa: ARG001
    ws = await app_state.state.model.workspaces.create_workspace(
        user["id"], "ledger-ws"
    )
    model = app_state.state.model.process_launch
    row = await model.record_launch(
        workspace_id=ws["id"],
        pid=1,
        ppid=0,
        uid=1000,
        comm="c",
        argv="a b",
        started_at=100.0,
        principal="agent",
        attribution_method="anchor",
    )
    assert row["id"]
    rows = await model.list_launches(ws["id"])
    assert len(rows) == 1
    assert rows[0]["argv"] == "a b"
    assert await model.count_launches(ws["id"]) == 1
    # prune by age: cutoff before started_at
    n = await model.prune(keep_rows=1000, keep_seconds=1.0)
    assert n == 1
    assert await model.count_launches(ws["id"]) == 0
