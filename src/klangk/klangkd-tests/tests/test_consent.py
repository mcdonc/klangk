"""Unit tests for :mod:`klangk.consent.egress` — the retention sweeper
(#2303). Event intake lives in the coordinator (see
test_consent_coordinator.py); the sweeper only prunes.
"""

from __future__ import annotations

import asyncio
import types
import pytest
from unittest.mock import AsyncMock

from klangk import consent

FULL_WS = "aaaa1111bbbb-cccc-dddd-eeee-ffffffffffff"


def _app(
    *, prune: AsyncMock | None = None, events_prune: AsyncMock | None = None
):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    egress_consent = types.SimpleNamespace(
        prune=prune or AsyncMock(return_value=0)
    )
    container_events = types.SimpleNamespace(
        prune=events_prune or AsyncMock(return_value=0)
    )
    app.state.model = types.SimpleNamespace(
        egress_consent=egress_consent,
        container_events=container_events,
    )
    return app


class TestEgressConsentSweeper:
    async def test_sweeps_at_startup(self):
        """The first sweep fires immediately (next_prune starts at 0) -- a
        prior run may have left the table past the window / over the cap."""
        app = _app(prune=AsyncMock(return_value=0))
        sw = consent.EgressConsentSweeper(app)
        sw.start()
        for _ in range(100):
            if app.state.model.egress_consent.prune.await_count >= 1:
                break
            await asyncio.sleep(0.01)
        assert app.state.model.egress_consent.prune.await_count == 1
        await sw.stop()

    async def test_sweeps_on_interval_when_idle(self, monkeypatch):
        """Idle for PRUNE_INTERVAL -> prune fires again (#2303)."""
        monkeypatch.setattr(consent.egress, "PRUNE_INTERVAL", 0.01)
        app = _app(prune=AsyncMock(return_value=3))
        sw = consent.EgressConsentSweeper(app)
        sw.start()
        for _ in range(100):
            if app.state.model.egress_consent.prune.await_count >= 2:
                break
            await asyncio.sleep(0.01)
        assert app.state.model.egress_consent.prune.await_count >= 2
        await sw.stop()

    async def test_sweep_failure_does_not_kill_loop(self, monkeypatch):
        """A failing sweep logs + retries an interval later."""
        monkeypatch.setattr(consent.egress, "PRUNE_INTERVAL", 0.01)
        app = _app(prune=AsyncMock(side_effect=RuntimeError("db locked")))
        sw = consent.EgressConsentSweeper(app)
        sw.start()
        for _ in range(100):
            if app.state.model.egress_consent.prune.await_count >= 2:
                break
            await asyncio.sleep(0.01)
        assert app.state.model.egress_consent.prune.await_count >= 2
        assert not sw._task.done()
        await sw.stop()

    async def test_stop_during_sweep_cancels_cleanly(self, monkeypatch):
        """Cancelling mid-sweep re-raises out of _prune and ends the loop
        via the outer CancelledError handler (no stray traceback)."""
        monkeypatch.setattr(consent.egress, "PRUNE_INTERVAL", 0.01)
        app = _app()
        started = asyncio.Event()
        release = asyncio.Event()

        async def hang():
            started.set()
            await release.wait()
            return 0

        app.state.model.egress_consent.prune = hang
        sw = consent.EgressConsentSweeper(app)
        sw.start()
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()  # a sweep is in flight
        await sw.stop()  # cancels it; must not raise
        release.set()

    async def test_start_stop_lifecycle(self):
        sw = consent.EgressConsentSweeper(_app())
        sw.start()
        assert sw._task is not None
        await asyncio.sleep(0.01)
        assert not sw._task.done()
        await sw.stop()
        assert sw._task is None

    async def test_start_is_idempotent(self):
        sw = consent.EgressConsentSweeper(_app())
        sw.start()
        first = sw._task
        sw.start()
        assert sw._task is first
        await sw.stop()

    async def test_stop_when_not_started_is_noop(self):
        sw = consent.EgressConsentSweeper(_app())
        await sw.stop()
        assert sw._task is None

    def test_reconfigure_swaps_app(self):
        sw = consent.EgressConsentSweeper(_app())
        app2 = _app()
        sw.reconfigure(app2)
        assert sw.app is app2


class TestContainerEventsSweep2924:
    """#2924: the retention sweep also prunes container_events, each
    target independent of the other's failures."""

    async def test_sweep_prunes_both_tables(self):
        """One sweep prunes egress_consent AND container_events."""
        app = _app()
        sw = consent.EgressConsentSweeper(app)
        await sw.sweep()
        assert app.state.model.egress_consent.prune.await_count == 1
        assert app.state.model.container_events.prune.await_count == 1

    async def test_prune_counts_logged(self, caplog):
        """A non-zero prune count is surfaced at info (operator can see the
        table was trimmed)."""
        import logging

        app = _app(
            prune=AsyncMock(return_value=7),
            events_prune=AsyncMock(return_value=9),
        )
        sw = consent.EgressConsentSweeper(app)
        with caplog.at_level(logging.INFO, logger="klangk.consent.egress"):
            await sw.sweep()
        assert "pruned 7 row(s)" in caplog.text
        assert "pruned 9 row(s)" in caplog.text

    async def test_container_events_failure_skipped_not_fatal(self):
        """A failing container_events prune is logged and skipped, but the
        egress_consent prune in the same sweep still ran."""
        app = _app(
            events_prune=AsyncMock(side_effect=RuntimeError("db locked"))
        )
        sw = consent.EgressConsentSweeper(app)
        await sw.sweep()  # must not raise
        assert app.state.model.egress_consent.prune.await_count == 1
        assert app.state.model.container_events.prune.await_count == 1

    async def test_egress_failure_still_prunes_events(self):
        """Mirror image: egress_consent failing must not skip the
        container_events half of the sweep."""
        app = _app(prune=AsyncMock(side_effect=RuntimeError("boom")))
        sw = consent.EgressConsentSweeper(app)
        await sw.sweep()  # must not raise
        assert app.state.model.container_events.prune.await_count == 1


class TestEgressSweeperBranchGaps2834:
    """#2834 branch gate: a retention tick that wakes BEFORE the interval
    (scheduler jitter) re-sleeps without sweeping."""

    async def test_early_wake_resleeps_without_pruning(self, monkeypatch):
        import klangk.interval as iv

        app = _app()
        sw = consent.EgressConsentSweeper(app)
        real_sweep = sw.sweep
        swept = []

        async def counting_sweep():
            swept.append(1)
            await real_sweep()

        monkeypatch.setattr(sw, "sweep", counting_sweep)

        # Freeze the clock and no-op the sleep, both scoped to the
        # IntervalWorker module's namespace (a global asyncio patch would
        # leak into same-worker neighbors under xdist).
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
        # Swept once at startup; the frozen clock makes every later tick
        # an early wake (the not-yet arm), so no second prune ran.
        app.state.model.egress_consent.prune.assert_awaited_once()
