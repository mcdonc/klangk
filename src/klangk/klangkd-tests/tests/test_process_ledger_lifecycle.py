"""Lifecycle + watcher-integration tests for ProcessLedger (#2520).

Complements test_process_ledger.py (which covers attribution and the
fallback poller in isolation): these exercise start/stop, the C-watcher
subprocess path (against a fake watcher script), restart-on-crash bounds,
scope pushes, root refresh, retention, and the status surface — the code
the 100% coverage gate demands.
"""

from __future__ import annotations

import asyncio
import json
import time
import types
from pathlib import Path

import pytest

from klangk import process_ledger as pl

WS = "ws-ledger-lc-0001"


class _LaunchRecorder:
    def __init__(self):
        self.launches = []

    async def record_launch(self, **kw):
        self.launches.append(kw)
        return {"id": "r", **kw}

    async def prune(self, *, keep_rows, keep_seconds):  # noqa: ARG002
        return 0


def _app(tmp_path, *, enabled=True, watcher=None, backend="proc"):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(
        process_ledger_enabled=enabled,
        process_ledger_interval_ms=80,
        process_ledger_fallback_interval_s=0.05,
        process_ledger_retention_seconds=1e9,
        process_ledger_retention_rows=100000,
        process_ledger_watcher=(
            str(tmp_path / "absent-procleddy")
            if watcher is None
            else watcher
        ),
        process_ledger_backend=backend,
    )
    recorder = _LaunchRecorder()
    app.state.model = types.SimpleNamespace(process_launch=recorder)
    app.state.container_registry = types.SimpleNamespace(states={})
    app.state.podman = types.SimpleNamespace(inspect_container=None)
    return app, recorder


FAKE_WATCHER = r"""#!/usr/bin/env python3
import json, sys, time
sys.stdin.buffer.read(1)  # wait for scope write (blocks until first byte)
print(json.dumps({"type": "snapshot_start", "ts": 1.0}), flush=True)
print(json.dumps({"type": "heartbeat", "interval_ms": 80.0}), flush=True)
time.sleep(30)
"""

CRASH_WATCHER = r"""#!/usr/bin/env python3
import sys
sys.stdin.buffer.read(1)
sys.exit(1)
"""


@pytest.mark.asyncio
async def test_start_disabled_is_noop(tmp_path):
    app, _ = _app(tmp_path, enabled=False)
    led = pl.ProcessLedger(app)
    await led.start()
    assert led._task is None
    assert led.backend == "stopped"


@pytest.mark.asyncio
async def test_start_fallback_when_watcher_missing(tmp_path, caplog):
    app, _ = _app(tmp_path)  # watcher path absent
    led = pl.ProcessLedger(app)
    with caplog.at_level("INFO", logger="klangk.process_ledger"):
        await led.start()
    assert led.backend == "python-fallback"
    assert led.effective_interval_ms == pytest.approx(50.0)
    assert led.started_at is not None
    assert "capture running — Python poller" in caplog.text
    await led.stop()
    assert led._task is None


@pytest.mark.asyncio
async def test_start_with_watcher_and_event_flow(tmp_path, caplog):
    wpath = tmp_path / "fakewatcher"
    wpath.write_text(FAKE_WATCHER)
    wpath.chmod(0o755)
    app, rec = _app(tmp_path, watcher=str(wpath))
    led = pl.ProcessLedger(app)
    led.set_root(WS, 100)
    with caplog.at_level("INFO", logger="klangk.process_ledger"):
        await led.start()
    assert led.backend == "c-watcher"
    assert "capture running — C watcher pid" in caplog.text
    # push an event through the reader
    proc = led._watcher_proc
    assert proc is not None
    # feed one birth line by writing to... the reader reads stdout of the
    # fake watcher; instead drive _handle_event directly through the
    # reader's consumption path via the watcher script output (snapshot +
    # heartbeat already sent). Give the reader a moment.
    await asyncio.sleep(0.3)
    assert led.effective_interval_ms == pytest.approx(80.0)
    # Set the anchor *after* start so the background _refresh_roots /
    # prune_stale_anchors cycle doesn't discard the fake host-pid before
    # the event is handled.
    led._anchors[101] = ("agent", WS)
    # simulate an event arriving on stdout via the public handler
    await led._handle_event(
        {
            "type": "birth",
            "pid": 102,
            "ppid": 101,
            "uid": 1000,
            "comm": "agent",
            "argv": "/agent",
            "ancestry": [102, 101, 100],
            "ts_realtime": 1.0,
        }
    )
    assert rec.launches[-1]["principal"] == "agent"
    await led.stop()
    assert led._watcher_proc is None


@pytest.mark.asyncio
async def test_watcher_exit_marks_gap_and_restarts(tmp_path):
    wpath = tmp_path / "crashwatcher"
    wpath.write_text(CRASH_WATCHER)
    wpath.chmod(0o755)
    app, _ = _app(tmp_path, watcher=str(wpath))
    led = pl.ProcessLedger(app)
    await led.start()
    assert led.backend == "c-watcher"
    # watcher reads one byte then exits -> reader notices -> restart
    for _ in range(40):
        if len(led._watcher_restarts) >= 1 or led.gaps:
            break
        await asyncio.sleep(0.05)
    assert led.gaps, "crash must mark a coverage gap"
    await led.stop()


@pytest.mark.asyncio
async def test_on_watcher_exit_falls_back_after_burst(tmp_path):
    app, _ = _app(tmp_path)
    led = pl.ProcessLedger(app)
    now = time.time()
    led._watcher_restarts = [
        now - 10,
        now - 5,
        now - 1,
    ]
    led.gaps = []
    await led._on_watcher_exit()
    assert led.backend == "python-fallback"


@pytest.mark.asyncio
async def test_reconfigure_disable_stops(tmp_path):
    app, _ = _app(tmp_path)
    led = pl.ProcessLedger(app)
    await led.start()
    assert led._task is not None
    led.reconfigure(app)  # still enabled: no-op
    app.state.settings.process_ledger_enabled = False
    led.reconfigure(app)
    for _ in range(40):
        if led._task is None:
            break
        await asyncio.sleep(0.05)
    assert led._task is None


@pytest.mark.asyncio
async def test_refresh_roots_via_inspect(tmp_path):
    app, _ = _app(tmp_path)

    class _Insp:
        def __init__(self):
            self.info = {
                "State": {"Running": True, "Pid": 4242},
            }

        async def __call__(self, cid):
            return self.info

    app.state.podman.inspect_container = _Insp()
    app.state.container_registry.states = {
        WS: types.SimpleNamespace(container_id="abc123")
    }
    led = pl.ProcessLedger(app)
    await led._refresh_roots()
    assert led._roots[WS] == 4242
    # container stops -> inspect says not running -> root dropped
    app.state.podman.inspect_container.info = {
        "State": {"Running": False, "Pid": 0}
    }
    await led._refresh_roots()
    assert WS not in led._roots


@pytest.mark.asyncio
async def test_prune_stale_roots_ttl(tmp_path):
    app, _ = _app(tmp_path)
    led = pl.ProcessLedger(app)
    led.set_root(WS, 100)
    led._roots_at[WS] = time.time() - 3600
    led.prune_stale_roots()
    assert WS not in led._roots


@pytest.mark.asyncio
async def test_retention_prune_called_from_loop(tmp_path):
    app, rec = _app(tmp_path)

    class _PruneRecorder(_LaunchRecorder):
        def __init__(self):
            super().__init__()
            self.prunes = []

        async def prune(self, *, keep_rows, keep_seconds):
            self.prunes.append((keep_rows, keep_seconds))
            return 0

    pruner = _PruneRecorder()
    app.state.model.process_launch = pruner
    led = pl.ProcessLedger(app)
    await led.start()  # fallback backend
    for _ in range(40):
        if pruner.prunes:
            break
        await asyncio.sleep(0.05)
    assert pruner.prunes, "retention prune must run from the loop"
    assert pruner.prunes[0] == (100000, 1e9)
    await led.stop()


@pytest.mark.asyncio
async def test_run_loop_fallback_polls(tmp_path, monkeypatch):
    app, rec = _app(tmp_path)
    led = pl.ProcessLedger(app)
    polled = []

    async def _fake_poll():
        polled.append(1)

    monkeypatch.setattr(led, "_fallback_poll_once", _fake_poll)
    await led.start()
    for _ in range(40):
        if polled:
            break
        await asyncio.sleep(0.05)
    assert polled
    await led.stop()


@pytest.mark.asyncio
async def test_status_reflects_state(tmp_path):
    app, _ = _app(tmp_path)
    led = pl.ProcessLedger(app)
    led.set_root(WS, 1)
    led._anchors[2] = ("agent", WS)
    st = led.status()
    assert st["roots"] == 1
    assert st["anchors"] == 1


def test_watcher_path_per_backend(tmp_path):
    """watcher_path picks the backend's wheel-adjacent binary; an
    explicit process_ledger_watcher path overrides either backend
    (#2520). watcher="" clears the fixture's absent-path default so
    the wheel-adjacent resolution is exercised.
    """
    app, _ = _app(tmp_path, watcher="")
    led = pl.ProcessLedger(app)
    assert led.watcher_path.name == "procleddy"

    app, _ = _app(tmp_path, watcher="", backend="ebpf")
    led = pl.ProcessLedger(app)
    assert led.watcher_path.name == "procleddy-ebpf"

    app, _ = _app(
        tmp_path, backend="ebpf", watcher=str(tmp_path / "explicit")
    )
    led = pl.ProcessLedger(app)
    assert led.watcher_path == Path(str(tmp_path / "explicit"))


@pytest.mark.asyncio
async def test_start_with_ebpf_backend_labels_and_logs(tmp_path, caplog):
    """backend=ebpf spawns the same contract (fake watcher works) and is
    labeled 'ebpf' in state, status, and the startup log (#2520 spike —
    the real binary needs CAP_BPF, exercised separately)."""
    wpath = tmp_path / "fakewatcher"
    wpath.write_text(FAKE_WATCHER)
    wpath.chmod(0o755)
    app, rec = _app(tmp_path, watcher=str(wpath), backend="ebpf")
    led = pl.ProcessLedger(app)
    led.set_root(WS, 100)
    with caplog.at_level("INFO", logger="klangk.process_ledger"):
        await led.start()
    assert led.backend == "ebpf"
    assert "capture running — eBPF watcher pid" in caplog.text
    await led.stop()


def test_log_task_exception_logs_error(caplog):
    """_log_task_exception logs when a fire-and-forget task fails."""
    loop = asyncio.new_event_loop()

    async def _boom():
        raise RuntimeError("boom")

    task = loop.create_task(_boom())
    loop.run_until_complete(asyncio.sleep(0.01))
    with caplog.at_level("ERROR"):
        pl._log_task_exception(task)
    assert "boom" in caplog.text
    loop.close()


def test_log_task_exception_silent_on_cancel():
    """_log_task_exception is silent for cancelled tasks."""
    loop = asyncio.new_event_loop()

    async def _sleep():
        await asyncio.sleep(60)

    task = loop.create_task(_sleep())
    task.cancel()
    try:
        loop.run_until_complete(task)
    except asyncio.CancelledError:
        pass
    # must not raise
    pl._log_task_exception(task)
    loop.close()


@pytest.mark.asyncio
async def test_read_watcher_stdout_stops_on_drain(tmp_path):
    """Reader exits when stdout EOFs while the ledger is stopping."""
    app, _ = _app(tmp_path)
    led = pl.ProcessLedger(app)
    r = asyncio.StreamReader()
    r.feed_eof()

    class _P:
        stdout = r
        returncode = None
        stdin = None

        def send_signal(self, sig):  # pragma: no cover - stub
            self.returncode = -15

        def kill(self):  # pragma: no cover - stub
            self.returncode = -9

        async def wait(self):  # pragma: no cover - stub
            self.returncode = self.returncode or 0

    led._watcher_proc = _P()  # type: ignore[assignment]
    led._task = asyncio.create_task(asyncio.sleep(0.1))
    await asyncio.wait_for(led._read_watcher_stdout(), timeout=2)


@pytest.mark.asyncio
async def test_read_watcher_stdout_skips_bad_json(tmp_path):
    app, _ = _app(tmp_path)
    led = pl.ProcessLedger(app)

    r = asyncio.StreamReader()
    r.feed_data(b"not json\n")
    r.feed_data(
        json.dumps(
            {
                "type": "heartbeat",
                "interval_ms": 90.0,
            }
        ).encode()
        + b"\n"
    )
    r.feed_eof()

    class _P:
        stdout = r

    led._watcher_proc = _P()  # type: ignore[assignment]
    led._task = None
    await asyncio.wait_for(led._read_watcher_stdout(), timeout=2)
    assert led.effective_interval_ms == pytest.approx(90.0)


@pytest.mark.asyncio
async def test_handle_event_ignores_junk(tmp_path):
    app, _ = _app(tmp_path)
    led = pl.ProcessLedger(app)
    led.set_root(WS, 100)
    # no pid
    await led._handle_event({"type": "birth"})
    # non-ledger type
    await led._handle_event({"type": "snapshot_end", "ts": 1.0})
    # pid not in any workspace ancestry
    await led._handle_event(
        {"type": "birth", "pid": 999, "ppid": 1, "ancestry": [999]}
    )
    # exec event for a workspace member
    led._anchors[101] = ("agent", WS)
    await led._handle_event(
        {
            "type": "exec",
            "pid": 102,
            "ppid": 101,
            "uid": 0,
            "comm": "sudo",
            "argv": "/usr/bin/sudo bash",
            "ancestry": [102, 101, 100],
            "ts_realtime": 2.0,
        }
    )
    # reparent: no row
    await led._handle_event(
        {"type": "reparent", "pid": 102, "ancestry": [102, 100]}
    )


@pytest.mark.asyncio
async def test_model_prune_row_cap(app_state, db, user):  # noqa: ARG001
    ws = await app_state.state.model.workspaces.create_workspace(
        user["id"], "ledger-ws2"
    )
    model = app_state.state.model.process_launch
    for i in range(5):
        await model.record_launch(
            workspace_id=ws["id"],
            pid=i,
            ppid=None,
            uid=None,
            comm="c",
            argv="a",
            started_at=100.0 + i,
            principal="agent",
            attribution_method="anchor",
        )
    # row cap 3 keeps newest 3
    n = await model.prune(keep_rows=3, keep_seconds=0)
    assert n == 2
    assert await model.count_launches(ws["id"]) == 3
    rows = await model.list_launches(ws["id"])
    assert [r["started_at"] for r in rows] == [104.0, 103.0, 102.0]
    # invalid kind coerces to birth at insert
    row = await model.record_launch(
        workspace_id=ws["id"],
        pid=99,
        ppid=None,
        uid=None,
        comm="c",
        argv="a",
        started_at=1.0,
        principal="unknown",
        attribution_method="fallback",
        event_kind="bogus",
    )
    assert row["event_kind"] == "birth"


def test_model_reconfigure(app_state):
    app_state.state.model.process_launch.reconfigure(app_state)


@pytest.mark.asyncio
async def test_api_handlers_missing_workspace(tmp_path):
    """Direct handler call: the defensive 404 branch (workspace gone
    between ACL pass and lookup)."""
    from fastapi import HTTPException

    from klangk.api.workspaces import (
        workspace_process_ledger_status,
        workspace_processes,
    )

    app, _ = _app(tmp_path)
    app.state.model.workspaces = types.SimpleNamespace()
    app.state.model.workspaces.get_workspace = _returns_none()
    with pytest.raises(HTTPException) as ei:
        await workspace_processes(
            "gone-ws", user={"id": "u"}, app=app, limit=10, offset=0
        )
    assert ei.value.status_code == 404
    with pytest.raises(HTTPException) as ei:
        await workspace_process_ledger_status(
            "gone-ws", user={"id": "u"}, app=app
        )
    assert ei.value.status_code == 404


class _returns_none:
    async def __call__(self, ws_id):  # noqa: ARG002
        return None
