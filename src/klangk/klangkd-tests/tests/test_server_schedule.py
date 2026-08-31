"""Tests for scheduled server stop/recycle (#2661).

Covers the model (persistence + validation), the ``resolve_fire_at``
payload parser, the :class:`ServerScheduler` loop (broadcast cadence,
due firing, handoff to the graceful stop/recycle paths), the
``broadcast_to_all`` fan-out, and the admin API endpoints.
"""

import asyncio
import signal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from klangk.server_schedule import (
    ServerScheduler,
    resolve_fire_at,
    parse_fire_at,
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


class TestServerSchedulesModel:
    async def test_create_and_pending_orders_soonest_first(self, sched_app):
        app, _ = sched_app
        model = app.state.model.server_schedules
        later = await model.create_schedule(
            "stop",
            datetime.now(timezone.utc) + timedelta(hours=2),
            created_by="u1",
        )
        sooner = await model.create_schedule(
            "recycle",
            datetime.now(timezone.utc) + timedelta(minutes=5),
            created_by="u1",
        )
        pending = await model.pending_schedules()
        assert [s["id"] for s in pending] == [sooner["id"], later["id"]]
        assert pending[0]["action"] == "recycle"

    async def test_create_rejects_bad_action(self, sched_app):
        app, _ = sched_app
        with pytest.raises(ValueError, match="action must be one of"):
            await app.state.model.server_schedules.create_schedule(
                "explode",
                datetime.now(timezone.utc) + timedelta(hours=1),
                created_by="u1",
            )

    async def test_create_rejects_old_action_names(self, sched_app):
        """#2661 scope change: shutdown/restart are gone, not aliases."""
        app, _ = sched_app
        for old in ("shutdown", "restart"):
            with pytest.raises(ValueError, match="action must be one of"):
                await app.state.model.server_schedules.create_schedule(
                    old,
                    datetime.now(timezone.utc) + timedelta(hours=1),
                    created_by="u1",
                )

    async def test_create_rejects_naive_datetime(self, sched_app):
        app, _ = sched_app
        with pytest.raises(ValueError, match="timezone-aware"):
            await app.state.model.server_schedules.create_schedule(
                "stop",
                datetime(2030, 1, 1, 12, 0, 0),
                created_by="u1",
            )

    async def test_cancel(self, sched_app):
        app, _ = sched_app
        model = app.state.model.server_schedules
        schedule = await model.create_schedule(
            "stop",
            datetime.now(timezone.utc) + timedelta(hours=1),
            created_by="u1",
        )
        assert await model.cancel_schedule(schedule["id"]) is True
        assert await model.pending_schedules() == []
        # Already gone.
        assert await model.cancel_schedule(schedule["id"]) is False

    async def test_delete_schedule(self, sched_app):
        app, _ = sched_app
        model = app.state.model.server_schedules
        schedule = await model.create_schedule(
            "recycle",
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

    def test_in_seconds_must_be_finite(self):
        for bad in (float("inf"), float("-inf"), float("nan"), "Infinity"):
            with pytest.raises(ValueError, match="finite"):
                resolve_fire_at({"in_seconds": bad})

    def test_in_seconds_bounded(self):
        # Beyond ~1000 years timedelta would OverflowError (a 500 from
        # the API); reject with a clean 422 instead.
        with pytest.raises(ValueError, match="at most"):
            resolve_fire_at({"in_seconds": 9e18})
        with pytest.raises(ValueError, match="at most"):
            resolve_fire_at({"in_seconds": 1e11})

    def test_neither(self):
        with pytest.raises(ValueError, match="'at' or 'in_seconds'"):
            resolve_fire_at({})

    def test_parse_fire_at_naive(self):
        assert parse_fire_at("2030-01-01T12:00:00").tzinfo is not None


def test_migrations_registered():
    # 0006 keeps its recorded name (frozen once shipped); the table
    # rename is 0007.
    names = {m.name for m in MIGRATIONS}
    assert "0006_host_schedules" in names
    assert "0007_server_schedules" in names


async def test_table_renamed_by_migration_0007(app_state):
    """A DB that ran 0006 (host_schedules) gets its table renamed to
    server_schedules by 0007; any intermediate server_schedules table
    (from an unreleased dev build) is dropped first."""
    app = app_state
    await app.state.model.init_db()
    async with app.state.db.transaction() as db:
        # Reset to a pre-0007 DB: 0006's host_schedules exists (as on
        # every DB that ran the original #2661 build), plus the leftover
        # server_schedules an intermediate unreleased commit created.
        await db.execute("DROP TABLE IF EXISTS server_schedules")
        await db.execute(
            "CREATE TABLE host_schedules (id TEXT PRIMARY KEY,"
            " action TEXT NOT NULL, fire_at TEXT NOT NULL,"
            " created_by TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE server_schedules (id TEXT PRIMARY KEY,"
            " action TEXT NOT NULL, fire_at TEXT NOT NULL,"
            " created_by TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
    from klangk.model.migrations.m0007_server_schedules import apply

    async with app.state.db.transaction() as db:
        await apply(db)
    names = {
        r["name"]
        for r in await app.state.db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "server_schedules" in names
    assert "host_schedules" not in names


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


def _scheduler_app(sched_app, *, shutting_down=False):
    """A scheduler wired to stub sockets/lifecycle records."""
    app, sent = sched_app
    lifecycle = SimpleNamespace(
        shutting_down=shutting_down,
        request_recycle=MagicMock(),
    )
    scheduler = ServerScheduler(app)
    app.state.lifecycle = lifecycle
    return scheduler, sent, lifecycle


class TestServerScheduler:
    async def test_notify_pending_broadcasts_snapshot(self, sched_app):
        app, sent = sched_app
        scheduler, sent, _ = _scheduler_app(sched_app)
        await app.state.model.server_schedules.create_schedule(
            "stop",
            datetime.now(timezone.utc) + timedelta(hours=1),
            created_by="u1",
        )
        await scheduler.notify_pending()
        assert sent and sent[-1]["type"] == "server_schedule"
        assert len(sent[-1]["schedules"]) == 1
        assert sent[-1]["schedules"][0]["action"] == "stop"

    async def test_send_snapshot_to_dead_socket_is_swallowed(self, sched_app):
        scheduler, _, _ = _scheduler_app(sched_app)

        class Dead:
            def send_json(self, message):
                raise RuntimeError("gone")

        await scheduler.send_snapshot_to(Dead())  # must not raise

    async def test_tick_fires_due_stop_with_sigterm(self, sched_app):
        """A due stop hands off to the #2527 graceful-shutdown path by
        SIGTERMing the process — the scheduler owns no teardown itself."""
        app, sent = sched_app
        scheduler, sent, lifecycle = _scheduler_app(sched_app)
        await app.state.model.server_schedules.create_schedule(
            "stop",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            created_by="u1",
        )
        with patch("klangk.server_schedule.os.kill") as mock_kill:
            await scheduler.sweep()
        mock_kill.assert_called_once()
        args = mock_kill.call_args[0]
        assert args[1] is signal.SIGTERM
        # Row deleted and fired event broadcast before the handoff.
        assert await app.state.model.server_schedules.pending_schedules() == []
        fired = [m for m in sent if m["type"] == "server_schedule_fired"]
        assert fired and fired[0]["action"] == "stop"

    async def test_tick_fires_due_recycle_via_request(self, sched_app):
        """A due recycle requests the SIGHUP graceful path (in-process,
        never exits)."""
        app, sent = sched_app
        scheduler, sent, lifecycle = _scheduler_app(sched_app)
        await app.state.model.server_schedules.create_schedule(
            "recycle",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            created_by="u1",
        )
        await scheduler.sweep()
        lifecycle.request_recycle.assert_called_once_with(
            source="scheduled recycle"
        )
        assert await app.state.model.server_schedules.pending_schedules() == []
        fired = [m for m in sent if m["type"] == "server_schedule_fired"]
        assert fired and fired[0]["action"] == "recycle"

    async def test_stop_skipped_when_shutting_down(self, sched_app):
        """A stop firing during a shutdown-in-progress must NOT send a
        second SIGTERM — the launcher's force-exit branch would abort
        the first drain mid-flight (#2661 review)."""
        app, sent = sched_app
        scheduler, sent, lifecycle = _scheduler_app(
            sched_app, shutting_down=True
        )
        await app.state.model.server_schedules.create_schedule(
            "stop",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            created_by="u1",
        )
        with patch("klangk.server_schedule.os.kill") as mock_kill:
            await scheduler.sweep()
        mock_kill.assert_not_called()
        # The row was still consumed — it must not re-fire on a restart.
        assert await app.state.model.server_schedules.pending_schedules() == []

    async def test_fire_skipped_when_cancelled_first(self, sched_app):
        """A cancel that removes the row between the tick's snapshot and
        the fire wins: the action must not run (#2661 review)."""
        app, sent = sched_app
        scheduler, sent, _ = _scheduler_app(sched_app)
        schedule = await app.state.model.server_schedules.create_schedule(
            "stop",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            created_by="u1",
        )
        # The row is gone (the admin's cancel won the race) — _fire's
        # claim finds nothing and must not run the action. Call _fire
        # directly with the stale in-memory dict: that is exactly the
        # state a cancel landing between the tick's snapshot and the
        # fire leaves behind.
        await app.state.model.server_schedules.cancel_schedule(schedule["id"])
        with patch("klangk.server_schedule.os.kill") as mock_kill:
            await scheduler._fire(schedule)
        mock_kill.assert_not_called()
        assert "server_schedule_fired" not in [m["type"] for m in sent]

    async def test_malformed_fire_at_row_skipped_not_fatal(self, sched_app):
        """A hand-edited row with a bad fire_at is skipped + logged; the
        healthy rows keep broadcasting and firing (#2661 review)."""
        app, sent = sched_app
        scheduler, sent, _ = _scheduler_app(sched_app)
        good = await app.state.model.server_schedules.create_schedule(
            "stop",
            datetime.now(timezone.utc) + timedelta(hours=1),
            created_by="u1",
        )
        async with app.state.db.transaction() as db:
            await db.execute(
                "INSERT INTO server_schedules"
                " (id, action, fire_at, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    "bad-row",
                    "stop",
                    "not-a-date",
                    "u1",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        with patch("klangk.server_schedule.os.kill"):
            await scheduler.sweep()  # must not raise
        # The healthy row still broadcasts; the bad one is carried in the
        # pending list (never fired) so clients see it exists.
        snapshot = [m for m in sent if m["type"] == "server_schedule"]
        assert snapshot and snapshot[-1]["schedules"], "no broadcast"
        ids = [s["id"] for s in snapshot[-1]["schedules"]]
        assert good["id"] in ids and "bad-row" in ids
        assert "server_schedule_fired" not in [m["type"] for m in sent]

    async def test_recycle_skipped_when_shutting_down(self, sched_app):
        """A recycle firing during a shutdown is a no-op (the exit owns
        the process)."""
        app, sent = sched_app
        scheduler, sent, lifecycle = _scheduler_app(
            sched_app, shutting_down=True
        )
        await app.state.model.server_schedules.create_schedule(
            "recycle",
            datetime.now(timezone.utc) - timedelta(seconds=1),
            created_by="u1",
        )
        await scheduler.sweep()
        lifecycle.request_recycle.assert_not_called()
        # The row was still consumed — it must not re-fire on a restart.
        assert await app.state.model.server_schedules.pending_schedules() == []

    async def test_tick_future_schedule_only_broadcasts(self, sched_app):
        app, sent = sched_app
        scheduler, sent, _ = _scheduler_app(sched_app)
        with patch("klangk.server_schedule.os.kill") as mock_kill:
            await app.state.model.server_schedules.create_schedule(
                "stop",
                datetime.now(timezone.utc) + timedelta(hours=1),
                created_by="u1",
            )
            await scheduler.sweep()
        mock_kill.assert_not_called()
        assert "server_schedule_fired" not in [m["type"] for m in sent]
        assert sent and sent[-1]["type"] == "server_schedule"

    async def test_broadcast_periodicity_resets(self, sched_app):
        """Same pending set re-broadcasts only after the cadence window."""
        app, sent = sched_app
        scheduler, sent, _ = _scheduler_app(sched_app)
        await app.state.model.server_schedules.create_schedule(
            "stop",
            datetime.now(timezone.utc) + timedelta(hours=1),
            created_by="u1",
        )
        await scheduler.sweep()
        first = len(sent)
        await scheduler.sweep()  # immediately again: set unchanged, cadence
        # not elapsed -> no second broadcast.
        assert len(sent) == first
        # Pretend the cadence window elapsed.
        scheduler._last_broadcast = datetime.now(timezone.utc) - timedelta(
            seconds=31
        )
        await scheduler.sweep()
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

        scheduler.sweep = flaky_tick
        with patch("klangk.server_schedule.POLL_INTERVAL_SECONDS", 0.01):
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
        live.send_json.assert_called_once_with({"type": "x"})


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
