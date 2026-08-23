"""Tests for scheduled host shutdown/restart (#2661).

Covers the model (persistence + validation), the ``resolve_fire_at``
payload parser, the :class:`HostScheduler` loop (broadcast cadence, due
firing, teardown, dry-run vs configured command), the
``broadcast_to_all`` fan-out, and the admin API endpoints.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from klangk.host_schedule import (
    HostScheduler,
    resolve_fire_at,
    _parse_fire_at,
)
from klangk.model.migrations import MIGRATIONS


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@pytest.fixture
async def sched_app(app_state):
    """app_state + schema + a stub sockets registry (broadcast recording)."""
    await app_state.state.model.init_db()
    sent: list[dict] = []

    class Sockets:
        def broadcast_to_all(self, message):
            sent.append(message)

    app_state.state.sockets = Sockets()
    return app_state, sent


class TestHostSchedulesModel:
    async def test_create_and_pending_orders_soonest_first(self, sched_app):
        app, _ = sched_app
        model = app.state.model.host_schedules
        later = await model.create_schedule(
            "shutdown",
            datetime.now(timezone.utc) + timedelta(hours=2),
            created_by="u1",
        )
        sooner = await model.create_schedule(
            "restart",
            datetime.now(timezone.utc) + timedelta(minutes=5),
            created_by="u1",
        )
        pending = await model.pending_schedules()
        assert [s["id"] for s in pending] == [sooner["id"], later["id"]]
        assert pending[0]["action"] == "restart"

    async def test_create_rejects_bad_action(self, sched_app):
        app, _ = sched_app
        with pytest.raises(ValueError, match="action must be one of"):
            await app.state.model.host_schedules.create_schedule(
                "explode",
                datetime.now(timezone.utc) + timedelta(hours=1),
                created_by="u1",
            )

    async def test_create_rejects_naive_datetime(self, sched_app):
        app, _ = sched_app
        with pytest.raises(ValueError, match="timezone-aware"):
            await app.state.model.host_schedules.create_schedule(
                "shutdown",
                datetime(2030, 1, 1, 12, 0, 0),
                created_by="u1",
            )

    async def test_cancel(self, sched_app):
        app, _ = sched_app
        model = app.state.model.host_schedules
        schedule = await model.create_schedule(
            "shutdown",
            datetime.now(timezone.utc) + timedelta(hours=1),
            created_by="u1",
        )
        assert await model.cancel_schedule(schedule["id"]) is True
        assert await model.pending_schedules() == []
        # Already gone.
        assert await model.cancel_schedule(schedule["id"]) is False

    async def test_delete_schedule(self, sched_app):
        app, _ = sched_app
        model = app.state.model.host_schedules
        schedule = await model.create_schedule(
            "restart",
            datetime.now(timezone.utc) + timedelta(hours=1),
            created_by="u1",
        )
        await model.delete_schedule(schedule["id"])
        assert await model.pending_schedules() == []


class TestResolveFireAt:
    def test_absolute(self):
        when = "2030-01-01T12:00:00+00:00"
        assert resolve_fire_at({"at": when}) == datetime(
            2030, 1, 1, 12, tzinfo=timezone.utc
        )

    def test_absolute_naive_treated_as_utc(self):
        parsed = resolve_fire_at({"at": "2030-01-01T12:00:00"})
        assert parsed.tzinfo is not None

    def test_relative(self):
        before = datetime.now(timezone.utc)
        parsed = resolve_fire_at({"in_seconds": 90})
        delta = parsed - before
        assert timedelta(seconds=89) < delta < timedelta(seconds=91)

    def test_invalid_at(self):
        with pytest.raises(ValueError, match="invalid 'at'"):
            resolve_fire_at({"at": "not-a-date"})

    def test_invalid_in_seconds(self):
        with pytest.raises(ValueError, match="number"):
            resolve_fire_at({"in_seconds": "soon"})

    def test_nonpositive_in_seconds(self):
        with pytest.raises(ValueError, match="positive"):
            resolve_fire_at({"in_seconds": 0})

    def test_neither(self):
        with pytest.raises(ValueError, match="'at' or 'in_seconds'"):
            resolve_fire_at({})

    def test_parse_fire_at_naive(self):
        assert _parse_fire_at("2030-01-01T12:00:00").tzinfo is not None


def test_migration_registered():
    assert any(m.name == "0006_host_schedules" for m in MIGRATIONS)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def _scheduler_app(sched_app, *, shutdown_cmd="", restart_cmd=""):
    """A scheduler wired to stub sockets/registry/settings records."""
    app, sent = sched_app
    registry = SimpleNamespace(
        draining=False,
        drain_all_containers=AsyncMock(return_value=3),
    )
    inflight = SimpleNamespace(
        count=0,
        wait_for_idle=AsyncMock(return_value=True),
    )
    scheduler = HostScheduler(app)
    # Point the scheduler's collaborators at the stubs.
    app.state.container_registry = registry
    app.state.inflight_requests = inflight
    app.state.settings = SimpleNamespace(
        quiesce_timeout=0.0,
        host_shutdown_command=shutdown_cmd,
        host_restart_command=restart_cmd,
    )
    return scheduler, sent, registry


class TestHostScheduler:
    async def test_notify_pending_broadcasts_snapshot(self, sched_app):
        app, sent = sched_app
        scheduler, sent, _ = _scheduler_app(sched_app)
        await app.state.model.host_schedules.create_schedule(
            "shutdown",
            datetime.now(timezone.utc) + timedelta(hours=1),
            created_by="u1",
        )
        await scheduler.notify_pending()
        assert sent and sent[-1]["type"] == "host_schedule"
        assert len(sent[-1]["schedules"]) == 1
        assert sent[-1]["schedules"][0]["action"] == "shutdown"

    async def test_send_snapshot_to_dead_socket_is_swallowed(self, sched_app):
        scheduler, _, _ = _scheduler_app(sched_app)

        class Dead:
            def send_json(self, message):
                raise RuntimeError("gone")

        await scheduler.send_snapshot_to(Dead())  # must not raise

    async def test_tick_fires_due_schedule_dry_run(self, sched_app):
        app, sent = sched_app
        scheduler, sent, registry = _scheduler_app(sched_app)
        # Due immediately.
        await app.state.model.host_schedules.create_schedule(
            "shutdown",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            created_by="u1",
        )
        await scheduler._tick()
        # Row deleted, fired event broadcast, workspaces drained, dry run
        # (no command configured -> no OS call, no crash).
        assert await app.state.model.host_schedules.pending_schedules() == []
        types = [m["type"] for m in sent]
        assert "host_schedule_fired" in types
        assert registry.drain_all_containers.await_count == 1
        assert registry.draining is True

    async def test_tick_fires_with_command(self, sched_app):
        app, sent = sched_app
        scheduler, sent, registry = _scheduler_app(
            sched_app, restart_cmd="/bin/true"
        )
        await app.state.model.host_schedules.create_schedule(
            "restart",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            created_by="u1",
        )
        proc = SimpleNamespace(returncode=0)
        with patch(
            "klangk.host_schedule.asyncio.create_subprocess_shell",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await scheduler._tick()
        mock_exec.assert_awaited_once_with(
            "/bin/true",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        fired = [m for m in sent if m["type"] == "host_schedule_fired"]
        assert fired and fired[0]["action"] == "restart"

    async def test_tick_teardown_failure_still_runs_command(self, sched_app):
        app, sent = sched_app
        scheduler, sent, registry = _scheduler_app(
            sched_app, shutdown_cmd="/bin/true"
        )
        registry.drain_all_containers = AsyncMock(
            side_effect=RuntimeError("podman down")
        )
        await app.state.model.host_schedules.create_schedule(
            "shutdown",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            created_by="u1",
        )
        proc = SimpleNamespace(returncode=0)
        with patch(
            "klangk.host_schedule.asyncio.create_subprocess_shell",
            new=AsyncMock(return_value=proc),
        ) as mock_exec:
            await scheduler._tick()  # must not raise
        mock_exec.assert_awaited_once()

    async def test_tick_future_schedule_only_broadcasts(self, sched_app):
        app, sent = sched_app
        scheduler, sent, registry = _scheduler_app(sched_app)
        await app.state.model.host_schedules.create_schedule(
            "shutdown",
            datetime.now(timezone.utc) + timedelta(hours=1),
            created_by="u1",
        )
        await scheduler._tick()
        assert registry.drain_all_containers.await_count == 0
        assert "host_schedule_fired" not in [m["type"] for m in sent]
        assert sent and sent[-1]["type"] == "host_schedule"

    async def test_broadcast_periodicity_resets(self, sched_app):
        """Same pending set re-broadcasts only after the cadence window."""
        app, sent = sched_app
        scheduler, sent, _ = _scheduler_app(sched_app)
        await app.state.model.host_schedules.create_schedule(
            "shutdown",
            datetime.now(timezone.utc) + timedelta(hours=1),
            created_by="u1",
        )
        await scheduler._tick()
        first = len(sent)
        await scheduler._tick()  # immediately again: set unchanged, cadence
        # not elapsed -> no second broadcast.
        assert len(sent) == first
        # Pretend the cadence window elapsed.
        scheduler._last_broadcast = datetime.now(timezone.utc) - timedelta(
            seconds=31
        )
        await scheduler._tick()
        assert len(sent) == first + 1

    async def test_start_stop_idempotent(self, sched_app):
        scheduler, _, _ = _scheduler_app(sched_app)
        scheduler.start()
        task = scheduler._task
        scheduler.start()  # no second task
        assert scheduler._task is task
        await scheduler.stop()
        assert scheduler._task is None
        await scheduler.stop()  # no-op


class TestLoop:
    async def test_run_survives_a_bad_tick(self, sched_app):
        """One failing tick is logged and skipped; the loop keeps going."""
        scheduler, sent, _ = _scheduler_app(sched_app)
        calls = {"n": 0}

        async def flaky_tick():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db hiccup")
            # Second tick: stop the loop.
            asyncio.current_task().cancel()

        scheduler._tick = flaky_tick
        with patch("klangk.host_schedule._POLL_INTERVAL_SECONDS", 0.01):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(scheduler._run(), timeout=5)
        assert calls["n"] >= 2


class TestBroadcastToAll:
    """#2661: the generic fan-out used by the scheduler."""

    class _FakeSock:
        def __init__(self):
            self.send_json = MagicMock()

    def _ws_state(self):
        from klangk.wshandler.session import WebSocketState

        return WebSocketState(SimpleNamespace(state=SimpleNamespace()))

    def test_sends_to_authenticated_and_skips_anonymous(self):

        ws_state = self._ws_state()
        ok = self._FakeSock()
        anon = self._FakeSock()
        ws_state.connections = {
            ok: SimpleNamespace(user={"id": "u1"}),
            anon: SimpleNamespace(user={}),
        }
        ws_state.broadcast_to_all({"type": "x"})
        ok.send_json.assert_called_once_with({"type": "x"})
        anon.send_json.assert_not_called()

    def test_dead_socket_is_dropped(self):
        from klangk.wshandler.safe_websocket import WS_ERRORS

        ws_state = self._ws_state()
        dead = self._FakeSock()
        dead.send_json = MagicMock(side_effect=WS_ERRORS[0]("gone"))
        live = self._FakeSock()
        ws_state.connections = {
            dead: SimpleNamespace(user={"id": "u1"}),
            live: SimpleNamespace(user={"id": "u2"}),
        }
        ws_state.broadcast_to_all({"type": "x"})
        assert dead not in ws_state.connections
        live.send_json.assert_called_once()


class TestSchedulerCoverageDetails:
    async def test_reconfigure(self, sched_app):
        scheduler, _, _ = _scheduler_app(sched_app)
        other = SimpleNamespace()
        scheduler.reconfigure(other)
        assert scheduler.app is other

    async def test_run_outer_cancel_logs_stop(self, sched_app):
        """Cancelling the loop mid-tick surfaces CancelledError (inner
        re-raise) and stops cleanly."""
        scheduler, _, _ = _scheduler_app(sched_app)
        held = asyncio.Event()

        async def stuck_tick():
            await held.wait()

        scheduler._tick = stuck_tick
        task = asyncio.get_event_loop().create_task(scheduler._run())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_fire_quiesce_stragglers_logged(self, sched_app):
        """In-flight requests past the quiesce window proceed with a
        warning, and the schedule still fires."""
        app, sent = sched_app
        scheduler, sent, registry = _scheduler_app(sched_app)
        app.state.inflight_requests = SimpleNamespace(
            count=2,
            wait_for_idle=AsyncMock(return_value=False),
        )
        await app.state.model.host_schedules.create_schedule(
            "shutdown",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            created_by="u1",
        )
        await scheduler._tick()
        registry.drain_all_containers.assert_awaited_once()
