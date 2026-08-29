"""Unit tests for :mod:`klangk.inactivity` — the dormant-account sweeper
(#2588). DB semantics (cutoff math, exemptions) live in
test_model_users.py; these cover the loop contract: startup sweep,
interval, failure tolerance, disable-at-0, live settings read.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

from klangk import inactivity


def _app(*, days=35, disable_inactive=None):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(inactivity_disable_days=days)
    app.state.model = types.SimpleNamespace(
        users=types.SimpleNamespace(
            disable_inactive_users=(
                disable_inactive
                if disable_inactive is not None
                else AsyncMock(return_value=[])
            )
        )
    )
    app.state.sockets = types.SimpleNamespace(disconnect_user=AsyncMock())
    return app


class TestInactivitySweeper:
    async def test_sweeps_at_startup(self):
        """The first sweep fires immediately (next_sweep starts at 0) —
        a prior run may have left accounts past the window."""
        app = _app(disable_inactive=AsyncMock(return_value=[]))
        sw = inactivity.InactivitySweeper(app)
        sw.start()
        for _ in range(100):
            if await_count(app.state.model.users.disable_inactive_users) >= 1:
                break
            await asyncio.sleep(0.01)
        assert await_count(app.state.model.users.disable_inactive_users) == 1
        await sw.stop()

    async def test_sweeps_on_interval_when_idle(self, monkeypatch):
        monkeypatch.setattr(inactivity, "SWEEP_INTERVAL", 0.01)
        app = _app(disable_inactive=AsyncMock(return_value=[]))
        sw = inactivity.InactivitySweeper(app)
        sw.start()
        for _ in range(100):
            if await_count(app.state.model.users.disable_inactive_users) >= 2:
                break
            await asyncio.sleep(0.01)
        assert await_count(app.state.model.users.disable_inactive_users) >= 2
        await sw.stop()

    async def test_sweep_failure_does_not_kill_loop(self, monkeypatch):
        """A failing sweep logs + retries an interval later."""
        monkeypatch.setattr(inactivity, "SWEEP_INTERVAL", 0.01)
        app = _app(
            disable_inactive=AsyncMock(side_effect=RuntimeError("db locked"))
        )
        sw = inactivity.InactivitySweeper(app)
        sw.start()
        for _ in range(100):
            if await_count(app.state.model.users.disable_inactive_users) >= 2:
                break
            await asyncio.sleep(0.01)
        assert await_count(app.state.model.users.disable_inactive_users) >= 2
        assert not sw._task.done()
        await sw.stop()

    async def test_stop_during_sweep_cancels_cleanly(self, monkeypatch):
        monkeypatch.setattr(inactivity, "SWEEP_INTERVAL", 0.01)
        started = asyncio.Event()
        release = asyncio.Event()

        async def hang(days):  # noqa: ARG001
            started.set()
            await release.wait()
            return []

        app = _app(disable_inactive=hang)
        sw = inactivity.InactivitySweeper(app)
        sw.start()
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()  # a sweep is in flight
        await sw.stop()  # cancels it; must not raise
        release.set()

    async def test_start_stop_lifecycle(self):
        sw = inactivity.InactivitySweeper(_app())
        sw.start()
        assert sw._task is not None
        await asyncio.sleep(0.01)
        assert not sw._task.done()
        await sw.stop()
        assert sw._task is None

    async def test_start_is_idempotent(self):
        sw = inactivity.InactivitySweeper(_app())
        sw.start()
        first = sw._task
        sw.start()
        assert sw._task is first
        await sw.stop()

    async def test_stop_when_not_started_is_noop(self):
        sw = inactivity.InactivitySweeper(_app())
        await sw.stop()
        assert sw._task is None

    def test_reconfigure_swaps_app(self):
        sw = inactivity.InactivitySweeper(_app())
        app2 = _app()
        sw.reconfigure(app2)
        assert sw.app is app2

    async def test_disabled_setting_skips_sweep(self):
        """inactivity_disable_days=0 (the sweep off) never reaches the
        model — read live each pass, so a SIGHUP flip applies next sweep."""
        app = _app(days=0)
        sw = inactivity.InactivitySweeper(app)
        await sw.sweep()
        assert await_count(app.state.model.users.disable_inactive_users) == 0

    async def test_days_passed_live_from_settings(self):
        """The sweep reads settings each pass (SIGHUP reload-safe)."""
        app = _app(days=7)
        mock = app.state.model.users.disable_inactive_users
        await inactivity.InactivitySweeper(app).sweep()
        mock.assert_awaited_once_with(7)
        app.state.settings.inactivity_disable_days = 3
        await inactivity.InactivitySweeper(app).sweep()
        assert mock.await_args_list[-1] == ((3,), {})

    async def test_disabled_users_are_logged_and_kicked(self, caplog):
        """A non-empty result logs the disabled emails and closes their
        live connections (#2588 review)."""
        app = _app(
            disable_inactive=AsyncMock(
                return_value=[{"id": "u1", "email": "gone@example.com"}]
            )
        )
        with caplog.at_level("INFO", logger="klangk.inactivity"):
            await inactivity.InactivitySweeper(app).sweep()
        assert "gone@example.com" in caplog.text
        app.state.sockets.disconnect_user.assert_awaited_once_with(
            "u1", code=4001, reason="Account disabled"
        )


def await_count(mock) -> int:
    return mock.await_count if hasattr(mock, "await_count") else 0
