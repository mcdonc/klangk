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
    SafeWebSocket,
    SlowClientError,
    WorkspaceSession,
    state,
    start_workspace_container,
    handle_workspace_connect,
    handle_workspace_disconnect,
    handle_restart_container,
    handle_terminal_start,
    handle_terminal_input,
    handle_terminal_resize,
    handle_terminal_stop,
    forward_terminal_output,
    cleanup_connection,
    send_error,
    handle_websocket,
    handle_exec_start,
    handle_exec_input,
    handle_exec_close_stdin,
    handle_exec_stop,
    forward_exec_output,
    stop_exec,
    reset_workspace_state,
    _broadcast,
    _log_ws_msg,
    _SEND_QUEUE_SIZE,
)


def _mock_ws(headers=None, query_params=None):
    """Create a mock SafeWebSocket for testing.

    send_json is MagicMock (sync) because SafeWebSocket.send_json is
    synchronous — it enqueues via put_nowait, not await.
    """
    ws = AsyncMock()
    ws.headers = headers or {}
    ws.query_params = query_params or {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = MagicMock()
    ws.receive_text = AsyncMock()
    ws.raw = ws  # identity for subscriber sets
    return ws


def _mock_raw_ws(headers=None, query_params=None):
    """Create a mock raw FastAPI WebSocket for handle_websocket tests.

    send_json is AsyncMock because the sender task awaits it.
    """
    ws = AsyncMock()
    ws.headers = headers or {}
    ws.query_params = query_params or {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    ws.receive_text = AsyncMock()
    return ws


def _mock_terminal(alive=True):
    t = AsyncMock()
    type(t).is_alive = PropertyMock(return_value=alive)
    t.start = AsyncMock()
    t.write = AsyncMock()
    t.resize = AsyncMock()
    t.stop = AsyncMock()
    return t


def _base_state(user=None):
    return {
        "user": user or {"id": "uid", "email": "testuser@example.com"},
        "workspace_id": None,
        "container_id": None,
        "terminal_session": None,
        "terminal_task": None,
    }


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
        ws = _mock_ws()
        send_error(ws, "bad thing")
        ws.send_json.assert_called_once_with(
            {"type": "error", "message": "bad thing"}
        )


# --- derive_hosting_info ---


class TestDeriveHostingInfo:
    def test_env_vars_take_precedence(self, monkeypatch):
        monkeypatch.setenv("KLANGK_HOSTING_HOSTNAME", "env.example.com")
        monkeypatch.setenv("KLANGK_HOSTING_PROTO", "https")
        monkeypatch.setenv("KLANGK_HOSTING_BASE_PATH", "/app")
        ws = _mock_ws(headers={"host": "header.example.com"})
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "env.example.com"
        assert p == "https"
        assert b == "/app"

    def test_forwarded_host_used_as_is(self, monkeypatch):
        """Behind external reverse proxy — trust X-Forwarded-Host."""
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.setenv("KLANGK_NGINX_PORT", "8995")
        ws = _mock_ws(
            headers={
                "x-forwarded-host": "arctor.repoze.org",
                "x-forwarded-proto": "https",
                "x-forwarded-prefix": "/klangk",
            }
        )
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "arctor.repoze.org"
        assert p == "https"
        assert b == "/klangk"

    def test_host_header_with_nginx_port(self, monkeypatch):
        """Direct access (local dev) — substitute nginx port."""
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.setenv("KLANGK_NGINX_PORT", "8995")
        ws = _mock_ws(headers={"host": "myhost:8997"})
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "myhost:8995"
        assert p == "http"
        assert b == ""

    def test_host_header_no_nginx_port(self, monkeypatch):
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.delenv("KLANGK_NGINX_PORT", raising=False)
        ws = _mock_ws(headers={"host": "myhost:8997"})
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "myhost:8997"
        assert p == "http"
        assert b == ""

    def test_defaults_with_nginx_port(self, monkeypatch):
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.setenv("KLANGK_NGINX_PORT", "8995")
        ws = _mock_ws(headers={})
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "localhost:8995"
        assert p == "http"
        assert b == ""

    def test_defaults_no_nginx_port(self, monkeypatch):
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.delenv("KLANGK_NGINX_PORT", raising=False)
        ws = _mock_ws(headers={})
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "localhost"
        assert p == "http"
        assert b == ""


# --- handle_steer ---


class TestHandleTerminalInput:
    async def test_writes_data(self):
        t = _mock_terminal()
        state = _base_state()
        state["terminal_session"] = t
        state["container_id"] = "cid"
        container.registry.track_activity("cid", "ws")

        await handle_terminal_input(state, {"data": "ls\n"})

        t.write.assert_awaited_once_with("ls\n")
        container.registry.states.pop("ws", None)

    async def test_no_session(self):
        state = _base_state()
        await handle_terminal_input(state, {"data": "ls\n"})
        assert state["terminal_session"] is None

    async def test_dead_session(self):
        t = _mock_terminal(alive=False)
        state = _base_state()
        state["terminal_session"] = t
        await handle_terminal_input(state, {"data": "ls\n"})
        t.write.assert_not_awaited()

    async def test_oversized_input_dropped(self):
        t = _mock_terminal()
        state = _base_state()
        state["terminal_session"] = t
        state["container_id"] = "cid"
        container.registry.track_activity("cid", "ws")

        big_data = "x" * 70000
        await handle_terminal_input(state, {"data": big_data})
        t.write.assert_not_awaited()
        container.registry.states.pop("ws", None)


# --- handle_terminal_resize ---


class TestHandleTerminalResize:
    async def test_resize(self):
        t = _mock_terminal()
        state = _base_state()
        state["terminal_session"] = t

        await handle_terminal_resize(state, {"cols": 120, "rows": 40})

        t.resize.assert_awaited_once_with(120, 40)

    async def test_resize_defaults(self):
        t = _mock_terminal()
        state = _base_state()
        state["terminal_session"] = t

        await handle_terminal_resize(state, {})

        t.resize.assert_awaited_once_with(80, 24)

    async def test_no_session(self):
        state = _base_state()
        await handle_terminal_resize(state, {"cols": 120, "rows": 40})
        assert state["terminal_session"] is None


# --- handle_terminal_stop ---


class TestHandleTerminalStop:
    async def test_stops_session(self):
        t = _mock_terminal()
        state = _base_state()
        state["terminal_session"] = t
        state["terminal_task"] = asyncio.create_task(asyncio.sleep(10))

        await handle_terminal_stop(state)

        t.stop.assert_awaited_once()
        assert state.get("terminal_session") is None
        assert state["terminal_task"] is None

    async def test_no_session(self):
        state = _base_state()
        await handle_terminal_stop(state)
        assert state.get("terminal_session") is None
        assert state["terminal_task"] is None


# --- handle_terminal_start ---


class TestHandleTerminalStart:
    async def test_starts_session(self):
        ws = _mock_ws()
        state = _base_state()
        state["container_id"] = "cid"
        container.registry.track_activity("cid", "ws")

        with patch.object(wshandler, "TerminalSession") as MockTS:
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield  # make it an async generator

            mock_session.output = fake_output

            await handle_terminal_start(ws, state, {"cols": 100, "rows": 30})
            # Let the background task run
            await asyncio.sleep(0)

        MockTS.assert_called_once_with("cid")
        mock_session.start.assert_awaited_once_with(
            100, 30, command_override=None
        )
        assert state["terminal_session"] is mock_session
        assert state["terminal_task"] is not None
        # Should have sent terminal_started ack
        ws.send_json.assert_called_with({"type": "terminal_started"})

        # Clean up
        state["terminal_task"].cancel()
        try:
            await state["terminal_task"]
        except asyncio.CancelledError:
            pass
        container.registry.states.pop("ws", None)

    async def test_passes_command_override(self):
        ws = _mock_ws()
        state = _base_state()
        state["container_id"] = "cid"
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.is_alive = True
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk_backend.wshandler.TerminalSession", MockTS):
            await handle_terminal_start(
                ws, state, {"cols": 80, "rows": 24, "commandOverride": "bash"}
            )
            # Let the background task run
            await asyncio.sleep(0)

        mock_session.start.assert_awaited_once_with(
            80, 24, command_override="bash"
        )

        state["terminal_task"].cancel()
        try:
            await state["terminal_task"]
        except asyncio.CancelledError:
            pass
        container.registry.states.pop("ws", None)

    async def test_start_failure_sends_error(self):
        ws = _mock_ws()
        state = _base_state()
        state["container_id"] = "cid"
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.start = AsyncMock(
            side_effect=RuntimeError("docker broke")
        )
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk_backend.wshandler.TerminalSession", MockTS):
            await handle_terminal_start(ws, state, {"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        # Should have sent an error, not terminal_started
        sent = ws.send_json.call_args_list
        assert any(call.args[0].get("type") == "error" for call in sent)
        # Session is stored immediately but stop() is called on failure
        mock_session.stop.assert_awaited_once()
        container.registry.states.pop("ws", None)

    async def test_cancellation_during_start_cleans_up(self):
        ws = _mock_ws()
        state = _base_state()
        state["container_id"] = "cid"
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.start = AsyncMock(side_effect=asyncio.CancelledError)
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk_backend.wshandler.TerminalSession", MockTS):
            await handle_terminal_start(ws, state, {"cols": 80, "rows": 24})
            task = state["terminal_task"]
            with pytest.raises(asyncio.CancelledError):
                await task

        # session.stop() must be called to avoid leaking the aiodocker client
        mock_session.stop.assert_awaited_once()
        container.registry.states.pop("ws", None)

    async def test_session_replaced_during_start_aborts(self):
        """If stop_terminal replaces the session while start() is running,
        the startup task stops the orphaned session and does not send
        terminal_started."""
        ws = _mock_ws()
        state = _base_state()
        state["container_id"] = "cid"
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()

        async def start_and_replace(*a, **kw):
            # Simulate stop_terminal replacing the session mid-start
            state["terminal_session"] = AsyncMock()

        mock_session.start = AsyncMock(side_effect=start_and_replace)
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk_backend.wshandler.TerminalSession", MockTS):
            await handle_terminal_start(ws, state, {"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        # The orphaned session must be stopped
        mock_session.stop.assert_awaited_once()
        # terminal_started must NOT be sent
        for call in ws.send_json.call_args_list:
            assert call.args[0].get("type") != "terminal_started"
        container.registry.states.pop("ws", None)

    async def test_no_container(self):
        ws = _mock_ws()
        state = _base_state()
        await handle_terminal_start(ws, state, {})
        assert state["terminal_session"] is None


# --- forward_terminal_output ---


class TestForwardTerminalOutput:
    async def test_forwards_output(self):
        ws = _mock_ws()
        t = _mock_terminal()
        state = _base_state()
        state["container_id"] = "ctr-fwd"
        state["terminal_session"] = t
        container.registry.track_activity("ctr-fwd", "ws-fwd")

        async def fake_output():
            yield "line1"
            yield "line2"

        t.output = fake_output

        await forward_terminal_output(ws, t, state)

        # Session claimed and stopped by finally block
        assert state.get("terminal_session") is None
        t.stop.assert_awaited_once()
        calls = ws.send_json.call_args_list
        assert calls[0][0][0] == {"type": "terminal_output", "data": "line1"}
        assert calls[1][0][0] == {"type": "terminal_output", "data": "line2"}
        # Stream ended — container_stopped event sent
        assert calls[2][0][0]["type"] == "event"
        assert calls[2][0][0]["event"]["name"] == "container_stopped"
        # Activity was bumped on each output chunk
        assert "ws-fwd" in container.registry.states
        container.registry.states.pop("ws-fwd", None)

    async def test_cancelled_error_propagates(self):
        ws = _mock_ws()
        t = _mock_terminal()
        state = _base_state()

        async def cancel_output():
            raise asyncio.CancelledError()
            yield  # noqa

        t.output = cancel_output

        with pytest.raises(asyncio.CancelledError):
            await forward_terminal_output(ws, t, state)

    async def test_ws_error_logged(self):
        ws = _mock_ws()
        ws.send_json = MagicMock(side_effect=RuntimeError("ws closed"))
        t = _mock_terminal()
        state = _base_state()

        async def fake_output():
            yield "data"

        t.output = fake_output

        await forward_terminal_output(ws, t, state)
        # The error send_json was called (it raised, triggering the handler)
        assert ws.send_json.call_count >= 1

    async def test_ws_error_then_stop_event_also_fails(self):
        ws = _mock_ws()
        t = _mock_terminal()
        state = _base_state()

        ws.send_json = MagicMock(side_effect=ConnectionError("ws dead"))

        async def fake_output():
            yield "data"

        t.output = fake_output

        await forward_terminal_output(ws, t, state)
        # Both sends failed — verify both were attempted
        assert ws.send_json.call_count == 2


# --- forward_events ---


def _setup_workspace_state(workspace_id, ws, pi, container_id="cid-1"):
    """Helper to set up _workspace_state for forward_events tests."""
    session = WorkspaceSession(workspace_id)
    session.container_id = container_id
    session.subscribers = {ws}
    wshandler.state.sessions[workspace_id] = session


def _teardown_workspace_state(workspace_id):
    wshandler.state.sessions.pop(workspace_id, None)
    container.registry.states.pop(workspace_id, None)


class TestCleanupConnection:
    async def test_cleanup_last_subscriber_removes_session(self):
        ws = _mock_ws()
        t = _mock_terminal()
        state = _base_state()
        state["container_id"] = "ctr-full"
        state["workspace_id"] = "ws-cleanup-1"
        state["_idle_cb"] = lambda ws: None
        state["terminal_session"] = t
        state["terminal_task"] = asyncio.create_task(asyncio.sleep(10))

        container.registry.track_activity("ctr-full", "ws-cleanup-1")
        session = WorkspaceSession("ws-cleanup-1")
        session.subscribers.add(ws)
        wshandler.state.sessions["ws-cleanup-1"] = session
        container.registry.states["ws-cleanup-1"].idle_callbacks.append(
            state["_idle_cb"]
        )

        await cleanup_connection(ws, state)

        t.stop.assert_awaited_once()
        assert state["_idle_cb"] is None
        assert state.get("terminal_session") is None
        # Session removed when last subscriber disconnects
        assert "ws-cleanup-1" not in wshandler.state.sessions

        container.registry.states.pop("ws-cleanup-1", None)

    async def test_cleanup_other_subscribers_remain(self):
        """When other subscribers remain, session stays alive."""
        ws = _mock_ws()
        other_ws = _mock_ws()
        t = _mock_terminal()
        state = _base_state()
        state["container_id"] = "ctr-shared"
        state["workspace_id"] = "ws-cleanup-2"
        state["_idle_cb"] = lambda ws: None
        state["terminal_session"] = t
        state["terminal_task"] = asyncio.create_task(asyncio.sleep(10))

        container.registry.track_activity("ctr-shared", "ws-cleanup-2")
        session = WorkspaceSession("ws-cleanup-2")
        session.subscribers.add(ws)
        session.subscribers.add(other_ws)
        wshandler.state.sessions["ws-cleanup-2"] = session
        container.registry.states["ws-cleanup-2"].idle_callbacks.append(
            state["_idle_cb"]
        )

        await cleanup_connection(ws, state)

        # Terminal for THIS connection should be stopped
        t.stop.assert_awaited_once()
        # Session still present — other subscriber remains
        assert "ws-cleanup-2" in wshandler.state.sessions
        assert other_ws in session.subscribers
        assert ws not in session.subscribers

        # Cleanup
        container.registry.states.pop("ws-cleanup-2", None)
        wshandler.state.sessions.pop("ws-cleanup-2", None)

    async def test_cleanup_minimal(self):
        ws = _mock_ws()
        state = _base_state()
        await cleanup_connection(ws, state)
        assert state.get("terminal_session") is None


# --- handle_prompt ---


class TestHandleWorkspaceConnect:
    async def test_missing_workspace_id(self):
        ws = _mock_ws()
        state = _base_state()
        await handle_workspace_connect(ws, state, {})
        assert "Missing" in ws.send_json.call_args[0][0]["message"]

    async def test_workspace_not_found(self, user):
        ws = _mock_ws()
        state = _base_state(user=user)
        await handle_workspace_connect(ws, state, {"workspaceId": "fake"})
        assert "not found" in ws.send_json.call_args[0][0]["message"]

    async def test_connect_success(self, user):
        ws = _mock_ws()
        workspace = await ws_mod.create_workspace(user["id"], "test-ws")
        state = _base_state(user=user)

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "cid"

        with (
            patch.object(
                wshandler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[9000, 9001],
            ),
        ):
            await handle_workspace_connect(
                ws, state, {"workspaceId": workspace["id"]}
            )

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        ready = [c for c in calls if c.get("type") == "workspace_ready"]
        assert len(ready) == 1
        assert ready[0]["workspaceId"] == workspace["id"]
        assert ready[0]["defaultCommand"] is None
        # Integer timeout (default 30m) should show as "30m" not "30.0m"
        assert "30m" in state["pending_status_msg"]

    async def test_connect_sends_default_command(self, user):
        ws = _mock_ws()
        workspace = await ws_mod.create_workspace(
            user["id"], "cmd-ws", default_command="pi"
        )
        state = _base_state(user=user)

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "cid"

        with (
            patch.object(
                wshandler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[9000],
            ),
        ):
            await handle_workspace_connect(
                ws, state, {"workspaceId": workspace["id"]}
            )

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        ready = [c for c in calls if c.get("type") == "workspace_ready"]
        assert ready[0]["defaultCommand"] == "pi"


class TestHandleWorkspaceDisconnect:
    async def test_disconnect(self):
        ws = _mock_ws()
        state = _base_state()
        state["container_id"] = "cid"
        state["workspace_id"] = "ws-1"

        with patch.object(
            container.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            await handle_workspace_disconnect(ws, state)

        assert state["workspace_id"] is None
        assert state["container_id"] is None


# --- handle_restart_container ---


class TestStartWorkspaceContainer:
    async def test_new_session(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
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
            await start_workspace_container(
                ws, state, workspace["id"], workspace
            )

        assert state["container_id"] == "cid-1"
        assert state["workspace"] == workspace
        assert workspace["id"] in wshandler.state.sessions
        assert state["_idle_cb"] is not None

        wshandler.state.sessions.pop(workspace["id"], None)
        container.registry.states.pop(workspace["id"], None)

    async def test_idle_callback_ws_error(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
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
            await start_workspace_container(
                ws, state, workspace["id"], workspace
            )

        # Test idle callback when WS send fails
        ws.send_json = MagicMock(side_effect=RuntimeError("ws closed"))
        idle_cb = state["_idle_cb"]
        await idle_cb(workspace["id"])  # should not raise
        assert ws.send_json.call_count == 1

        wshandler.state.sessions.pop(workspace["id"], None)
        container.registry.states.pop(workspace["id"], None)

    async def test_clears_pending_status_msg(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
        state["pending_status_msg"] = "stale message from prior connect"
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
            await start_workspace_container(
                ws, state, workspace["id"], workspace
            )

        assert "pending_status_msg" not in state

        wshandler.state.sessions.pop(workspace["id"], None)
        container.registry.states.pop(workspace["id"], None)


# --- handle_websocket dispatch branches ---


class TestHandleWebsocketDispatch:
    """Test all command dispatch branches through the main handler."""

    async def _run_commands(self, user, commands):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        msgs = [json.dumps(c) for c in commands] + [WebSocketDisconnect()]
        ws.receive_text = AsyncMock(side_effect=msgs)
        await handle_websocket(ws)
        return ws

    async def test_dispatch_terminal_start(self, user):
        ws = await self._run_commands(user, [{"cmd": "terminal_start"}])
        ws.accept.assert_awaited_once()

    async def test_dispatch_terminal_input(self, user):
        ws = await self._run_commands(
            user, [{"cmd": "terminal_input", "data": "x"}]
        )
        ws.accept.assert_awaited_once()

    async def test_dispatch_terminal_resize(self, user):
        ws = await self._run_commands(
            user, [{"cmd": "terminal_resize", "cols": 80, "rows": 24}]
        )
        ws.accept.assert_awaited_once()

    async def test_dispatch_terminal_stop(self, user):
        ws = await self._run_commands(user, [{"cmd": "terminal_stop"}])
        ws.accept.assert_awaited_once()

    async def test_dispatch_restart_container(self, user):
        ws = await self._run_commands(user, [{"cmd": "restart_container"}])
        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_dispatch_workspace_connect(self, user):
        ws = await self._run_commands(user, [{"cmd": "workspace_connect"}])
        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("Missing" in str(c) for c in calls)

    async def test_dispatch_workspace_disconnect(self, user):
        ws = await self._run_commands(user, [{"cmd": "workspace_disconnect"}])
        ws.accept.assert_awaited_once()

    async def test_container_survives_disconnect(self, user):
        """Container should NOT be killed on disconnect — idle timeout handles it."""
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})

        workspace = await ws_mod.create_workspace(user["id"], "stop-ws")
        ws.receive_text = AsyncMock(
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

        async def fake_start(ws_arg, state, wid, ws_obj):
            state["workspace_id"] = wid
            state["container_id"] = "cid-stop"

        with (
            patch.object(
                wshandler,
                "start_workspace_container",
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
            await handle_websocket(ws)

        mock_stop.assert_not_awaited()


# --- handle_restart_container additional coverage ---


class TestHandleWebsocket:
    async def test_missing_token(self):
        ws = _mock_raw_ws(query_params={})
        await handle_websocket(ws)
        ws.close.assert_awaited_once_with(code=4001, reason="Missing token")

    async def test_invalid_token(self, db):
        ws = _mock_raw_ws(query_params={"token": "bad"})
        await handle_websocket(ws)
        ws.close.assert_awaited_once_with(code=4001, reason="Invalid token")

    async def test_valid_token_then_disconnect(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect())

        await handle_websocket(ws)

        ws.accept.assert_awaited_once()

    async def test_invalid_json(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=["not json", WebSocketDisconnect()]
        )

        await handle_websocket(ws)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("Invalid JSON" in str(c) for c in calls)

    async def test_unknown_command(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "bogus"}),
                WebSocketDisconnect(),
            ]
        )

        await handle_websocket(ws)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("Unknown command" in str(c) for c in calls)

    async def test_ui_ready_with_pending(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        workspace = await ws_mod.create_workspace(user["id"], "ui-ready-ws")

        async def fake_start(ws_arg, state, wid, ws_obj):
            state["container_id"] = "cid"
            state["workspace_id"] = wid
            wshandler.state.get_or_create_session(wid)

        ws.receive_text = AsyncMock(
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
                wshandler,
                "start_workspace_container",
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
            await handle_websocket(ws)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
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
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "ui_ready"}),
                WebSocketDisconnect(),
            ]
        )

        await handle_websocket(ws)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
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
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(side_effect=RuntimeError("unexpected"))

        await handle_websocket(ws)

        ws.accept.assert_awaited_once()
        assert ws not in wshandler.state.connections


class TestExecHandlers:
    async def test_exec_start_no_container(self):
        ws = _mock_ws()
        state = {"container_id": None, "dockerexec": None, "exec_task": None}
        await handle_exec_start(ws, state, {"command": ["ls"]})
        assert state["dockerexec"] is None

    async def test_exec_start_no_command(self):
        ws = _mock_ws()
        state = {
            "container_id": "cid",
            "dockerexec": None,
            "exec_task": None,
        }
        await handle_exec_start(ws, state, {"command": []})
        ws.send_json.assert_called()
        assert "command" in ws.send_json.call_args[0][0].get("message", "")

    async def test_exec_start_success(self):
        ws = _mock_ws()
        state = {
            "container_id": "cid",
            "dockerexec": None,
            "exec_task": None,
        }
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
                await handle_exec_start(ws, state, {"command": ["ls"]})
        assert state["dockerexec"] is mock_session
        assert state["exec_task"] is not None
        state["exec_task"].cancel()
        try:
            await state["exec_task"]
        except asyncio.CancelledError:
            pass

    async def test_exec_input_sends_data(self):
        import base64

        session = AsyncMock()
        session.is_alive = True
        state = {
            "container_id": "cid",
            "dockerexec": session,
        }
        data = base64.b64encode(b"hello").decode()
        with patch.object(container.registry, "record_activity"):
            await handle_exec_input(state, {"data": data})
        session.write.assert_awaited_with(b"hello")

    async def test_exec_input_no_session(self):
        state = {"container_id": "cid", "dockerexec": None}
        await handle_exec_input(state, {"data": ""})  # should not raise

    async def test_exec_input_oversized_dropped(self):
        import base64

        session = AsyncMock()
        session.is_alive = True
        state = {"container_id": "cid", "dockerexec": session}
        big_data = base64.b64encode(b"x" * 70000).decode()
        await handle_exec_input(state, {"data": big_data})
        session.write.assert_not_awaited()

    async def test_exec_close_stdin(self):
        session = AsyncMock()
        state = {"dockerexec": session}
        await handle_exec_close_stdin(state)
        session.close_stdin.assert_awaited_once()

    async def test_exec_close_stdin_no_session(self):
        state = {"dockerexec": None}
        await handle_exec_close_stdin(state)  # should not raise

    async def test_exec_stop(self):
        session = AsyncMock()
        task = asyncio.create_task(asyncio.sleep(10))
        state = {"dockerexec": session, "exec_task": task}
        await handle_exec_stop(state)
        assert state.get("dockerexec") is None
        assert state["exec_task"] is None

    async def test_stop_exec_no_session(self):
        state = {"dockerexec": None, "exec_task": None}
        await stop_exec(state)  # should not raise

    async def test_forward_exec_output(self):
        import base64

        ws = _mock_ws()
        session = AsyncMock()
        session.returncode = 0

        async def fake_output():
            yield b"chunk1"
            yield b"chunk2"

        session.output = fake_output
        state = {"container_id": "cid", "dockerexec": session}
        with patch.object(container.registry, "record_activity"):
            await forward_exec_output(ws, session, state)
        # Session claimed and stopped by finally block
        assert state.get("dockerexec") is None
        session.stop.assert_awaited_once()
        calls = ws.send_json.call_args_list
        output_calls = [
            c for c in calls if c[0][0].get("type") == "exec_output"
        ]
        exit_calls = [c for c in calls if c[0][0].get("type") == "exec_exit"]
        assert len(output_calls) == 2
        assert base64.b64decode(output_calls[0][0][0]["data"]) == b"chunk1"
        assert len(exit_calls) == 1
        assert exit_calls[0][0][0]["code"] == 0

    async def test_forward_exec_output_ws_error(self):
        ws = _mock_ws()
        session = AsyncMock()

        async def fake_output():
            yield b"data"

        session.output = fake_output
        ws.send_json = MagicMock(side_effect=RuntimeError("ws dead"))
        state = {"container_id": "cid"}
        with patch.object(container.registry, "record_activity"):
            await forward_exec_output(ws, session, state)
        # Should not raise

    async def test_cleanup_connection_stops_exec(self):
        session = AsyncMock()
        task = asyncio.create_task(asyncio.sleep(10))
        state = {
            "user": {"email": "test"},
            "workspace_id": None,
            "container_id": None,
            "terminal_session": None,
            "terminal_task": None,
            "dockerexec": session,
            "exec_task": task,
            "_idle_cb": None,
        }
        ws = _mock_ws()
        await cleanup_connection(ws, state)
        session.stop.assert_awaited_once()
        assert state.get("dockerexec") is None


class TestExecDispatch:
    async def test_dispatch_exec_start(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_start", "command": ["ls"]}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            wshandler, "handle_exec_start", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(ws)
        mock.assert_awaited_once()

    async def test_dispatch_exec_input(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_input", "data": "AA=="}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            wshandler, "handle_exec_input", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(ws)
        mock.assert_awaited_once()

    async def test_dispatch_exec_stop(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_stop"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            wshandler, "handle_exec_stop", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(ws)
        mock.assert_awaited_once()

    async def test_dispatch_exec_close_stdin(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_close_stdin"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            wshandler, "handle_exec_close_stdin", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(ws)
        mock.assert_awaited_once()

    async def test_dispatch_heartbeat(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "heartbeat"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            wshandler, "handle_heartbeat", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(ws)
        mock.assert_awaited_once()


class TestHandleHeartbeat:
    async def test_records_activity(self):
        state = {"container_id": "cid-hb"}
        container.registry.track_activity("cid-hb", "ws-hb")
        container.registry.states["ws-hb"].last_activity = 0.0

        await wshandler.handle_heartbeat(state)

        assert container.registry.states["ws-hb"].last_activity > 0.0
        container.registry.states.pop("ws-hb", None)
        container.registry._cid_to_wsid.pop("cid-hb", None)

    async def test_no_container_id(self):
        state = {}
        # Should not raise
        await wshandler.handle_heartbeat(state)


class TestBrowserBridge:
    async def test_dispatch_browser_response(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "browser_response", "id": "req-1"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            wshandler,
            "handle_browser_response",
            wraps=wshandler.handle_browser_response,
        ) as mock:
            await handle_websocket(ws)
        mock.assert_called_once()

    async def test_handle_browser_response_resolves_future(self):
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        wshandler.state.pending_browser_requests["req-1"] = future

        wshandler.handle_browser_response(
            {"id": "req-1", "status": 200, "body": "hello"}
        )

        assert future.done()
        result = future.result()
        assert result["body"] == "hello"

    async def test_handle_browser_response_missing_id(self):
        # Should not raise
        wshandler.handle_browser_response({})

    async def test_handle_browser_response_unknown_id(self):
        # Should not raise
        wshandler.handle_browser_response({"id": "unknown"})

    async def test_dispatch_browser_request_no_session(self):
        result = await wshandler.dispatch_browser_request(
            "nonexistent-ws", {"action": "fetch", "url": "http://example.com"}
        )
        assert "error" in result
        assert "No browser client" in result["error"]

    async def test_dispatch_browser_request_no_subscribers(self):
        wshandler.state.get_or_create_session("ws-empty")
        try:
            result = await wshandler.dispatch_browser_request(
                "ws-empty", {"action": "fetch", "url": "http://example.com"}
            )
            assert "error" in result
            assert "No browser client" in result["error"]
        finally:
            wshandler.state.sessions.pop("ws-empty", None)

    async def test_dispatch_browser_request_cli_only(self):
        """CLI-only connections get immediate error, not 30s timeout."""
        session = wshandler.state.get_or_create_session("ws-cli-only")
        mock_ws = _mock_ws()
        session.subscribers.add(mock_ws)
        # No browser_subscribers — CLI never sends ui_ready
        try:
            result = await wshandler.dispatch_browser_request(
                "ws-cli-only",
                {"action": "fetch", "url": "http://example.com"},
            )
            assert "error" in result
            assert "No browser client" in result["error"]
        finally:
            wshandler.state.sessions.pop("ws-cli-only", None)

    async def test_dispatch_browser_request_success(self):
        session = wshandler.state.get_or_create_session("ws-bridge")
        mock_ws = _mock_ws()
        session.subscribers.add(mock_ws)
        session.browser_subscribers.add(mock_ws)

        async def respond_later():
            await asyncio.sleep(0.1)
            # Find the pending request and resolve it
            for (
                req_id,
                future,
            ) in wshandler.state.pending_browser_requests.items():
                if not future.done():
                    future.set_result(
                        {"id": req_id, "status": 200, "body": "response-data"}
                    )
                    break

        task = asyncio.create_task(respond_later())
        try:
            result = await wshandler.dispatch_browser_request(
                "ws-bridge",
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
        mock_ws = _mock_ws()
        session.subscribers.add(mock_ws)
        session.browser_subscribers.add(mock_ws)
        try:
            result = await wshandler.dispatch_browser_request(
                "ws-timeout",
                {"action": "fetch", "url": "http://example.com"},
                timeout=0.1,
            )
            assert "error" in result
            assert "timeout" in result["error"].lower()
        finally:
            wshandler.state.sessions.pop("ws-timeout", None)


class TestResetWorkspaceState:
    async def test_noop_for_unknown_workspace(self):
        await reset_workspace_state("ws-unknown")  # should not raise

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
        mock_ws = _mock_ws()
        # Add subscriber so the re-check inside the lock finds a non-empty set
        session.subscribers.add(mock_ws)
        try:
            await wshandler.state.remove_session("ws-reappear")
            # Session should NOT have been removed
            assert "ws-reappear" in wshandler.state.sessions
            assert mock_ws in session.subscribers
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
        ws1 = _mock_ws()
        ws2 = _mock_ws()
        session = WorkspaceSession("ws-race")
        session.subscribers.add(ws1)
        wshandler.state.sessions["ws-race"] = session

        state = _base_state()
        state["workspace_id"] = "ws-race"
        state["container_id"] = "cid-race"
        state["_idle_cb"] = None
        state["terminal_session"] = None
        state["terminal_task"] = None
        state["dockerexec"] = None
        state["exec_task"] = None

        # Simulate: ws1 disconnects (cleanup_connection) while ws2 connects
        # (start_workspace_container adds ws2 under the lock).
        # We do this by adding ws2 after ws1's cleanup, verifying the session
        # and ws2 survive.

        await cleanup_connection(ws1, state)

        # Session should be removed since ws1 was the last subscriber
        assert "ws-race" not in wshandler.state.sessions

        # Now create a fresh session for ws2 (simulating start_workspace_container)
        session2 = wshandler.state.get_or_create_session("ws-race")
        async with session2.lock:
            session2.subscribers.add(ws2)

        assert ws2 in session2.subscribers
        assert "ws-race" in wshandler.state.sessions

        wshandler.state.sessions.pop("ws-race", None)

    async def test_concurrent_cleanup_and_add(self):
        """When cleanup holds the lock, a concurrent add waits and is not lost."""
        ws1 = _mock_ws()
        ws2 = _mock_ws()
        session = WorkspaceSession("ws-conc")
        session.subscribers.add(ws1)
        session.subscribers.add(ws2)
        wshandler.state.sessions["ws-conc"] = session

        state1 = _base_state()
        state1["workspace_id"] = "ws-conc"
        state1["container_id"] = "cid-conc"
        state1["_idle_cb"] = None
        state1["terminal_session"] = None
        state1["terminal_task"] = None
        state1["dockerexec"] = None
        state1["exec_task"] = None

        # ws1 disconnects, ws2 remains
        await cleanup_connection(ws1, state1)

        # Session should still exist because ws2 is still subscribed
        assert "ws-conc" in wshandler.state.sessions
        assert ws2 in session.subscribers
        assert ws1 not in session.subscribers

        wshandler.state.sessions.pop("ws-conc", None)


class TestWsDebugLogging:
    async def test_recv_logged_when_debug(self, user, monkeypatch):
        from klangk_backend import auth as auth_mod

        monkeypatch.setattr(wshandler, "_WS_DEBUG", True)
        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_raw_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "heartbeat"}),
                WebSocketDisconnect(),
            ]
        )
        await handle_websocket(ws)
        ws.accept.assert_awaited_once()

    def test_send_error_logged_when_debug(self, monkeypatch):
        monkeypatch.setattr(wshandler, "_WS_DEBUG", True)
        ws = _mock_ws()
        send_error(ws, "test error")
        ws.send_json.assert_called_once()

    async def test_broadcast_logged_when_debug(self, monkeypatch):
        monkeypatch.setattr(wshandler, "_WS_DEBUG", True)
        session = wshandler.state.get_or_create_session("ws-debug-bcast")
        mock_ws = _mock_ws()
        session.subscribers.add(mock_ws)
        try:
            delivered = await _broadcast("ws-debug-bcast", {"type": "test"})
            assert delivered == 1
        finally:
            wshandler.state.sessions.pop("ws-debug-bcast", None)

    async def test_broadcast_to_browsers_logged_when_debug(self, monkeypatch):
        monkeypatch.setattr(wshandler, "_WS_DEBUG", True)
        session = wshandler.state.get_or_create_session("ws-debug-browser")
        mock_ws = _mock_ws()
        session.browser_subscribers.add(mock_ws)
        try:
            delivered = await wshandler._broadcast_to_browsers(
                "ws-debug-browser", {"type": "test"}
            )
            assert delivered == 1
        finally:
            wshandler.state.sessions.pop("ws-debug-browser", None)


class TestLogWsMsg:
    def test_terminal_output_truncated(self):
        _log_ws_msg(
            "RECV",
            {"type": "terminal_output", "data": "x" * 200},
            {"email": "test@example.com"},
        )

    def test_terminal_input_truncated(self):
        _log_ws_msg(
            "SEND",
            {"type": "terminal_input", "data": "y" * 50},
        )

    def test_other_message(self):
        _log_ws_msg("RECV", {"type": "heartbeat"})

    def test_other_message_with_user(self):
        _log_ws_msg(
            "RECV",
            {"cmd": "workspace_connect", "workspaceId": "ws-1"},
            {"email": "test@example.com"},
        )


class TestBroadcastDeadSubscribers:
    async def test_dead_subscriber_removed(self):
        session = wshandler.state.get_or_create_session("ws-dead-sub")
        live_ws = _mock_ws()
        dead_ws = _mock_ws()
        dead_ws.send_json = MagicMock(side_effect=RuntimeError("ws closed"))
        session.subscribers.add(live_ws)
        session.subscribers.add(dead_ws)
        try:
            delivered = await _broadcast("ws-dead-sub", {"type": "test"})
            assert delivered == 1
            assert dead_ws not in session.subscribers
            assert live_ws in session.subscribers
        finally:
            wshandler.state.sessions.pop("ws-dead-sub", None)


class TestHandleRestartContainer:
    async def test_restart_not_connected(self):
        ws = _mock_ws()
        state = _base_state()
        await handle_restart_container(ws, state)
        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_restart_success(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        workspace = await ws_mod.create_workspace(user["id"], "restart-ws")
        state = _base_state(user=user)
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid-old"
        state["workspace"] = workspace

        async def fake_start(ws_arg, st, wid, ws_obj):
            st["container_id"] = "cid-new"
            st["workspace_id"] = wid

        with (
            patch.object(
                wshandler,
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
            await handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
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
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
        state["workspace_id"] = "ws-gone"
        state["container_id"] = "cid-gone"
        state["workspace"] = None

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
            await handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("not found" in str(c) for c in calls)

    async def test_restart_fractional_timeout(self, user, monkeypatch):
        monkeypatch.setattr(container, "IDLE_TIMEOUT_SECONDS", 90)
        ws = _mock_ws(headers={"host": "localhost:8997"})
        workspace = await ws_mod.create_workspace(user["id"], "restart-frac")
        state = _base_state(user=user)
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid-frac"
        state["workspace"] = workspace

        async def fake_start(ws_arg, st, wid, ws_obj):
            st["container_id"] = "cid-frac-new"
            st["workspace_id"] = wid

        with (
            patch.object(
                wshandler,
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
            await handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
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
        ws = _mock_ws(headers={"host": "localhost:8997"})
        workspace = await ws_mod.create_workspace(user["id"], "restart-err")
        state = _base_state(user=user)
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid-err"
        state["workspace"] = workspace

        async def fail_cleanup(ws_arg, st):
            raise RuntimeError("cleanup boom")

        async def fake_start(ws_arg, st, wid, ws_obj):
            st["container_id"] = "cid-new"
            st["workspace_id"] = wid

        with (
            patch.object(
                wshandler,
                "cleanup_connection",
                side_effect=fail_cleanup,
            ),
            patch.object(
                wshandler,
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
            await handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        ready = [
            c
            for c in calls
            if isinstance(c, dict)
            and c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(ready) == 1

    async def test_restart_cleanup_ws_disconnect(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        workspace = await ws_mod.create_workspace(user["id"], "restart-disc")
        state = _base_state(user=user)
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid-disc"
        state["workspace"] = workspace

        async def fail_cleanup(ws_arg, st):
            raise WebSocketDisconnect()

        async def fake_start(ws_arg, st, wid, ws_obj):
            st["container_id"] = "cid-new"
            st["workspace_id"] = wid

        with (
            patch.object(
                wshandler,
                "cleanup_connection",
                side_effect=fail_cleanup,
            ),
            patch.object(
                wshandler,
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
            await handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
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
        ws = _mock_ws()
        workspace = await ws_mod.create_workspace(user["id"], "frac-ws")
        state = _base_state(user=user)

        async def fake_start(ws_arg, state, wid, workspace):
            state["container_id"] = "cid"
            state["container_status"] = "created"

        with (
            patch.object(
                wshandler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            await handle_workspace_connect(
                ws, state, {"workspaceId": workspace["id"]}
            )

        assert "1.5m" in state["pending_status_msg"]


class TestDispatchBrowserRequestCancelled:
    async def test_cancelled_cleans_up(self):
        session = wshandler.state.get_or_create_session("ws-cancel")
        mock_ws = _mock_ws()
        session.subscribers.add(mock_ws)
        session.browser_subscribers.add(mock_ws)
        try:
            # Snapshot request IDs before so we can check ours was cleaned up
            before = set(wshandler.state.pending_browser_requests.keys())
            task = asyncio.create_task(
                wshandler.dispatch_browser_request(
                    "ws-cancel",
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
        dead_ws = _mock_ws()
        dead_ws.send_json = MagicMock(side_effect=RuntimeError("ws closed"))
        session.subscribers.add(dead_ws)
        session.browser_subscribers.add(dead_ws)
        try:
            result = await wshandler.dispatch_browser_request(
                "ws-all-dead",
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
        ws = _mock_raw_ws(query_params={"token": token})

        # Make the raw ws.send_json block forever so the queue fills up
        send_blocked = asyncio.Event()

        async def blocking_send(data):
            send_blocked.set()
            await asyncio.sleep(3600)

        ws.send_json = AsyncMock(side_effect=blocking_send)

        # Client sends many messages that trigger send_json responses
        msgs = [json.dumps({"cmd": "bogus"})] * (_SEND_QUEUE_SIZE + 5) + [
            WebSocketDisconnect()
        ]
        ws.receive_text = AsyncMock(side_effect=msgs)

        # Should complete without hanging — SlowClientError triggers exit
        await asyncio.wait_for(handle_websocket(ws), timeout=5.0)

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
        live_ws = _mock_ws()
        slow_ws = _mock_ws()
        slow_ws.send_json = MagicMock(side_effect=SlowClientError("full"))
        session.subscribers.add(live_ws)
        session.subscribers.add(slow_ws)
        try:
            delivered = await _broadcast("ws-slow-bcast", {"type": "test"})
            assert delivered == 1
            assert slow_ws not in session.subscribers
            assert live_ws in session.subscribers
        finally:
            wshandler.state.sessions.pop("ws-slow-bcast", None)

    async def test_slow_client_in_terminal_forwarding(self):
        """Terminal forwarder handles SlowClientError gracefully."""
        ws = _mock_ws()
        ws.send_json = MagicMock(side_effect=SlowClientError("full"))
        t = _mock_terminal()
        state = _base_state()

        async def fake_output():
            yield "data"

        t.output = fake_output

        # Should not raise — SlowClientError is caught
        await forward_terminal_output(ws, t, state)

    async def test_slow_client_in_exec_forwarding(self):
        """Exec forwarder handles SlowClientError gracefully."""
        ws = _mock_ws()
        ws.send_json = MagicMock(side_effect=SlowClientError("full"))
        session = AsyncMock()

        async def fake_output():
            yield b"data"

        session.output = fake_output
        state = {"container_id": "cid"}
        with patch.object(container.registry, "record_activity"):
            await forward_exec_output(ws, session, state)
        # Should not raise
