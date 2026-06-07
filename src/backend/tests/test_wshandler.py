"""Tests for wshandler: WebSocket command dispatch, event forwarding, terminal, cleanup."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from fastapi import WebSocketDisconnect

from klangk_backend import (
    wshandler,
    container,
    workspaces as ws_mod,
)
from klangk_backend.util import (
    derive_hosting_info,
)
from klangk_backend.wshandler import (
    Connection,
    SafeWebSocket,
    SlowClientError,
    WorkspaceSession,
    state,
    send_error,
    handle_websocket,
    reset_workspace_state,
    _log_ws_msg,
    _SEND_QUEUE_SIZE,
)


def _mock_sock(headers=None, query_params=None):
    """Create a mock SafeWebSocket for testing.

    send_json is MagicMock (sync) because SafeWebSocket.send_json is
    synchronous — it enqueues via put_nowait, not await.
    """
    sock = AsyncMock()
    sock.headers = headers or {}
    sock.query_params = query_params or {}
    sock.accept = AsyncMock()
    sock.close = AsyncMock()
    sock.send_json = MagicMock()
    sock.receive_text = AsyncMock()
    sock.raw = sock  # identity for subscriber sets
    return sock


def _mock_raw_sock(headers=None, query_params=None):
    """Create a mock raw FastAPI WebSocket for handle_websocket tests.

    send_json is AsyncMock because the sender task awaits it.
    """
    raw_sock = AsyncMock()
    raw_sock.headers = headers or {}
    raw_sock.query_params = query_params or {}
    raw_sock.accept = AsyncMock()
    raw_sock.close = AsyncMock()
    raw_sock.send_json = AsyncMock()
    raw_sock.receive_text = AsyncMock()
    return raw_sock


def _mock_terminal(alive=True):
    t = AsyncMock()
    type(t).is_alive = PropertyMock(return_value=alive)
    t.start = AsyncMock()
    t.write = AsyncMock()
    t.resize = AsyncMock()
    t.stop = AsyncMock()
    return t


def _base_conn(user=None, ws=None):
    if ws is None:
        ws = _mock_sock()
    if user is None:
        user = {"id": "uid", "email": "testuser@example.com"}
    return Connection(ws, user)


async def _create_workspace_with_acl(user_id, name, **kwargs):
    """Create a workspace and grant the owner full ACL access."""
    from klangk_backend import model
    from klangk_backend.model import ACTION_ALLOW, PRINCIPAL_USER

    workspace = await ws_mod.create_workspace(user_id, name, **kwargs)
    await model.add_acl_entry(
        f"/workspaces/{workspace['id']}",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_USER,
        user_id=user_id,
    )
    return workspace


# --- SafeWebSocket ---


class TestSafeWebSocket:
    async def test_accept_delegates(self):
        raw = AsyncMock()
        sw = SafeWebSocket(raw)
        await sw.accept()
        raw.accept.assert_awaited_once()

    async def test_receive_text_delegates(self):
        raw = AsyncMock()
        raw.receive_text = AsyncMock(return_value="hello")
        sw = SafeWebSocket(raw)
        result = await sw.receive_text()
        assert result == "hello"

    async def test_close_delegates(self):
        raw = AsyncMock()
        sw = SafeWebSocket(raw)
        await sw.close(code=4001)
        raw.close.assert_awaited_once_with(code=4001)

    async def test_headers_delegates(self):
        raw = AsyncMock()
        raw.headers = {"host": "example.com"}
        sw = SafeWebSocket(raw)
        assert sw.headers == {"host": "example.com"}

    async def test_raw_returns_underlying(self):
        raw = AsyncMock()
        sw = SafeWebSocket(raw)
        assert sw.raw is raw

    async def test_send_json_enqueues(self):
        raw = AsyncMock()
        sw = SafeWebSocket(raw)
        sw.send_json({"type": "test"})
        # Message is in the queue, not yet sent to raw
        assert sw._queue.qsize() == 1

    async def test_sender_loop_drains_queue(self):
        raw = AsyncMock()
        sw = SafeWebSocket(raw)
        sw.send_json({"type": "a"})
        sw.send_json({"type": "b"})
        sw.start_sender()
        await sw.stop_sender()
        assert raw.send_json.call_count == 2
        raw.send_json.assert_any_await({"type": "a"})
        raw.send_json.assert_any_await({"type": "b"})

    async def test_send_json_queue_full_raises(self):
        raw = AsyncMock()
        sw = SafeWebSocket(raw, maxsize=2)
        sw.send_json({"type": "a"})
        sw.send_json({"type": "b"})
        with pytest.raises(SlowClientError):
            sw.send_json({"type": "c"})

    async def test_stop_sender_when_queue_full(self):
        raw = AsyncMock()
        # Block the sender on the first send so the queue stays full
        blocked = asyncio.Event()

        async def block_forever(data):
            blocked.set()
            await asyncio.sleep(3600)

        raw.send_json = AsyncMock(side_effect=block_forever)
        sw = SafeWebSocket(raw, maxsize=1)
        sw.send_json({"type": "a"})
        sw.start_sender()
        # Wait for sender to pick up "a" and block
        await blocked.wait()
        # Queue is now empty; fill it so sentinel can't be put
        sw.send_json({"type": "b"})
        await sw.stop_sender()
        # Should complete without hanging — stop_sender cancels the task

    async def test_stop_sender_no_task(self):
        raw = AsyncMock()
        sw = SafeWebSocket(raw)
        await sw.stop_sender()
        # Should be a no-op without error

    async def test_sender_loop_handles_ws_error(self):
        raw = AsyncMock()
        raw.send_json = AsyncMock(side_effect=RuntimeError("ws dead"))
        sw = SafeWebSocket(raw)
        sw.send_json({"type": "test"})
        sw.start_sender()
        await sw.stop_sender()
        # Sender should exit gracefully

    async def test_stop_sender_catches_unexpected_exception(self):
        """stop_sender doesn't propagate unexpected exceptions from the sender task."""
        raw = AsyncMock()
        raw.send_json = AsyncMock(side_effect=ValueError("bad value"))
        sw = SafeWebSocket(raw)
        sw.send_json({"type": "boom"})
        sw.start_sender()
        # stop_sender should not raise even though the sender dies with ValueError
        await sw.stop_sender()

    async def test_send_json_after_stop_raises(self):
        """send_json raises SlowClientError after stop_sender is called."""
        raw = AsyncMock()
        sw = SafeWebSocket(raw)
        sw.start_sender()
        await sw.stop_sender()
        with pytest.raises(SlowClientError):
            sw.send_json({"type": "late"})


# --- send_error ---


class TestSendError:
    def test_sends_error_json(self):
        sock = _mock_sock()
        send_error(sock, "bad thing")
        sock.send_json.assert_called_once_with(
            {"type": "error", "message": "bad thing"}
        )


# --- derive_hosting_info ---


class TestDeriveHostingInfo:
    def test_env_vars_take_precedence(self, monkeypatch):
        monkeypatch.setenv("KLANGK_HOSTING_HOSTNAME", "env.example.com")
        monkeypatch.setenv("KLANGK_HOSTING_PROTO", "https")
        monkeypatch.setenv("KLANGK_HOSTING_BASE_PATH", "/app")
        sock = _mock_sock(headers={"host": "header.example.com"})
        h, p, b = derive_hosting_info(sock.headers)
        assert h == "env.example.com"
        assert p == "https"
        assert b == "/app"

    def test_forwarded_host_used_as_is(self, monkeypatch):
        """Behind external reverse proxy — trust X-Forwarded-Host."""
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.setenv("KLANGK_NGINX_PORT", "8995")
        sock = _mock_sock(
            headers={
                "x-forwarded-host": "arctor.repoze.org",
                "x-forwarded-proto": "https",
                "x-forwarded-prefix": "/klangk",
            }
        )
        h, p, b = derive_hosting_info(sock.headers)
        assert h == "arctor.repoze.org"
        assert p == "https"
        assert b == "/klangk"

    def test_host_header_with_nginx_port(self, monkeypatch):
        """Direct access (local dev) — substitute nginx port."""
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.setenv("KLANGK_NGINX_PORT", "8995")
        sock = _mock_sock(headers={"host": "myhost:8997"})
        h, p, b = derive_hosting_info(sock.headers)
        assert h == "myhost:8995"
        assert p == "http"
        assert b == ""

    def test_host_header_no_nginx_port(self, monkeypatch):
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.delenv("KLANGK_NGINX_PORT", raising=False)
        sock = _mock_sock(headers={"host": "myhost:8997"})
        h, p, b = derive_hosting_info(sock.headers)
        assert h == "myhost:8997"
        assert p == "http"
        assert b == ""

    def test_defaults_with_nginx_port(self, monkeypatch):
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.setenv("KLANGK_NGINX_PORT", "8995")
        sock = _mock_sock(headers={})
        h, p, b = derive_hosting_info(sock.headers)
        assert h == "localhost:8995"
        assert p == "http"
        assert b == ""

    def test_defaults_no_nginx_port(self, monkeypatch):
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.delenv("KLANGK_NGINX_PORT", raising=False)
        sock = _mock_sock(headers={})
        h, p, b = derive_hosting_info(sock.headers)
        assert h == "localhost"
        assert p == "http"
        assert b == ""


# --- handle_steer ---


class TestHandleTerminalInput:
    async def test_writes_data(self):
        t = _mock_terminal()
        conn = _base_conn()
        conn.terminal_session = t
        conn.container_id = "cid"
        container.registry.track_activity("cid", "ws")

        await conn.handle_terminal_input({"data": "ls\n"})

        t.write.assert_awaited_once_with("ls\n")
        container.registry.states.pop("ws", None)

    async def test_no_session(self):
        conn = _base_conn()
        await conn.handle_terminal_input({"data": "ls\n"})
        assert conn.terminal_session is None

    async def test_dead_session(self):
        t = _mock_terminal(alive=False)
        conn = _base_conn()
        conn.terminal_session = t
        await conn.handle_terminal_input({"data": "ls\n"})
        t.write.assert_not_awaited()

    async def test_oversized_input_dropped(self):
        t = _mock_terminal()
        conn = _base_conn()
        conn.terminal_session = t
        conn.container_id = "cid"
        container.registry.track_activity("cid", "ws")

        big_data = "x" * 70000
        await conn.handle_terminal_input({"data": big_data})
        t.write.assert_not_awaited()
        container.registry.states.pop("ws", None)


# --- handle_terminal_resize ---


class TestHandleTerminalResize:
    async def test_resize(self):
        t = _mock_terminal()
        conn = _base_conn()
        conn.terminal_session = t

        await conn.handle_terminal_resize({"cols": 120, "rows": 40})

        t.resize.assert_awaited_once_with(120, 40)

    async def test_resize_defaults(self):
        t = _mock_terminal()
        conn = _base_conn()
        conn.terminal_session = t

        await conn.handle_terminal_resize({})

        t.resize.assert_awaited_once_with(80, 24)

    async def test_no_session(self):
        conn = _base_conn()
        await conn.handle_terminal_resize({"cols": 120, "rows": 40})
        assert conn.terminal_session is None


# --- handle_terminal_stop ---


class TestHandleTerminalStop:
    async def test_stops_session(self):
        t = _mock_terminal()
        conn = _base_conn()
        conn.terminal_session = t
        conn.terminal_task = asyncio.create_task(asyncio.sleep(10))

        await conn.handle_terminal_stop()

        t.stop.assert_awaited_once()
        assert conn.terminal_session is None
        assert conn.terminal_task is None

    async def test_no_session(self):
        conn = _base_conn()
        await conn.handle_terminal_stop()
        assert conn.terminal_session is None
        assert conn.terminal_task is None


# --- handle_terminal_start ---


class TestHandleTerminalStart:
    async def test_starts_session(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        container.registry.track_activity("cid", "ws")

        with patch.object(wshandler, "TerminalSession") as MockTS:
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield  # make it an async generator

            mock_session.output = fake_output

            await conn.handle_terminal_start({"cols": 100, "rows": 30})
            # Let the background task run
            await asyncio.sleep(0)

        MockTS.assert_called_once_with("cid")
        # bridge_token should be passed (a UUID string)
        start_kwargs = mock_session.start.call_args
        assert start_kwargs[1]["command_override"] is None
        assert start_kwargs[1]["bridge_token"] is not None
        assert conn._bridge_token == start_kwargs[1]["bridge_token"]
        assert conn.terminal_session is mock_session
        assert conn.terminal_task is not None
        # Should have sent terminal_started ack
        sock.send_json.assert_called_with({"type": "terminal_started"})

        # Clean up
        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        container.registry.revoke_bridge_token("ws")
        container.registry.states.pop("ws", None)

    async def test_restart_revokes_old_bridge_token(self):
        """Starting a second terminal revokes the previous bridge token."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        container.registry.track_activity("cid", "ws")

        with patch.object(wshandler, "TerminalSession") as MockTS:
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield

            mock_session.output = fake_output

            # First terminal start
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)
            first_token = conn._bridge_token
            assert first_token is not None
            assert (
                container.registry.resolve_bridge_token(first_token)
                is not None
            )

            # Cancel the first terminal task
            conn.terminal_task.cancel()
            try:
                await conn.terminal_task
            except asyncio.CancelledError:
                pass

            # Second terminal start — should revoke first token
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)
            second_token = conn._bridge_token
            assert second_token is not None
            assert second_token != first_token

            # Old token should be revoked
            assert container.registry.resolve_bridge_token(first_token) is None
            # New token should be valid
            assert (
                container.registry.resolve_bridge_token(second_token)
                is not None
            )

            conn.terminal_task.cancel()
            try:
                await conn.terminal_task
            except asyncio.CancelledError:
                pass
        container.registry.revoke_bridge_token("ws")
        container.registry.states.pop("ws", None)

    async def test_passes_command_override(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.is_alive = True
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk_backend.wshandler.TerminalSession", MockTS):
            await conn.handle_terminal_start(
                {"cols": 80, "rows": 24, "commandOverride": "bash"}
            )
            # Let the background task run
            await asyncio.sleep(0)

        mock_session.start.assert_awaited_once_with(
            80, 24, command_override="bash", bridge_token=conn._bridge_token
        )

        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        container.registry.revoke_bridge_token("ws")
        container.registry.states.pop("ws", None)

    async def test_start_failure_sends_error(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.start = AsyncMock(
            side_effect=RuntimeError("podman broke")
        )
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk_backend.wshandler.TerminalSession", MockTS):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        # Should have sent an error, not terminal_started
        sent = sock.send_json.call_args_list
        assert any(call.args[0].get("type") == "error" for call in sent)
        # Session is stored immediately but stop() is called on failure
        mock_session.stop.assert_awaited_once()
        container.registry.revoke_bridge_token("ws")
        container.registry.states.pop("ws", None)

    async def test_cancellation_during_start_cleans_up(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.start = AsyncMock(side_effect=asyncio.CancelledError)
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk_backend.wshandler.TerminalSession", MockTS):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            task = conn.terminal_task
            with pytest.raises(asyncio.CancelledError):
                await task

        # session.stop() must be called to clean up the PTY subprocess
        mock_session.stop.assert_awaited_once()
        container.registry.revoke_bridge_token("ws")
        container.registry.states.pop("ws", None)

    async def test_session_replaced_during_start_aborts(self):
        """If stop_terminal replaces the session while start() is running,
        the startup task stops the orphaned session and does not send
        terminal_started."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()

        async def start_and_replace(*a, **kw):
            # Simulate stop_terminal replacing the session mid-start
            conn.terminal_session = AsyncMock()

        mock_session.start = AsyncMock(side_effect=start_and_replace)
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk_backend.wshandler.TerminalSession", MockTS):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        # The orphaned session must be stopped
        mock_session.stop.assert_awaited_once()
        # terminal_started must NOT be sent
        for call in sock.send_json.call_args_list:
            assert call.args[0].get("type") != "terminal_started"
        container.registry.revoke_bridge_token("ws")
        container.registry.states.pop("ws", None)

    async def test_no_container(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_terminal_start({})
        assert conn.terminal_session is None


# --- forward_terminal_output ---


class TestForwardTerminalOutput:
    async def test_forwards_output(self):
        sock = _mock_sock()
        t = _mock_terminal()
        conn = _base_conn(ws=sock)
        conn.container_id = "ctr-fwd"
        conn.terminal_session = t
        container.registry.track_activity("ctr-fwd", "ws-fwd")

        async def fake_output():
            yield "line1"
            yield "line2"

        t.output = fake_output

        await conn.forward_terminal_output(t)

        # Session claimed and stopped by finally block
        assert conn.terminal_session is None
        t.stop.assert_awaited_once()
        calls = sock.send_json.call_args_list
        assert calls[0][0][0] == {"type": "terminal_output", "data": "line1"}
        assert calls[1][0][0] == {"type": "terminal_output", "data": "line2"}
        # Stream ended — container_stopped event sent
        assert calls[2][0][0]["type"] == "event"
        assert calls[2][0][0]["event"]["name"] == "container_stopped"
        # Activity was bumped on each output chunk
        assert "ws-fwd" in container.registry.states
        container.registry.states.pop("ws-fwd", None)

    async def test_cancelled_error_propagates(self):
        sock = _mock_sock()
        t = _mock_terminal()
        conn = _base_conn(ws=sock)

        async def cancel_output():
            raise asyncio.CancelledError()
            yield  # noqa

        t.output = cancel_output

        with pytest.raises(asyncio.CancelledError):
            await conn.forward_terminal_output(t)

    async def test_ws_error_logged(self):
        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=RuntimeError("ws closed"))
        t = _mock_terminal()
        conn = _base_conn(ws=sock)

        async def fake_output():
            yield "data"

        t.output = fake_output

        await conn.forward_terminal_output(t)
        # The error send_json was called (it raised, triggering the handler)
        assert sock.send_json.call_count >= 1

    async def test_ws_error_then_stop_event_also_fails(self):
        sock = _mock_sock()
        t = _mock_terminal()
        conn = _base_conn(ws=sock)

        sock.send_json = MagicMock(side_effect=ConnectionError("ws dead"))

        async def fake_output():
            yield "data"

        t.output = fake_output

        await conn.forward_terminal_output(t)
        # Both sends failed — verify both were attempted
        assert sock.send_json.call_count == 2


# --- forward_events ---


def _setup_workspace_state(workspace_id, sock, pi, container_id="cid-1"):
    """Helper to set up _workspace_state for forward_events tests."""
    session = WorkspaceSession(workspace_id)
    session.container_id = container_id
    session.subscribers = {sock}
    wshandler.state.sessions[workspace_id] = session


def _teardown_workspace_state(workspace_id):
    wshandler.state.sessions.pop(workspace_id, None)
    container.registry.states.pop(workspace_id, None)


class TestCleanupConnection:
    async def test_cleanup_last_subscriber_removes_session(self):
        sock = _mock_sock()
        t = _mock_terminal()
        conn = _base_conn(ws=sock)
        conn.container_id = "ctr-full"
        conn.workspace_id = "ws-cleanup-1"
        conn._idle_cb = lambda ws: None
        conn.terminal_session = t
        conn.terminal_task = asyncio.create_task(asyncio.sleep(10))

        container.registry.track_activity("ctr-full", "ws-cleanup-1")
        session = WorkspaceSession("ws-cleanup-1")
        session.subscribers.add(sock)
        wshandler.state.sessions["ws-cleanup-1"] = session
        container.registry.states["ws-cleanup-1"].idle_callbacks.append(
            conn._idle_cb
        )

        await conn.cleanup()

        t.stop.assert_awaited_once()
        assert conn._idle_cb is None
        assert conn.terminal_session is None
        # Session removed when last subscriber disconnects
        assert "ws-cleanup-1" not in wshandler.state.sessions

        container.registry.states.pop("ws-cleanup-1", None)

    async def test_cleanup_other_subscribers_remain(self):
        """When other subscribers remain, session stays alive."""
        sock = _mock_sock()
        other_sock = _mock_sock()
        t = _mock_terminal()
        conn = _base_conn(ws=sock)
        conn.container_id = "ctr-shared"
        conn.workspace_id = "ws-cleanup-2"
        conn._idle_cb = lambda ws: None
        conn.terminal_session = t
        conn.terminal_task = asyncio.create_task(asyncio.sleep(10))

        container.registry.track_activity("ctr-shared", "ws-cleanup-2")
        session = WorkspaceSession("ws-cleanup-2")
        session.subscribers.add(sock)
        session.subscribers.add(other_sock)
        wshandler.state.sessions["ws-cleanup-2"] = session
        container.registry.states["ws-cleanup-2"].idle_callbacks.append(
            conn._idle_cb
        )

        await conn.cleanup()

        # Terminal for THIS connection should be stopped
        t.stop.assert_awaited_once()
        # Session still present — other subscriber remains
        assert "ws-cleanup-2" in wshandler.state.sessions
        assert other_sock in session.subscribers
        assert sock not in session.subscribers

        # Cleanup
        container.registry.states.pop("ws-cleanup-2", None)
        wshandler.state.sessions.pop("ws-cleanup-2", None)

    async def test_cleanup_minimal(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.cleanup()
        assert conn.terminal_session is None


# --- handle_prompt ---


class TestHandleWorkspaceConnect:
    async def test_missing_workspace_id(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_workspace_connect({})
        assert "Missing" in sock.send_json.call_args[0][0]["message"]

    async def test_workspace_not_found(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        await conn.handle_workspace_connect({"workspaceId": "fake"})
        assert "Permission denied" in sock.send_json.call_args[0][0]["message"]

    async def test_connect_success(self, user):
        sock = _mock_sock()
        workspace = await _create_workspace_with_acl(user["id"], "test-ws")
        conn = _base_conn(user=user, ws=sock)

        async def fake_start(wid, workspace):
            conn.container_id = "cid"

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[9000, 9001],
            ),
        ):
            await conn.handle_workspace_connect(
                {"workspaceId": workspace["id"]}
            )

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        ready = [c for c in calls if c.get("type") == "workspace_ready"]
        assert len(ready) == 1
        assert ready[0]["workspaceId"] == workspace["id"]
        assert ready[0]["defaultCommand"] is None
        # Integer timeout (default 30m) should show as "30m" not "30.0m"
        assert "30m" in conn.pending_status_msg

    async def test_connect_sends_default_command(self, user):
        sock = _mock_sock()
        workspace = await _create_workspace_with_acl(
            user["id"], "cmd-ws", default_command="pi"
        )
        conn = _base_conn(user=user, ws=sock)

        async def fake_start(wid, workspace):
            conn.container_id = "cid"

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[9000],
            ),
        ):
            await conn.handle_workspace_connect(
                {"workspaceId": workspace["id"]}
            )

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        ready = [c for c in calls if c.get("type") == "workspace_ready"]
        assert ready[0]["defaultCommand"] == "pi"

    async def test_connect_denied_no_acl(self, user):
        """User without ACL entry gets 'Permission denied'."""
        sock = _mock_sock()
        workspace = await ws_mod.create_workspace(user["id"], "no-acl-ws")
        conn = _base_conn(user={"id": "other-user", "email": "x"}, ws=sock)
        await conn.handle_workspace_connect({"workspaceId": workspace["id"]})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission denied" in str(c) for c in calls)

    async def test_connect_race_deleted(self, user):
        """ACL passes but workspace deleted before lookup."""
        from klangk_backend import model

        sock = _mock_sock()
        fake_id = "deleted-ws-id"
        await model.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        conn = _base_conn(user=user, ws=sock)
        await conn.handle_workspace_connect({"workspaceId": fake_id})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Workspace not found" in str(c) for c in calls)


class TestHandleWorkspaceDisconnect:
    async def test_disconnect(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws-1"

        with patch.object(
            container.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            await conn.handle_workspace_disconnect()

        assert conn.workspace_id is None
        assert conn.container_id is None


# --- handle_restart_container ---


class TestStartWorkspaceContainer:
    async def test_new_session(self, user):
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock)
        workspace = await ws_mod.create_workspace(user["id"], "start-ws")

        async def fake_start(*a, **kw):
            container.registry.track_activity("cid-1", workspace["id"])
            return ("cid-1", "created")

        with (
            patch.object(
                container.registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch("glob.glob", return_value=[]),
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        assert conn.container_id == "cid-1"
        assert conn.workspace == workspace
        assert workspace["id"] in wshandler.state.sessions
        assert conn._idle_cb is not None

        wshandler.state.sessions.pop(workspace["id"], None)
        container.registry.states.pop(workspace["id"], None)

    async def test_idle_callback_ws_error(self, user):
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock)
        workspace = await ws_mod.create_workspace(user["id"], "idle-ws")

        async def fake_start(*a, **kw):
            container.registry.track_activity("cid-3", workspace["id"])
            return ("cid-3", "created")

        with (
            patch.object(
                container.registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch("glob.glob", return_value=[]),
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        # Test idle callback when WS send fails
        sock.send_json = MagicMock(side_effect=RuntimeError("ws closed"))
        idle_cb = conn._idle_cb
        await idle_cb(workspace["id"])  # should not raise
        assert sock.send_json.call_count == 1

        wshandler.state.sessions.pop(workspace["id"], None)
        container.registry.states.pop(workspace["id"], None)

    async def test_clears_pending_status_msg(self, user):
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock)
        conn.pending_status_msg = "stale message from prior connect"
        workspace = await ws_mod.create_workspace(user["id"], "pending-ws")

        async def fake_start(*a, **kw):
            container.registry.track_activity("cid-p", workspace["id"])
            return ("cid-p", "created")

        with (
            patch.object(
                container.registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch("glob.glob", return_value=[]),
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        assert conn.pending_status_msg is None

        wshandler.state.sessions.pop(workspace["id"], None)
        container.registry.states.pop(workspace["id"], None)


# --- handle_websocket dispatch branches ---


class TestHandleWebsocketDispatch:
    """Test all command dispatch branches through the main handler."""

    async def _run_commands(self, user, commands):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        msgs = [json.dumps(c) for c in commands] + [WebSocketDisconnect()]
        websocket.receive_text = AsyncMock(side_effect=msgs)
        await handle_websocket(websocket)
        return websocket

    async def test_dispatch_terminal_start(self, user):
        websocket = await self._run_commands(user, [{"cmd": "terminal_start"}])
        websocket.accept.assert_awaited_once()

    async def test_dispatch_terminal_input(self, user):
        websocket = await self._run_commands(
            user, [{"cmd": "terminal_input", "data": "x"}]
        )
        websocket.accept.assert_awaited_once()

    async def test_dispatch_terminal_resize(self, user):
        websocket = await self._run_commands(
            user, [{"cmd": "terminal_resize", "cols": 80, "rows": 24}]
        )
        websocket.accept.assert_awaited_once()

    async def test_dispatch_terminal_stop(self, user):
        websocket = await self._run_commands(user, [{"cmd": "terminal_stop"}])
        websocket.accept.assert_awaited_once()

    async def test_dispatch_restart_container(self, user):
        websocket = await self._run_commands(
            user, [{"cmd": "restart_container"}]
        )
        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_dispatch_workspace_connect(self, user):
        websocket = await self._run_commands(
            user, [{"cmd": "workspace_connect"}]
        )
        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Missing" in str(c) for c in calls)

    async def test_dispatch_workspace_disconnect(self, user):
        websocket = await self._run_commands(
            user, [{"cmd": "workspace_disconnect"}]
        )
        websocket.accept.assert_awaited_once()

    async def test_container_survives_disconnect(self, user):
        """Container should NOT be killed on disconnect — idle timeout handles it."""
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})

        workspace = await ws_mod.create_workspace(user["id"], "stop-ws")
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "cmd": "workspace_connect",
                        "workspaceId": workspace["id"],
                    }
                ),
                WebSocketDisconnect(),
            ]
        )

        async def fake_start(self_arg, wid, ws_obj):
            self_arg.workspace_id = wid
            self_arg.container_id = "cid-stop"

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                autospec=True,
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[],
            ),
            patch.object(
                container.registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ) as mock_stop,
        ):
            await handle_websocket(websocket)

        mock_stop.assert_not_awaited()


# --- handle_restart_container additional coverage ---


class TestHandleWebsocket:
    async def test_missing_token(self):
        websocket = _mock_raw_sock(query_params={})
        await handle_websocket(websocket)
        websocket.close.assert_awaited_once_with(
            code=4001, reason="Missing token"
        )

    async def test_invalid_token(self, db):
        websocket = _mock_raw_sock(query_params={"token": "bad"})
        await handle_websocket(websocket)
        websocket.close.assert_awaited_once_with(
            code=4001, reason="Invalid token"
        )

    async def test_valid_token_then_disconnect(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(side_effect=WebSocketDisconnect())

        await handle_websocket(websocket)

        websocket.accept.assert_awaited_once()

    async def test_invalid_json(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=["not json", WebSocketDisconnect()]
        )

        await handle_websocket(websocket)

        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Invalid JSON" in str(c) for c in calls)

    async def test_unknown_command(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "bogus"}),
                WebSocketDisconnect(),
            ]
        )

        await handle_websocket(websocket)

        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Unknown command" in str(c) for c in calls)

    async def test_ui_ready_with_pending(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        workspace = await _create_workspace_with_acl(user["id"], "ui-ready-ws")

        async def fake_start(self_arg, wid, ws_obj):
            self_arg.container_id = "cid"
            self_arg.workspace_id = wid
            wshandler.state.get_or_create_session(wid)

        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "cmd": "workspace_connect",
                        "workspaceId": workspace["id"],
                    }
                ),
                json.dumps({"cmd": "ui_ready"}),
                WebSocketDisconnect(),
            ]
        )

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                autospec=True,
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[],
            ),
            patch.object(
                container.registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
        ):
            await handle_websocket(websocket)

        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        ready = [
            c
            for c in calls
            if isinstance(c, dict)
            and c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(ready) == 1

    async def test_ui_ready_no_pending(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "ui_ready"}),
                WebSocketDisconnect(),
            ]
        )

        await handle_websocket(websocket)

        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        ready = [
            c
            for c in calls
            if isinstance(c, dict)
            and c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(ready) == 0

    async def test_general_exception_logged(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=RuntimeError("unexpected")
        )

        await handle_websocket(websocket)

        websocket.accept.assert_awaited_once()
        assert websocket not in wshandler.state.connections


class TestExecHandlers:
    async def test_exec_start_no_container(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_exec_start({"command": ["ls"]})
        assert conn.exec_session is None

    async def test_exec_start_no_command(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        await conn.handle_exec_start({"command": []})
        sock.send_json.assert_called()
        assert "command" in sock.send_json.call_args[0][0].get("message", "")

    async def test_exec_start_success(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        mock_session = AsyncMock()
        mock_session.start = AsyncMock()

        async def empty_output():
            return
            yield  # pragma: no cover

        mock_session.output = empty_output
        mock_session.returncode = 0
        with patch(
            "klangk_backend.wshandler.ExecSession",
            return_value=mock_session,
        ):
            with patch.object(container.registry, "record_activity"):
                await conn.handle_exec_start({"command": ["ls"]})
        assert conn.exec_session is mock_session
        assert conn.exec_task is not None
        conn.exec_task.cancel()
        try:
            await conn.exec_task
        except asyncio.CancelledError:
            pass

    async def test_exec_input_sends_data(self):
        import base64

        session = AsyncMock()
        session.is_alive = True
        conn = _base_conn()
        conn.container_id = "cid"
        conn.exec_session = session
        data = base64.b64encode(b"hello").decode()
        with patch.object(container.registry, "record_activity"):
            await conn.handle_exec_input({"data": data})
        session.write.assert_awaited_with(b"hello")

    async def test_exec_input_no_session(self):
        conn = _base_conn()
        conn.container_id = "cid"
        await conn.handle_exec_input({"data": ""})  # should not raise

    async def test_exec_input_oversized_dropped(self):
        import base64

        session = AsyncMock()
        session.is_alive = True
        conn = _base_conn()
        conn.container_id = "cid"
        conn.exec_session = session
        big_data = base64.b64encode(b"x" * 70000).decode()
        await conn.handle_exec_input({"data": big_data})
        session.write.assert_not_awaited()

    async def test_exec_close_stdin(self):
        session = AsyncMock()
        conn = _base_conn()
        conn.exec_session = session
        await conn.handle_exec_close_stdin()
        session.close_stdin.assert_awaited_once()

    async def test_exec_close_stdin_no_session(self):
        conn = _base_conn()
        await conn.handle_exec_close_stdin()  # should not raise

    async def test_exec_stop(self):
        session = AsyncMock()
        task = asyncio.create_task(asyncio.sleep(10))
        conn = _base_conn()
        conn.exec_session = session
        conn.exec_task = task
        await conn.handle_exec_stop()
        assert conn.exec_session is None
        assert conn.exec_task is None

    async def test_stop_exec_no_session(self):
        conn = _base_conn()
        await conn.stop_exec()  # should not raise

    async def test_forward_exec_output(self):
        import base64

        sock = _mock_sock()
        session = AsyncMock()
        session.returncode = 0

        async def fake_output():
            yield b"chunk1"
            yield b"chunk2"

        session.output = fake_output
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.exec_session = session
        with patch.object(container.registry, "record_activity"):
            await conn.forward_exec_output(session)
        # Session claimed and stopped by finally block
        assert conn.exec_session is None
        session.stop.assert_awaited_once()
        calls = sock.send_json.call_args_list
        output_calls = [
            c for c in calls if c[0][0].get("type") == "exec_output"
        ]
        exit_calls = [c for c in calls if c[0][0].get("type") == "exec_exit"]
        assert len(output_calls) == 2
        assert base64.b64decode(output_calls[0][0][0]["data"]) == b"chunk1"
        assert len(exit_calls) == 1
        assert exit_calls[0][0][0]["code"] == 0

    async def test_forward_exec_output_ws_error(self):
        sock = _mock_sock()
        session = AsyncMock()

        async def fake_output():
            yield b"data"

        session.output = fake_output
        sock.send_json = MagicMock(side_effect=RuntimeError("ws dead"))
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        with patch.object(container.registry, "record_activity"):
            await conn.forward_exec_output(session)
        # Should not raise

    async def test_cleanup_connection_stops_exec(self):
        session = AsyncMock()
        task = asyncio.create_task(asyncio.sleep(10))
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.exec_session = session
        conn.exec_task = task
        await conn.cleanup()
        session.stop.assert_awaited_once()
        assert conn.exec_session is None


class TestExecDispatch:
    async def test_dispatch_exec_start(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_start", "command": ["ls"]}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_exec_start", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket)
        mock.assert_awaited_once()

    async def test_dispatch_exec_input(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_input", "data": "AA=="}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_exec_input", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket)
        mock.assert_awaited_once()

    async def test_dispatch_exec_stop(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_stop"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_exec_stop", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket)
        mock.assert_awaited_once()

    async def test_dispatch_exec_close_stdin(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_close_stdin"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_exec_close_stdin", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket)
        mock.assert_awaited_once()

    async def test_dispatch_heartbeat(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "heartbeat"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_heartbeat", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket)
        mock.assert_awaited_once()

    async def test_dispatch_chat_send(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "chat_send", "message": "hi"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_chat_send", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket)
        mock.assert_awaited_once()

    async def test_dispatch_chat_delete(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "chat_delete", "message_id": "x"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_chat_delete", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket)
        mock.assert_awaited_once()


class TestHandleHeartbeat:
    async def test_records_activity(self):
        conn = _base_conn()
        conn.container_id = "cid-hb"
        container.registry.track_activity("cid-hb", "ws-hb")
        container.registry.states["ws-hb"].last_activity = 0.0

        await conn.handle_heartbeat()

        assert container.registry.states["ws-hb"].last_activity > 0.0
        container.registry.states.pop("ws-hb", None)
        container.registry._cid_to_wsid.pop("cid-hb", None)

    async def test_no_container_id(self):
        conn = _base_conn()
        # Should not raise
        await conn.handle_heartbeat()


class TestBrowserBridge:
    async def test_dispatch_browser_response(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "browser_response", "id": "req-1"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            wshandler.state,
            "handle_browser_response",
            wraps=wshandler.state.handle_browser_response,
        ) as mock:
            await handle_websocket(websocket)
        mock.assert_called_once()

    async def test_handle_browser_response_resolves_future(self):
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        mock_sock = _mock_sock()
        wshandler.state.pending_browser_requests["req-1"] = (
            future,
            mock_sock,
        )

        wshandler.state.handle_browser_response(
            {"id": "req-1", "status": 200, "body": "hello"}, sender=mock_sock
        )

        assert future.done()
        result = future.result()
        assert result["body"] == "hello"

    async def test_handle_browser_response_wrong_sender_rejected(self):
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        expected = _mock_sock()
        imposter = _mock_sock()
        wshandler.state.pending_browser_requests["req-2"] = (
            future,
            expected,
        )

        wshandler.state.handle_browser_response(
            {"id": "req-2", "status": 200}, sender=imposter
        )

        # Future should NOT be resolved — wrong sender
        assert not future.done()
        # Entry should still be pending
        assert "req-2" in wshandler.state.pending_browser_requests
        wshandler.state.pending_browser_requests.pop("req-2", None)

    async def test_handle_browser_response_missing_id(self):
        # Should not raise
        wshandler.state.handle_browser_response({})

    async def test_handle_browser_response_unknown_id(self):
        # Should not raise
        wshandler.state.handle_browser_response({"id": "unknown"})

    async def test_dispatch_browser_request_no_subscribers(self):
        session = wshandler.state.get_or_create_session("ws-empty")
        try:
            result = await session.dispatch_browser_request(
                {"action": "fetch", "url": "http://example.com"}
            )
            assert "error" in result
            assert "No browser client" in result["error"]
        finally:
            wshandler.state.sessions.pop("ws-empty", None)

    async def test_dispatch_browser_request_cli_only(self):
        """CLI-only connections get immediate error, not 30s timeout."""
        session = wshandler.state.get_or_create_session("ws-cli-only")
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        # No browser_subscribers — CLI never sends ui_ready
        try:
            result = await session.dispatch_browser_request(
                {"action": "fetch", "url": "http://example.com"},
            )
            assert "error" in result
            assert "No browser client" in result["error"]
        finally:
            wshandler.state.sessions.pop("ws-cli-only", None)

    async def test_dispatch_browser_request_success(self):
        session = wshandler.state.get_or_create_session("ws-bridge")
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)

        async def respond_later():
            await asyncio.sleep(0.1)
            # Find the pending request and resolve it
            for req_id, (
                future,
                _sock,
            ) in wshandler.state.pending_browser_requests.items():
                if not future.done():
                    future.set_result(
                        {"id": req_id, "status": 200, "body": "response-data"}
                    )
                    break

        task = asyncio.create_task(respond_later())
        try:
            result = await session.dispatch_browser_request(
                {"action": "fetch", "url": "http://example.com"},
                timeout=5.0,
            )
            assert result["body"] == "response-data"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            wshandler.state.sessions.pop("ws-bridge", None)

    async def test_dispatch_browser_request_timeout(self):
        session = wshandler.state.get_or_create_session("ws-timeout")
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)
        try:
            result = await session.dispatch_browser_request(
                {"action": "fetch", "url": "http://example.com"},
                timeout=0.1,
            )
            assert "error" in result
            assert "timeout" in result["error"].lower()
        finally:
            wshandler.state.sessions.pop("ws-timeout", None)


class TestDispatchBrowserRequestTo:
    async def test_success(self):
        session = wshandler.state.get_or_create_session("ws-to")
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)

        async def respond_later():
            await asyncio.sleep(0.1)
            for req_id, (
                future,
                _sock,
            ) in wshandler.state.pending_browser_requests.items():
                if not future.done():
                    future.set_result(
                        {"id": req_id, "status": 200, "body": "targeted"}
                    )
                    break

        task = asyncio.create_task(respond_later())
        try:
            result = await session.dispatch_browser_request_to(
                mock_sock,
                {"action": "fetch", "url": "http://example.com"},
                timeout=5.0,
            )
            assert result["body"] == "targeted"
            # Message should have been sent to the specific socket
            mock_sock.send_json.assert_called_once()
            sent = mock_sock.send_json.call_args[0][0]
            assert sent["type"] == "browser_request"
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            wshandler.state.sessions.pop("ws-to", None)

    async def test_dead_socket(self):
        session = wshandler.state.get_or_create_session("ws-to-dead")
        mock_sock = _mock_sock()
        mock_sock.send_json = MagicMock(side_effect=RuntimeError("ws closed"))
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)
        try:
            result = await session.dispatch_browser_request_to(
                mock_sock,
                {"action": "fetch"},
            )
            assert "error" in result
            assert "not available" in result["error"]
        finally:
            wshandler.state.sessions.pop("ws-to-dead", None)

    async def test_timeout(self):
        session = wshandler.state.get_or_create_session("ws-to-timeout")
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)
        try:
            result = await session.dispatch_browser_request_to(
                mock_sock,
                {"action": "fetch"},
                timeout=0.1,
            )
            assert "error" in result
            assert "timeout" in result["error"].lower()
        finally:
            wshandler.state.sessions.pop("ws-to-timeout", None)

    async def test_cancelled_cleans_up(self):
        session = wshandler.state.get_or_create_session("ws-to-cancel")
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)
        try:
            before = set(wshandler.state.pending_browser_requests.keys())
            task = asyncio.create_task(
                session.dispatch_browser_request_to(
                    mock_sock,
                    {"action": "fetch"},
                    timeout=10.0,
                )
            )
            await asyncio.sleep(0.05)
            new_ids = (
                set(wshandler.state.pending_browser_requests.keys()) - before
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for rid in new_ids:
                assert rid not in wshandler.state.pending_browser_requests
        finally:
            wshandler.state.sessions.pop("ws-to-cancel", None)


class TestCleanupRevokesBridgeToken:
    async def test_cleanup_revokes_connection_token(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.workspace_id = "ws-revoke"
        conn.container_id = "cid-revoke"

        # Create a per-connection bridge token
        token = container.registry.create_bridge_token("ws-revoke", sock)
        conn._bridge_token = token

        container.registry.track_activity("cid-revoke", "ws-revoke")
        session = WorkspaceSession("ws-revoke")
        session.subscribers.add(sock)
        wshandler.state.sessions["ws-revoke"] = session

        await conn.cleanup()

        # Per-connection token should be revoked
        assert container.registry.resolve_bridge_token(token) is None
        assert conn._bridge_token is None

        container.registry.revoke_bridge_token("ws-revoke")
        container.registry.states.pop("ws-revoke", None)
        wshandler.state.sessions.pop("ws-revoke", None)


class TestLogoutUser:
    async def test_kills_container_when_no_other_subscribers(self):
        """Logout kills containers when no other users are connected."""
        with (
            patch.object(
                wshandler.model,
                "get_user_workspaces_with_containers",
                return_value=[
                    {"id": "ws-logout", "container_id": "cid-logout"}
                ],
            ),
            patch.object(
                container.registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ) as mock_stop,
        ):
            await wshandler.state.logout_user("uid-1")
        mock_stop.assert_awaited_once_with("cid-logout")

    async def test_skips_container_when_other_user_connected(self):
        """Logout skips killing container if another user has an active subscription."""
        session = wshandler.state.get_or_create_session("ws-shared")
        # User B's connection
        sock_b = _mock_sock()
        conn_b = Connection(sock_b, {"id": "uid-2", "email": "b@test.com"})
        wshandler.state.connections[sock_b] = conn_b
        session.subscribers.add(sock_b)
        try:
            with (
                patch.object(
                    wshandler.model,
                    "get_user_workspaces_with_containers",
                    return_value=[
                        {"id": "ws-shared", "container_id": "cid-shared"}
                    ],
                ),
                patch.object(
                    container.registry,
                    "stop_and_remove_container",
                    new_callable=AsyncMock,
                ) as mock_stop,
            ):
                await wshandler.state.logout_user("uid-1")
            # Container should NOT have been killed
            mock_stop.assert_not_awaited()
        finally:
            wshandler.state.sessions.pop("ws-shared", None)
            wshandler.state.connections.pop(sock_b, None)

    async def test_skips_workspace_without_container(self):
        """Logout skips workspaces that have no container."""
        with (
            patch.object(
                wshandler.model,
                "get_user_workspaces_with_containers",
                return_value=[{"id": "ws-nocontainer", "container_id": None}],
            ),
            patch.object(
                container.registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ) as mock_stop,
        ):
            await wshandler.state.logout_user("uid-1")
        mock_stop.assert_not_awaited()

    async def test_kills_when_only_same_user_subscribed(self):
        """Logout kills container when only the logging-out user is subscribed."""
        session = wshandler.state.get_or_create_session("ws-self")
        sock_a = _mock_sock()
        conn_a = Connection(sock_a, {"id": "uid-1", "email": "a@test.com"})
        wshandler.state.connections[sock_a] = conn_a
        session.subscribers.add(sock_a)
        try:
            with (
                patch.object(
                    wshandler.model,
                    "get_user_workspaces_with_containers",
                    return_value=[
                        {"id": "ws-self", "container_id": "cid-self"}
                    ],
                ),
                patch.object(
                    container.registry,
                    "stop_and_remove_container",
                    new_callable=AsyncMock,
                ) as mock_stop,
            ):
                await wshandler.state.logout_user("uid-1")
            mock_stop.assert_awaited_once_with("cid-self")
        finally:
            wshandler.state.sessions.pop("ws-self", None)
            wshandler.state.connections.pop(sock_a, None)


class TestResetWorkspaceState:
    async def test_noop_for_unknown_workspace(self):
        await reset_workspace_state("ws-unknown")  # should not raise

    async def test_remove_session_noop_for_unknown(self):
        await wshandler.state.remove_session("nonexistent")  # should not raise

    async def test_removes_session_with_no_subscribers(self):
        """remove_session acquires lock and removes empty session."""
        wshandler.state.get_or_create_session("ws-reset-empty")
        assert "ws-reset-empty" in wshandler.state.sessions
        container.registry.track_activity("cid-reset", "ws-reset-empty")
        try:
            await reset_workspace_state("ws-reset-empty")
            assert "ws-reset-empty" not in wshandler.state.sessions
        finally:
            wshandler.state.sessions.pop("ws-reset-empty", None)
            container.registry.states.pop("ws-reset-empty", None)

    async def test_remove_session_skips_if_subscribers_reappear(self):
        """remove_session re-checks subscribers under lock and aborts if non-empty."""
        session = wshandler.state.get_or_create_session("ws-reappear")
        mock_sock = _mock_sock()
        # Add subscriber so the re-check inside the lock finds a non-empty set
        session.subscribers.add(mock_sock)
        try:
            await wshandler.state.remove_session("ws-reappear")
            # Session should NOT have been removed
            assert "ws-reappear" in wshandler.state.sessions
            assert mock_sock in session.subscribers
        finally:
            wshandler.state.sessions.pop("ws-reappear", None)


class TestRemoveSessionLocked:
    async def test_removes_session(self):
        session = wshandler.state.get_or_create_session("ws-locked-rm")
        try:
            async with session.lock:
                await state.remove_session_locked(session)
            assert "ws-locked-rm" not in wshandler.state.sessions
        finally:
            wshandler.state.sessions.pop("ws-locked-rm", None)


class TestCleanupSubscriberRace:
    async def test_new_subscriber_not_lost_during_cleanup(self):
        """A subscriber added under the lock while cleanup runs is not lost."""
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        session = WorkspaceSession("ws-race")
        session.subscribers.add(sock1)
        wshandler.state.sessions["ws-race"] = session

        conn = _base_conn(ws=sock1)
        conn.workspace_id = "ws-race"
        conn.container_id = "cid-race"

        # Simulate: sock1 disconnects (cleanup) while sock2 connects
        # (start_workspace_container adds sock2 under the lock).
        # We do this by adding sock2 after sock1's cleanup, verifying the session
        # and sock2 survive.

        await conn.cleanup()

        # Session should be removed since sock1 was the last subscriber
        assert "ws-race" not in wshandler.state.sessions

        # Now create a fresh session for sock2 (simulating start_workspace_container)
        session2 = wshandler.state.get_or_create_session("ws-race")
        async with session2.lock:
            session2.subscribers.add(sock2)

        assert sock2 in session2.subscribers
        assert "ws-race" in wshandler.state.sessions

        wshandler.state.sessions.pop("ws-race", None)

    async def test_concurrent_cleanup_and_add(self):
        """When cleanup holds the lock, a concurrent add waits and is not lost."""
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        session = WorkspaceSession("ws-conc")
        session.subscribers.add(sock1)
        session.subscribers.add(sock2)
        wshandler.state.sessions["ws-conc"] = session

        conn1 = _base_conn(ws=sock1)
        conn1.workspace_id = "ws-conc"
        conn1.container_id = "cid-conc"

        # sock1 disconnects, sock2 remains
        await conn1.cleanup()

        # Session should still exist because sock2 is still subscribed
        assert "ws-conc" in wshandler.state.sessions
        assert sock2 in session.subscribers
        assert sock1 not in session.subscribers

        wshandler.state.sessions.pop("ws-conc", None)


class TestWsDebugLogging:
    async def test_recv_logged_when_debug(self, user, monkeypatch):
        from klangk_backend import auth as auth_mod

        monkeypatch.setattr(wshandler, "_WS_DEBUG", True)
        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "heartbeat"}),
                WebSocketDisconnect(),
            ]
        )
        await handle_websocket(websocket)
        websocket.accept.assert_awaited_once()

    def test_send_error_logged_when_debug(self, monkeypatch):
        monkeypatch.setattr(wshandler, "_WS_DEBUG", True)
        sock = _mock_sock()
        send_error(sock, "test error")
        sock.send_json.assert_called_once()

    async def test_broadcast_sends_to_subscribers(self):
        session = wshandler.state.get_or_create_session("ws-bcast")
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        try:
            delivered = session.broadcast({"type": "test"})
            assert delivered == 1
        finally:
            wshandler.state.sessions.pop("ws-bcast", None)

    async def test_broadcast_to_browsers_sends_to_browser_subscribers(self):
        session = wshandler.state.get_or_create_session("ws-browser-bcast")
        mock_sock = _mock_sock()
        session.browser_subscribers.add(mock_sock)
        try:
            delivered = session.broadcast_to_browsers({"type": "test"})
            assert delivered == 1
        finally:
            wshandler.state.sessions.pop("ws-browser-bcast", None)


class TestLogWsMsg:
    def test_terminal_output_truncated(self):
        with patch.object(wshandler, "_WS_DEBUG", True):
            _log_ws_msg(
                "RECV",
                {"type": "terminal_output", "data": "x" * 200},
                {"email": "test@example.com"},
            )

    def test_terminal_input_truncated(self):
        with patch.object(wshandler, "_WS_DEBUG", True):
            _log_ws_msg(
                "SEND",
                {"type": "terminal_input", "data": "y" * 50},
            )

    def test_other_message(self):
        with patch.object(wshandler, "_WS_DEBUG", True):
            _log_ws_msg("RECV", {"type": "heartbeat"})

    def test_other_message_with_user(self):
        with patch.object(wshandler, "_WS_DEBUG", True):
            _log_ws_msg(
                "RECV",
                {"cmd": "workspace_connect", "workspaceId": "ws-1"},
                {"email": "test@example.com"},
            )

    def test_noop_when_debug_disabled(self):
        with patch.object(wshandler, "_WS_DEBUG", False):
            _log_ws_msg("RECV", {"type": "heartbeat"})


class TestBroadcastDeadSubscribers:
    async def test_dead_subscriber_removed(self):
        session = wshandler.state.get_or_create_session("ws-dead-sub")
        live_sock = _mock_sock()
        dead_sock = _mock_sock()
        dead_sock.send_json = MagicMock(side_effect=RuntimeError("ws closed"))
        session.subscribers.add(live_sock)
        session.subscribers.add(dead_sock)
        try:
            delivered = session.broadcast({"type": "test"})
            assert delivered == 1
            assert dead_sock not in session.subscribers
            assert live_sock in session.subscribers
        finally:
            wshandler.state.sessions.pop("ws-dead-sub", None)


class TestHandleRestartContainer:
    async def test_restart_not_connected(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_restart_container()
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_restart_success(self, user):
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await ws_mod.create_workspace(user["id"], "restart-ws")
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid-old"
        conn.workspace = workspace

        async def fake_start(wid, ws_obj):
            conn.container_id = "cid-new"
            conn.workspace_id = wid

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
            patch.object(container.registry, "record_activity"),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[9000],
            ),
        ):
            await conn.handle_restart_container()

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        restart_events = [
            c
            for c in calls
            if isinstance(c, dict)
            and c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_restart"
        ]
        ready_events = [
            c
            for c in calls
            if isinstance(c, dict)
            and c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(restart_events) == 1
        assert len(ready_events) == 1

    async def test_restart_workspace_gone(self, user):
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-gone"
        conn.container_id = "cid-gone"
        conn.workspace = None

        with (
            patch.object(
                ws_mod,
                "get_workspace",
                return_value=None,
            ),
            patch.object(
                container.registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
        ):
            await conn.handle_restart_container()

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("not found" in str(c) for c in calls)

    async def test_restart_fractional_timeout(self, user, monkeypatch):
        monkeypatch.setattr(container, "IDLE_TIMEOUT_SECONDS", 90)
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await ws_mod.create_workspace(user["id"], "restart-frac")
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid-frac"
        conn.workspace = workspace

        async def fake_start(wid, ws_obj):
            conn.container_id = "cid-frac-new"
            conn.workspace_id = wid

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
            patch.object(container.registry, "record_activity"),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            await conn.handle_restart_container()

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        ready = [
            c
            for c in calls
            if isinstance(c, dict)
            and c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(ready) == 1
        assert "1.5m" in ready[0]["event"]["value"]["reason"]

    async def test_restart_cleanup_error(self, user):
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await ws_mod.create_workspace(user["id"], "restart-err")
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid-err"
        conn.workspace = workspace

        async def fail_cleanup():
            raise RuntimeError("cleanup boom")

        async def fake_start(wid, ws_obj):
            conn.container_id = "cid-new"
            conn.workspace_id = wid

        with (
            patch.object(
                Connection,
                "cleanup",
                side_effect=fail_cleanup,
            ),
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(container.registry, "record_activity"),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            await conn.handle_restart_container()

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        ready = [
            c
            for c in calls
            if isinstance(c, dict)
            and c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(ready) == 1

    async def test_restart_cleanup_ws_disconnect(self, user):
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await ws_mod.create_workspace(user["id"], "restart-disc")
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid-disc"
        conn.workspace = workspace

        async def fail_cleanup():
            raise WebSocketDisconnect()

        async def fake_start(wid, ws_obj):
            conn.container_id = "cid-new"
            conn.workspace_id = wid

        with (
            patch.object(
                Connection,
                "cleanup",
                side_effect=fail_cleanup,
            ),
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(container.registry, "record_activity"),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            await conn.handle_restart_container()

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        ready = [
            c
            for c in calls
            if isinstance(c, dict)
            and c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(ready) == 1


class TestFractionalTimeout:
    async def test_fractional_timeout_display(self, user, monkeypatch):
        monkeypatch.setattr(container, "IDLE_TIMEOUT_SECONDS", 90)
        sock = _mock_sock()
        workspace = await _create_workspace_with_acl(user["id"], "frac-ws")
        conn = _base_conn(user=user, ws=sock)

        async def fake_start(wid, workspace):
            conn.container_id = "cid"
            conn.container_status = "created"

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            await conn.handle_workspace_connect(
                {"workspaceId": workspace["id"]}
            )

        assert "1.5m" in conn.pending_status_msg


class TestDispatchBrowserRequestCancelled:
    async def test_cancelled_cleans_up(self):
        session = wshandler.state.get_or_create_session("ws-cancel")
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)
        try:
            # Snapshot request IDs before so we can check ours was cleaned up
            before = set(wshandler.state.pending_browser_requests.keys())
            task = asyncio.create_task(
                session.dispatch_browser_request(
                    {"action": "fetch"},
                    timeout=10.0,
                )
            )
            await asyncio.sleep(0.05)
            # Find the new request_id added by our dispatch
            new_ids = (
                set(wshandler.state.pending_browser_requests.keys()) - before
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Our request should have been cleaned up
            for rid in new_ids:
                assert rid not in wshandler.state.pending_browser_requests
        finally:
            wshandler.state.sessions.pop("ws-cancel", None)


class TestDispatchBrowserRequestDeadSubscribers:
    async def test_all_subscribers_dead(self):
        session = wshandler.state.get_or_create_session("ws-all-dead")
        dead_sock = _mock_sock()
        dead_sock.send_json = MagicMock(side_effect=RuntimeError("ws closed"))
        session.subscribers.add(dead_sock)
        session.browser_subscribers.add(dead_sock)
        try:
            result = await session.dispatch_browser_request(
                {"action": "fetch", "url": "http://example.com"},
            )
            assert "error" in result
            assert "No browser client" in result["error"]
        finally:
            wshandler.state.sessions.pop("ws-all-dead", None)


class TestSendQueueBehavior:
    """Tests for the bounded outbound send queue (BRYAN5)."""

    async def test_slow_client_closes_connection(self, user):
        """When the send queue is full, handle_websocket drops the client."""
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})

        # Make the raw websocket.send_json block forever so the queue fills up
        send_blocked = asyncio.Event()

        async def blocking_send(data):
            send_blocked.set()
            await asyncio.sleep(3600)

        websocket.send_json = AsyncMock(side_effect=blocking_send)

        # Client sends many messages that trigger send_json responses
        msgs = [json.dumps({"cmd": "bogus"})] * (_SEND_QUEUE_SIZE + 5) + [
            WebSocketDisconnect()
        ]
        websocket.receive_text = AsyncMock(side_effect=msgs)

        # Should complete without hanging — SlowClientError triggers exit
        await asyncio.wait_for(handle_websocket(websocket), timeout=5.0)

    async def test_normal_sends_go_through_queue(self):
        """Messages sent via SafeWebSocket.send_json arrive at raw ws."""
        raw = AsyncMock()
        sw = SafeWebSocket(raw, maxsize=10)
        sw.start_sender()
        sw.send_json({"type": "hello"})
        sw.send_json({"type": "world"})
        await sw.stop_sender()
        assert raw.send_json.await_count == 2
        raw.send_json.assert_any_await({"type": "hello"})
        raw.send_json.assert_any_await({"type": "world"})

    async def test_slow_client_in_broadcast(self):
        """Broadcast drops slow subscribers instead of blocking."""
        session = wshandler.state.get_or_create_session("ws-slow-bcast")
        live_sock = _mock_sock()
        slow_sock = _mock_sock()
        slow_sock.send_json = MagicMock(side_effect=SlowClientError("full"))
        session.subscribers.add(live_sock)
        session.subscribers.add(slow_sock)
        try:
            delivered = session.broadcast({"type": "test"})
            assert delivered == 1
            assert slow_sock not in session.subscribers
            assert live_sock in session.subscribers
        finally:
            wshandler.state.sessions.pop("ws-slow-bcast", None)

    async def test_slow_client_in_terminal_forwarding(self):
        """Terminal forwarder handles SlowClientError gracefully."""
        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=SlowClientError("full"))
        t = _mock_terminal()
        conn = _base_conn(ws=sock)

        async def fake_output():
            yield "data"

        t.output = fake_output

        # Should not raise — SlowClientError is caught
        await conn.forward_terminal_output(t)

    async def test_slow_client_in_exec_forwarding(self):
        """Exec forwarder handles SlowClientError gracefully."""
        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=SlowClientError("full"))
        session = AsyncMock()

        async def fake_output():
            yield b"data"

        session.output = fake_output
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        with patch.object(container.registry, "record_activity"):
            await conn.forward_exec_output(session)
        # Should not raise


class TestChatSend:
    async def test_chat_send_broadcasts(self, workspace, user):
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        conn = _base_conn(user=user, ws=sock1)
        conn.workspace_id = workspace["id"]

        session = wshandler.state.get_or_create_session(workspace["id"])
        session.subscribers.add(sock1)
        session.subscribers.add(sock2)
        try:
            await conn.handle_chat_send({"message": "hello everyone"})
            # Both subscribers should receive the broadcast
            assert sock1.send_json.call_count == 1
            assert sock2.send_json.call_count == 1
            sent = sock1.send_json.call_args[0][0]
            assert sent["type"] == "chat_message"
            assert sent["message"] == "hello everyone"
            assert sent["user_email"] == user["email"]
            assert "id" in sent
            assert "created_at" in sent
        finally:
            wshandler.state.sessions.pop(workspace["id"], None)

    async def test_chat_send_no_workspace(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_chat_send({"message": "hello"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_chat_send_empty(self, workspace, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        await conn.handle_chat_send({"message": "   "})
        sock.send_json.assert_not_called()

    async def test_chat_history_on_connect(self, user):
        from klangk_backend import model

        workspace = await _create_workspace_with_acl(user["id"], "chat-ws")
        await model.add_chat_message(
            workspace["id"], "uid-other", "someone@test.com", "old message"
        )

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)

        async def fake_start(wid, ws_obj):
            conn.container_id = "cid"

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            await conn.handle_workspace_connect(
                {"workspaceId": workspace["id"]}
            )

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        history = [c for c in calls if c.get("type") == "chat_history"]
        assert len(history) == 1
        assert len(history[0]["messages"]) == 1
        assert history[0]["messages"][0]["message"] == "old message"


class TestChatDelete:
    async def test_chat_delete_broadcasts_update(self, user):
        from klangk_backend import model

        workspace = await ws_mod.create_workspace(user["id"], "chat-del-ws")
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"

        msg = await model.add_chat_message(
            workspace["id"], user["id"], "u@test.com", "delete me"
        )

        session = WorkspaceSession(workspace["id"])
        session.subscribers.add(sock)
        wshandler.state.sessions[workspace["id"]] = session

        await conn.handle_chat_delete({"message_id": msg["id"]})

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        updated = [c for c in calls if c.get("type") == "chat_updated"]
        assert len(updated) == 1
        assert updated[0]["message_id"] == msg["id"]
        assert updated[0]["message"] == "<message deleted by author>"

        wshandler.state.sessions.pop(workspace["id"], None)

    async def test_chat_delete_wrong_user_ignored(self, user):
        from klangk_backend import model

        workspace = await ws_mod.create_workspace(user["id"], "chat-del-ws2")
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.workspace_id = workspace["id"]

        msg = await model.add_chat_message(
            workspace["id"], "other-uid", "other@test.com", "not yours"
        )

        await conn.handle_chat_delete({"message_id": msg["id"]})
        sock.send_json.assert_not_called()

    async def test_chat_delete_no_workspace(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_chat_delete({"message_id": "x"})
        sock.send_json.assert_not_called()

    async def test_chat_delete_no_message_id(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.workspace_id = "ws"
        await conn.handle_chat_delete({})
        sock.send_json.assert_not_called()


class TestBridgeIdleTimeout:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("KLANGK_BRIDGE_TIMEOUT_SECONDS", raising=False)
        assert wshandler.bridge_idle_timeout() == 30.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("KLANGK_BRIDGE_TIMEOUT_SECONDS", "45")
        assert wshandler.bridge_idle_timeout() == 45.0

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("KLANGK_BRIDGE_TIMEOUT_SECONDS", "nope")
        assert wshandler.bridge_idle_timeout() == 30.0


class TestHandleBrowserChunk:
    def test_missing_id(self):
        wshandler.state.handle_browser_chunk({})  # no raise

    def test_unknown_id(self):
        wshandler.state.handle_browser_chunk({"id": "nope", "delta": "x"})

    def test_wrong_sender_ignored(self):
        q: asyncio.Queue = asyncio.Queue()
        expected = _mock_sock()
        imposter = _mock_sock()
        wshandler.state.streaming_browser_requests["c-1"] = (q, expected)
        try:
            wshandler.state.handle_browser_chunk(
                {"id": "c-1", "delta": "x"}, sender=imposter
            )
            assert q.empty()
        finally:
            wshandler.state.streaming_browser_requests.pop("c-1", None)

    def test_success(self):
        q: asyncio.Queue = asyncio.Queue()
        sock = _mock_sock()
        wshandler.state.streaming_browser_requests["c-2"] = (q, sock)
        try:
            wshandler.state.handle_browser_chunk(
                {"id": "c-2", "delta": "hello"}, sender=sock
            )
            assert q.get_nowait() == {"type": "chunk", "delta": "hello"}
        finally:
            wshandler.state.streaming_browser_requests.pop("c-2", None)


class TestHandleBrowserResponseStreaming:
    def test_done_enqueued(self):
        q: asyncio.Queue = asyncio.Queue()
        sock = _mock_sock()
        wshandler.state.streaming_browser_requests["d-1"] = (q, sock)
        try:
            wshandler.state.handle_browser_response(
                {"id": "d-1", "cmd": "browser_response", "text": "final"},
                sender=sock,
            )
            assert q.get_nowait() == {
                "type": "done",
                "result": {"text": "final"},
            }
        finally:
            wshandler.state.streaming_browser_requests.pop("d-1", None)

    def test_wrong_sender_ignored(self):
        q: asyncio.Queue = asyncio.Queue()
        expected = _mock_sock()
        imposter = _mock_sock()
        wshandler.state.streaming_browser_requests["d-2"] = (q, expected)
        try:
            wshandler.state.handle_browser_response(
                {"id": "d-2", "text": "x"}, sender=imposter
            )
            assert q.empty()
        finally:
            wshandler.state.streaming_browser_requests.pop("d-2", None)


class TestDispatchBrowserRequestStreamTo:
    async def test_streams_chunks_then_done(self):
        session = wshandler.state.get_or_create_session("ws-stream")
        sock = _mock_sock()
        session.subscribers.add(sock)
        session.browser_subscribers.add(sock)

        async def feed():
            await asyncio.sleep(0.05)
            for rid, (_q, _s) in list(
                wshandler.state.streaming_browser_requests.items()
            ):
                wshandler.state.handle_browser_chunk(
                    {"id": rid, "delta": "hel"}, sender=sock
                )
                wshandler.state.handle_browser_chunk(
                    {"id": rid, "delta": "lo"}, sender=sock
                )
                wshandler.state.handle_browser_response(
                    {"id": rid, "cmd": "browser_response", "text": "hello"},
                    sender=sock,
                )

        task = asyncio.create_task(feed())
        try:
            lines = [
                json.loads(line)
                async for line in session.dispatch_browser_request_stream_to(
                    sock, {"action": "soliplex_query"}, 5.0
                )
            ]
            assert lines[0] == {"type": "chunk", "delta": "hel"}
            assert lines[1] == {"type": "chunk", "delta": "lo"}
            assert lines[2]["type"] == "done"
            assert lines[2]["result"]["text"] == "hello"
            # cleaned up after the stream ends
            assert not wshandler.state.streaming_browser_requests
        finally:
            await task
            wshandler.state.sessions.pop("ws-stream", None)

    async def test_send_failure_yields_error(self):
        session = wshandler.state.get_or_create_session("ws-stream-dead")
        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=RuntimeError("dead"))
        try:
            lines = [
                json.loads(line)
                async for line in session.dispatch_browser_request_stream_to(
                    sock, {"action": "soliplex_query"}, 5.0
                )
            ]
            assert len(lines) == 1
            assert lines[0]["type"] == "error"
            assert "not available" in lines[0]["error"]
            assert not wshandler.state.streaming_browser_requests
        finally:
            wshandler.state.sessions.pop("ws-stream-dead", None)

    async def test_idle_timeout_yields_error(self):
        session = wshandler.state.get_or_create_session("ws-stream-to")
        sock = _mock_sock()
        try:
            lines = [
                json.loads(line)
                async for line in session.dispatch_browser_request_stream_to(
                    sock, {"action": "soliplex_query"}, 0.05
                )
            ]
            assert len(lines) == 1
            assert lines[0]["type"] == "error"
            assert "timeout" in lines[0]["error"].lower()
            assert not wshandler.state.streaming_browser_requests
        finally:
            wshandler.state.sessions.pop("ws-stream-to", None)

    async def test_loop_dispatches_browser_chunk(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "browser_chunk", "id": "x", "delta": "d"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            wshandler.state,
            "handle_browser_chunk",
            wraps=wshandler.state.handle_browser_chunk,
        ) as mock:
            await handle_websocket(websocket)
        mock.assert_called_once()
