"""Tests for :mod:`klangk.session_idle` and the WebSocket half of the
idle session timeout (#3151): the per-connection idle clock, the sweep
that closes quiet sockets, and the loop contract of the monitor. The
refresh-seam half (HTTP) lives in test_auth.py.
"""

from __future__ import annotations

import asyncio
import time
import types

from unittest.mock import AsyncMock

from klangk import session_idle
from klangk.wshandler.connection import Connection
from klangk.wshandler.session import WebSocketState


def _app(*, minutes=15):
    """A minimal app with the armed/unarmed setting and stub auth."""
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.state.settings = types.SimpleNamespace(
        session_idle_timeout_minutes=minutes
    )
    app.state.auth = types.SimpleNamespace(
        idle_window_minutes_for_user=AsyncMock(return_value=minutes)
    )
    return app


def _conn(app, *, idle_secs=0.0):
    """A Connection whose idle clock reads *idle_secs* seconds ago."""
    conn = types.SimpleNamespace(
        user={"id": "u1", "email": "idle@example.com"},
        last_seen_monotonic=time.monotonic() - idle_secs,
    )
    return conn


class _Sock:
    """Records close() calls so assertions don't need a real socket."""

    def __init__(self):
        self.closed_with = None

    async def close(self, code=1000, reason=""):
        self.closed_with = (code, reason)


class TestCloseIdleConnections:
    async def test_closes_quiet_socket_past_window(self):
        """A connection with no frames for longer than its owner's
        window is closed 4001 (client logout, no reconnect loop)."""
        app = _app()
        state = WebSocketState(app)
        sock = _Sock()
        state.connections[sock] = _conn(app, idle_secs=16 * 60)
        closed = await state.close_idle_connections(
            app.state.auth.idle_window_minutes_for_user
        )
        assert closed == 1
        assert sock.closed_with == (4001, "Session idle timeout")

    async def test_active_socket_untouched(self):
        """A connection that sent a frame recently stays open."""
        app = _app()
        state = WebSocketState(app)
        sock = _Sock()
        state.connections[sock] = _conn(app, idle_secs=5 * 60)
        closed = await state.close_idle_connections(
            app.state.auth.idle_window_minutes_for_user
        )
        assert closed == 0
        assert sock.closed_with is None

    async def test_window_resolved_per_user(self):
        """Suspects get their own (admin-aware) window: an admin at 11
        minutes with a 15-minute setting is still closed (their window
        is the 10-minute privileged one)."""
        app = _app()
        app.state.auth.idle_window_minutes_for_user = AsyncMock(
            return_value=10
        )
        state = WebSocketState(app)
        sock = _Sock()
        state.connections[sock] = _conn(app, idle_secs=11 * 60)
        closed = await state.close_idle_connections(
            app.state.auth.idle_window_minutes_for_user
        )
        assert closed == 1
        app.state.auth.idle_window_minutes_for_user.assert_awaited_once_with(
            "u1"
        )

    async def test_window_disabled_mid_flight_reopens(self):
        """A suspect whose resolved window is 0 (setting turned off on
        SIGHUP between the pre-filter and the resolution) is left
        alone."""
        app = _app()
        app.state.auth.idle_window_minutes_for_user = AsyncMock(return_value=0)
        state = WebSocketState(app)
        sock = _Sock()
        state.connections[sock] = _conn(app, idle_secs=60 * 60)
        closed = await state.close_idle_connections(
            app.state.auth.idle_window_minutes_for_user
        )
        assert closed == 0
        assert sock.closed_with is None

    async def test_pre_filter_skips_non_suspects(self):
        """Connections idle less than the shortest possible window never
        reach the window resolver — the common sweep costs no DB reads."""
        app = _app()
        state = WebSocketState(app)
        state.connections[_Sock()] = _conn(app, idle_secs=9 * 60)
        await state.close_idle_connections(
            app.state.auth.idle_window_minutes_for_user
        )
        app.state.auth.idle_window_minutes_for_user.assert_not_awaited()


class TestSessionIdleMonitor:
    def test_interval_scales_with_window(self):
        # 15 min -> a third is 300s, clamped to the 60s ceiling.
        assert session_idle.SessionIdleMonitor(_app()).interval == 60.0
        assert (
            session_idle.SessionIdleMonitor(_app(minutes=0)).interval == 60.0
        )
        # A huge window clamps to the 60-second ceiling.
        assert (
            session_idle.SessionIdleMonitor(_app(minutes=600)).interval == 60.0
        )
        # A one-minute window sweeps every 20s.
        assert (
            session_idle.SessionIdleMonitor(_app(minutes=1)).interval == 20.0
        )

    async def test_sweep_unarmed_is_noop(self):
        app = _app(minutes=0)
        app.state.sockets = types.SimpleNamespace(
            close_idle_connections=AsyncMock()
        )
        await session_idle.SessionIdleMonitor(app).sweep()
        app.state.sockets.close_idle_connections.assert_not_awaited()

    async def test_sweep_delegates_with_auth_resolver(self):
        app = _app()
        app.state.sockets = types.SimpleNamespace(
            close_idle_connections=AsyncMock(return_value=2)
        )
        await session_idle.SessionIdleMonitor(app).sweep()
        app.state.sockets.close_idle_connections.assert_awaited_once_with(
            app.state.auth.idle_window_minutes_for_user
        )

    async def test_sweep_with_nothing_to_close_stays_quiet(self, caplog):
        """A sweep that closed nothing does not log."""
        import logging

        app = _app()
        app.state.sockets = types.SimpleNamespace(
            close_idle_connections=AsyncMock(return_value=0)
        )
        with caplog.at_level(logging.INFO, logger="klangk.session_idle"):
            await session_idle.SessionIdleMonitor(app).sweep()
        assert not any("closed" in r.getMessage() for r in caplog.records)

    async def test_sweep_failure_does_not_kill_loop(self, monkeypatch):
        """A failing sweep logs + retries an interval later (the
        IntervalWorker contract)."""
        monkeypatch.setattr(session_idle.SessionIdleMonitor, "interval", 0.01)
        app = _app()
        app.state.sockets = types.SimpleNamespace(
            close_idle_connections=AsyncMock(side_effect=RuntimeError("db"))
        )
        mon = session_idle.SessionIdleMonitor(app)
        mon.start()
        for _ in range(200):
            if app.state.sockets.close_idle_connections.await_count >= 2:
                break
            await asyncio.sleep(0.01)
        assert app.state.sockets.close_idle_connections.await_count >= 2
        assert not mon._task.done()
        await mon.stop()


class TestConnectionFrameActivity:
    """Every inbound frame resets the connection's idle clock and
    stamps the session row (throttled)."""

    def _conn(self, *, jti="jti-1"):
        app = _app()
        app.state.auth = types.SimpleNamespace(
            record_session_activity=AsyncMock()
        )
        conn = Connection.__new__(Connection)
        conn.app = app
        conn.jti = jti
        conn.last_seen_monotonic = 0.0
        return conn

    async def test_frame_bumps_clock_and_stamps(self):
        conn = self._conn()
        before = conn.last_seen_monotonic
        await conn.mark_frame_activity()
        assert conn.last_seen_monotonic > before
        conn.app.state.auth.record_session_activity.assert_awaited_once_with(
            "jti-1"
        )

    async def test_frame_without_jti_skips_stamp(self):
        """A connection that never carried a JTI (defensive) still
        bumps its clock."""
        conn = self._conn(jti=None)
        before = conn.last_seen_monotonic
        await conn.mark_frame_activity()
        assert conn.last_seen_monotonic > before
        conn.app.state.auth.record_session_activity.assert_not_awaited()


class TestWsAuthenticateJti:
    """ws_authenticate returns the JTI alongside the user (#3151)."""

    async def test_returns_user_and_jti(self, user, app_state):
        from unittest.mock import AsyncMock

        from klangk.wshandler.dispatch import ws_authenticate

        a = app_state.state.auth
        token = await a.issue_token(user["id"], user["email"])
        ws = types.SimpleNamespace(
            query_params={"token": token}, close=AsyncMock()
        )
        authed = await ws_authenticate(ws, app_state)
        assert authed is not None
        authed_user, jti = authed
        assert authed_user["id"] == user["id"]
        assert jti == a.decode_token(token)["jti"]

    async def test_invalid_token_still_refused(self, user, app_state):
        from unittest.mock import AsyncMock

        from klangk.wshandler.dispatch import ws_authenticate

        ws = types.SimpleNamespace(
            query_params={"token": "garbage"}, close=AsyncMock()
        )
        assert await ws_authenticate(ws, app_state) is None
        ws.close.assert_awaited_once_with(code=4001, reason="Invalid token")
