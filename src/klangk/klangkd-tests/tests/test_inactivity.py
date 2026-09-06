"""Unit tests for :mod:`klangk.inactivity` — the dormant-account sweeper
(#2588). DB semantics (cutoff math, exemptions) live in
test_model_users.py; these cover the loop contract: startup sweep,
interval, failure tolerance, disable-at-0, live settings read.
"""

from __future__ import annotations

import asyncio
import types
import pytest
from unittest.mock import AsyncMock, Mock

from klangk import inactivity


def _app(*, days=35, disable_inactive=None):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(inactivity_disable_days=days)
    # The sweep writes one user.disable audit row per disabled account
    # (#3251 review); the AsyncMock is the assertion surface for it.
    app.state.model = types.SimpleNamespace(
        users=types.SimpleNamespace(
            disable_inactive_users=(
                disable_inactive
                if disable_inactive is not None
                else AsyncMock(return_value=[])
            )
        ),
        audit_events=types.SimpleNamespace(record_best_effort=AsyncMock()),
    )
    app.state.sockets = types.SimpleNamespace(disconnect_user=AsyncMock())
    # #3250: the sweep notifies on a non-empty disable result; the
    # spy doubles as the assertion surface in the disable tests.
    app.state.notifier = Mock()
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

    async def test_disabled_users_write_audit_rows(self):
        """#3251 review: the sweep's disables leave the same
        ``user.disable`` trail the admin toggle writes — one row per
        account, no actor (system action), via=inactivity in the
        detail — so every events view (audit, merged) surfaces them."""
        app = _app(
            disable_inactive=AsyncMock(
                return_value=[
                    {"id": "u1", "email": "gone@example.com"},
                    {"id": "u2", "email": "away@example.com"},
                ]
            )
        )
        await inactivity.InactivitySweeper(app).sweep()
        audit = app.state.model.audit_events.record_best_effort
        assert audit.await_count == 2
        audit.assert_any_await(
            "user.disable",
            target_type="user",
            target_id="u1",
            detail={
                "via": "inactivity",
                "days": 35,
                "email": "gone@example.com",
            },
        )
        audit.assert_any_await(
            "user.disable",
            target_type="user",
            target_id="u2",
            detail={
                "via": "inactivity",
                "days": 35,
                "email": "away@example.com",
            },
        )

        # An empty sweep writes nothing.
        idle_app = _app()
        await inactivity.InactivitySweeper(idle_app).sweep()
        idle_app.state.model.audit_events.record_best_effort.assert_not_awaited()

    async def test_disabled_users_notify_sa_isso(self):
        """#3250 (SV-222419): a non-empty sweep result notifies the
        SA/ISSO stream once — the batch in one message, no actor
        (system action), via=inactivity in the detail."""
        app = _app(
            disable_inactive=AsyncMock(
                return_value=[
                    {"id": "u1", "email": "gone@example.com"},
                    {"id": "u2", "email": "away@example.com"},
                ]
            )
        )
        await inactivity.InactivitySweeper(app).sweep()
        app.state.notifier.notify_admins.assert_called_once_with(
            "user.disable",
            detail={
                "via": "inactivity",
                "days": 35,
                "users": ["gone@example.com", "away@example.com"],
            },
        )

    async def test_disabled_users_deciders_are_kicked(self):
        """#3162: the sweep also closes the disabled user's live
        consent-decider sockets — a decider holds egress-consent
        authority and must not outlive the disable. The decider is a
        REAL SafeWebSocket (fakes masked the reason-kwarg no-op class
        of bug, #3160 review)."""
        from klangk.consent.deciders import ConsentDeciderRegistry
        from klangk.wshandler.safe_websocket import SafeWebSocket

        app = _app(
            disable_inactive=AsyncMock(
                return_value=[{"id": "u1", "email": "gone@example.com"}]
            )
        )
        app.state.consent_deciders = ConsentDeciderRegistry(app)
        raw = AsyncMock()
        raw.close = AsyncMock()
        app.state.consent_deciders.register(
            "d1",
            "ws-1",
            "gone@example.com",
            SafeWebSocket(raw),
            jti="j1",
            user_id="u1",
        )
        await inactivity.InactivitySweeper(app).sweep()
        raw.close.assert_awaited_once_with(
            code=4001, reason="Account disabled"
        )
        assert app.state.consent_deciders._deciders == {}


def await_count(mock) -> int:
    return mock.await_count if hasattr(mock, "await_count") else 0


class TestInactivityBranchGaps2834:
    """#2834 branch gate: a loop tick that wakes BEFORE the sweep interval
    (scheduler jitter) re-sleeps without sweeping."""

    async def test_early_wake_resleeps_without_sweeping(self, monkeypatch):
        import klangk.interval as iv

        app = _app(disable_inactive=AsyncMock(return_value=[]))
        sw = inactivity.InactivitySweeper(app)
        real_sweep = sw.sweep
        swept = []

        async def counting_sweep():
            swept.append(1)
            await real_sweep()

        monkeypatch.setattr(sw, "sweep", counting_sweep)
        clock = types.SimpleNamespace(t=1000.0)
        monkeypatch.setattr(
            iv, "time", types.SimpleNamespace(monotonic=lambda: clock.t)
        )
        sleeps = {"n": 0}

        real_sleep = asyncio.sleep

        async def fake_sleep(_s):
            sleeps["n"] += 1
            if sleeps["n"] >= 3:
                sw._task.cancel()
            await real_sleep(0)  # yield: the awaiting test interleaves

        # Delegate everything to the real asyncio except sleep, scoped
        # to the IntervalWorker module's namespace only.
        stub = types.SimpleNamespace(
            **{
                n: getattr(asyncio, n)
                for n in dir(asyncio)
                if not n.startswith("__")
            }
        )
        stub.sleep = fake_sleep
        monkeypatch.setattr(iv, "asyncio", stub)
        sw.start()
        with pytest.raises(asyncio.CancelledError):
            await sw._task
        # The startup sweep ran once; the frozen clock makes every later
        # tick an early wake, so the users sweep saw one pass only.
        assert app.state.model.users.disable_inactive_users.await_count == 1
