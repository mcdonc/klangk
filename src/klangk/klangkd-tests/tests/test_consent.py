"""Unit tests for :mod:`klangk.consent` (the monitor) (#2242). The monitor
dispatches on the workspace's egress_mode: static -> record_static_denial
(denied, no human, immediate); interactive -> create_request (pending) +
timeout.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock

from klangk import consent

FULL_WS = "aaaa1111bbbb-cccc-dddd-eeee-ffffffffffff"


def _app(
    *,
    rate_limit: int = 50,
    timeout: float = 30.0,
    count_pending: int = 0,
    request=None,
    expire: bool = True,
    static_denial=None,
    egress_mode: str = "static",
    workspace_exists: bool = True,
    has_decider: bool = True,
):
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(
        egress_consent_rate_limit=rate_limit,
        egress_consent_timeout=timeout,
    )
    egress_consent = AsyncMock()
    egress_consent.count_pending = AsyncMock(return_value=count_pending)
    egress_consent.create_request = AsyncMock(return_value=request)
    egress_consent.expire_pending = AsyncMock(return_value=expire)
    egress_consent.record_static_denial = AsyncMock(return_value=static_denial)
    # The _run loop sweeps retention at startup and on a wall-clock deadline
    # (#2303); default to a no-op so event-path tests aren't coupled to it.
    egress_consent.prune = AsyncMock(return_value=0)
    workspaces = AsyncMock()
    workspaces.get_workspace = AsyncMock(
        return_value={"egress_mode": egress_mode} if workspace_exists else None
    )
    app.state.model = types.SimpleNamespace(
        egress_consent=egress_consent, workspaces=workspaces
    )
    app.state.consent_deciders = types.SimpleNamespace(
        has_decider=lambda workspace_id: has_decider
    )
    return app


def _denial():
    return {
        "id": "sid",
        "workspace_id": FULL_WS,
        "dest_host": "1.2.3.4",
        "dest_port": 80,
        "decision": "denied",
        "decided_by": None,
    }


class TestEgressConsentMonitor:
    async def test_static_records_denial_immediately(self):
        # static: record_static_denial (denied, no human); no pending, no
        # timeout, no create_request.
        app = _app(egress_mode="static", static_denial=_denial())
        mon = consent.EgressConsentMonitor(app)
        await mon._handle_event(FULL_WS, "1.2.3.4", 80)
        app.state.model.egress_consent.record_static_denial.assert_awaited_once_with(
            FULL_WS, "1.2.3.4", 80
        )
        app.state.model.egress_consent.create_request.assert_not_called()
        assert mon._timeouts == set()

    async def test_static_dedup_skips_notify(self):
        # record_static_denial returns None (already recorded) -> no notify.
        app = _app(egress_mode="static", static_denial=None)
        mon = consent.EgressConsentMonitor(app)
        mon._notify = AsyncMock()
        await mon._handle_event(FULL_WS, "1.2.3.4", 80)
        app.state.model.egress_consent.record_static_denial.assert_awaited_once()
        mon._notify.assert_not_called()

    async def test_unknown_workspace_defaults_to_static(self):
        app = _app(workspace_exists=True, egress_mode="static")
        mon = consent.EgressConsentMonitor(app)
        await mon._handle_event(FULL_WS, "1.2.3.4", 80)
        app.state.model.egress_consent.record_static_denial.assert_awaited_once()

    async def test_interactive_without_decider_falls_to_static(self):
        # #2308: interactive mode is runtime state -- with no live decider
        # registered, a blocked destination is recorded as a static denial,
        # not queued as pending (no hanging connection).
        app = _app(
            egress_mode="interactive",
            has_decider=False,
            static_denial=_denial(),
        )
        mon = consent.EgressConsentMonitor(app)
        await mon._handle_event(FULL_WS, "1.2.3.4", 80)
        app.state.model.egress_consent.record_static_denial.assert_awaited_once_with(
            FULL_WS, "1.2.3.4", 80
        )
        app.state.model.egress_consent.create_request.assert_not_called()

    async def test_interactive_creates_pending_and_schedules_timeout(self):
        req = {
            "id": "rid",
            "workspace_id": FULL_WS,
            "dest_host": "1.2.3.4",
            "dest_port": 80,
        }
        # Short timeout: if stop() didn't cancel it, expire_pending would
        # fire before the post-stop sleep below.
        app = _app(
            egress_mode="interactive",
            count_pending=0,
            request=req,
            timeout=0.02,
        )
        mon = consent.EgressConsentMonitor(app)
        await mon._handle_event(FULL_WS, "1.2.3.4", 80)
        app.state.model.egress_consent.create_request.assert_awaited_once_with(
            FULL_WS, "1.2.3.4", 80
        )
        app.state.model.egress_consent.record_static_denial.assert_not_called()
        assert len(mon._timeouts) == 1
        await mon.stop()
        await asyncio.sleep(0.05)
        app.state.model.egress_consent.expire_pending.assert_not_called()

    async def test_interactive_rate_limited(self):
        app = _app(egress_mode="interactive", count_pending=50, rate_limit=50)
        mon = consent.EgressConsentMonitor(app)
        await mon._handle_event(FULL_WS, "1.2.3.4", 80)
        app.state.model.egress_consent.count_pending.assert_awaited_once_with(
            FULL_WS
        )
        app.state.model.egress_consent.create_request.assert_not_called()

    async def test_interactive_dedup_skips(self):
        app = _app(egress_mode="interactive", count_pending=0, request=None)
        mon = consent.EgressConsentMonitor(app)
        await mon._handle_event(FULL_WS, "1.2.3.4", 80)
        app.state.model.egress_consent.create_request.assert_awaited_once()
        assert mon._timeouts == set()

    async def test_timeout_auto_expires(self):
        app = _app(timeout=0.01)
        mon = consent.EgressConsentMonitor(app)
        await mon._timeout("rid")
        app.state.model.egress_consent.expire_pending.assert_awaited_once_with(
            "rid"
        )

    async def test_timeout_is_cancellable(self):
        app = _app(timeout=10.0)
        mon = consent.EgressConsentMonitor(app)
        task = asyncio.create_task(mon._timeout("rid"))
        await asyncio.sleep(0)
        task.cancel()
        await task
        app.state.model.egress_consent.expire_pending.assert_not_called()

    def test_properties_and_reconfigure(self):
        mon = consent.EgressConsentMonitor(_app(rate_limit=7, timeout=11.0))
        assert mon.rate_limit == 7
        assert mon.timeout == 11.0
        app2 = _app(rate_limit=9, timeout=13.0)
        mon.reconfigure(app2)
        assert mon.app is app2 and mon.rate_limit == 9 and mon.timeout == 13.0

    async def test_run_processes_submitted_events(self):
        req = {
            "id": "r",
            "workspace_id": FULL_WS,
            "dest_host": "h",
            "dest_port": 80,
        }
        app = _app(egress_mode="interactive", count_pending=0, request=req)
        mon = consent.EgressConsentMonitor(app)
        mon.start()
        mon.submit(FULL_WS, "1.2.3.4", 80)
        mon.submit(FULL_WS, "5.6.7.8", 443)
        for _ in range(40):
            if app.state.model.egress_consent.create_request.await_count >= 2:
                break
            await asyncio.sleep(0.01)
        assert app.state.model.egress_consent.create_request.await_count == 2
        await mon.stop()

    async def test_run_isolates_failing_events(self):
        app = _app(egress_mode="interactive", count_pending=0, request=None)
        app.state.model.egress_consent.create_request = AsyncMock(
            side_effect=[RuntimeError("boom"), None]
        )
        mon = consent.EgressConsentMonitor(app)
        mon.start()
        mon.submit(FULL_WS, "1.1.1.1", 1)  # raises
        mon.submit(FULL_WS, "2.2.2.2", 2)  # ok
        for _ in range(40):
            if app.state.model.egress_consent.create_request.await_count >= 2:
                break
            await asyncio.sleep(0.01)
        assert app.state.model.egress_consent.create_request.await_count == 2
        await mon.stop()

    async def test_run_re_raises_cancellation_from_handler(self):
        app = _app(egress_mode="interactive", count_pending=0)
        app.state.model.egress_consent.create_request = AsyncMock(
            side_effect=asyncio.CancelledError
        )
        mon = consent.EgressConsentMonitor(app)
        mon.start()
        mon.submit(FULL_WS, "1.1.1.1", 1)
        for _ in range(40):
            if mon._task.done():
                break
            await asyncio.sleep(0.01)
        assert mon._task.done()
        await mon.stop()

    async def test_start_stop_lifecycle(self):
        mon = consent.EgressConsentMonitor(_app())
        mon.start()
        assert mon._task is not None
        await asyncio.sleep(0.01)
        assert not mon._task.done()
        await mon.stop()
        assert mon._task is None

    async def test_start_is_idempotent(self):
        mon = consent.EgressConsentMonitor(_app())
        mon.start()
        first = mon._task
        mon.start()
        assert mon._task is first
        await mon.stop()

    async def test_stop_when_not_started_is_noop(self):
        mon = consent.EgressConsentMonitor(_app())
        await mon.stop()
        assert mon._task is None

    async def test_run_sweeps_retention_on_idle_interval(self, monkeypatch):
        """Queue idle for PRUNE_INTERVAL -> one prune pass (#2303)."""
        monkeypatch.setattr(consent, "PRUNE_INTERVAL", 0.01)
        app = _app()
        app.state.model.egress_consent.prune = AsyncMock(return_value=3)
        mon = consent.EgressConsentMonitor(app)
        mon.start()
        for _ in range(100):
            if app.state.model.egress_consent.prune.await_count >= 1:
                break
            await asyncio.sleep(0.01)
        assert app.state.model.egress_consent.prune.await_count >= 1
        await mon.stop()

    async def test_run_sweep_failure_does_not_kill_loop(self, monkeypatch):
        """A failing sweep logs + retries later; events still flow."""
        monkeypatch.setattr(consent, "PRUNE_INTERVAL", 0.01)
        app = _app(egress_mode="static", static_denial=_denial())
        app.state.model.egress_consent.prune = AsyncMock(
            side_effect=RuntimeError("db locked")
        )
        mon = consent.EgressConsentMonitor(app)
        mon.start()
        # let at least one failing sweep fire (the warning path)
        for _ in range(100):
            if app.state.model.egress_consent.prune.await_count >= 1:
                break
            await asyncio.sleep(0.01)
        assert app.state.model.egress_consent.prune.await_count >= 1
        mon.submit(FULL_WS, "3.3.3.3", 9)
        for _ in range(100):
            if (
                app.state.model.egress_consent.record_static_denial.await_count
                >= 1
            ):
                break
            await asyncio.sleep(0.01)
        assert (
            app.state.model.egress_consent.record_static_denial.await_count
            == 1
        )
        assert not mon._task.done()
        await mon.stop()

    async def test_run_sweep_then_event_ordering(self, monkeypatch):
        """After a sweep fires, a later event is still processed (the queue
        wait restarts after each sweep)."""
        monkeypatch.setattr(consent, "PRUNE_INTERVAL", 0.01)
        req = {
            "id": "r2",
            "workspace_id": FULL_WS,
            "dest_host": "h2",
            "dest_port": 80,
        }
        app = _app(egress_mode="interactive", count_pending=0, request=req)
        app.state.model.egress_consent.prune = AsyncMock(return_value=0)
        mon = consent.EgressConsentMonitor(app)
        mon.start()
        for _ in range(100):
            if app.state.model.egress_consent.prune.await_count >= 2:
                break
            await asyncio.sleep(0.01)
        mon.submit(FULL_WS, "4.4.4.4", 80)
        for _ in range(100):
            if app.state.model.egress_consent.create_request.await_count >= 1:
                break
            await asyncio.sleep(0.01)
        assert app.state.model.egress_consent.create_request.await_count == 1
        await mon.stop()

    async def test_stop_during_sweep_cancels_cleanly(self, monkeypatch):
        """Cancelling the monitor mid-sweep re-raises out of _prune and ends
        the loop via the outer CancelledError handler (no stray traceback)."""
        monkeypatch.setattr(consent, "PRUNE_INTERVAL", 0.01)
        app = _app()
        started = asyncio.Event()
        release = asyncio.Event()

        async def hang():
            started.set()
            await release.wait()
            return 0

        app.state.model.egress_consent.prune = hang
        mon = consent.EgressConsentMonitor(app)
        mon.start()
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()  # a sweep is in flight
        await mon.stop()  # cancels it; must not raise
        release.set()

    async def test_run_sweeps_at_startup(self):
        """The first sweep fires immediately (next_prune starts at 0) -- a
        prior run may have left the table past the window / over the cap."""
        app = _app()
        app.state.model.egress_consent.prune = AsyncMock(return_value=0)
        mon = consent.EgressConsentMonitor(app)
        mon.start()
        for _ in range(100):
            if app.state.model.egress_consent.prune.await_count >= 1:
                break
            await asyncio.sleep(0.01)
        assert app.state.model.egress_consent.prune.await_count == 1
        await mon.stop()

    async def test_run_busy_queue_does_not_postpone_sweep(self, monkeypatch):
        """Steady event traffic must not starve the sweep: the deadline is
        wall-clock, not a queue-idle timeout (the row cap exists for exactly
        the flooding workspace that keeps the queue busy)."""
        monkeypatch.setattr(consent, "PRUNE_INTERVAL", 0.05)
        req = {
            "id": "r3",
            "workspace_id": FULL_WS,
            "dest_host": "h3",
            "dest_port": 80,
        }
        app = _app(egress_mode="interactive", count_pending=0, request=req)
        app.state.model.egress_consent.prune = AsyncMock(return_value=0)
        mon = consent.EgressConsentMonitor(app)
        mon.start()
        # Feed events continuously -- one every 10ms, well under the 50ms
        # interval, so an idle-timeout-based timer would never fire.
        for _ in range(40):
            mon.submit(FULL_WS, "6.6.6.6", 80)
            await asyncio.sleep(0.01)
        assert app.state.model.egress_consent.prune.await_count >= 2
        assert app.state.model.egress_consent.create_request.await_count > 0
        await mon.stop()
