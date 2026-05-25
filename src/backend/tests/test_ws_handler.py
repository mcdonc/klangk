"""Tests for ws_handler: WebSocket command dispatch, event forwarding, terminal, cleanup."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from fastapi import WebSocketDisconnect

from bark_backend import (
    ws_handler,
    container_manager,
    user_store,
    workspace_manager,
)
from bark_backend.pi_rpc_client import PiDeadError
from bark_backend.ws_handler import (
    WorkspaceSession,
    derive_hosting_info,
    start_workspace_container,
    handle_workspace_connect,
    handle_workspace_disconnect,
    handle_prompt,
    handle_steer,
    handle_follow_up,
    handle_abort,
    handle_extension_ui_response,
    handle_restart_container,
    handle_terminal_start,
    handle_terminal_input,
    handle_terminal_resize,
    handle_terminal_stop,
    forward_terminal_output,
    forward_events,
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
)


def _mock_ws(headers=None, query_params=None):
    ws = AsyncMock()
    ws.headers = headers or {}
    ws.query_params = query_params or {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.send_json = AsyncMock()
    ws.receive_text = AsyncMock()
    return ws


def _mock_pi_client(alive=True):
    pi = AsyncMock()
    type(pi).is_alive = PropertyMock(return_value=alive)
    pi.connect = AsyncMock()
    pi.prompt = AsyncMock()
    pi.steer = AsyncMock()
    pi.follow_up = AsyncMock()
    pi.abort = AsyncMock()
    pi.send_command = AsyncMock()
    pi.disconnect = AsyncMock()
    pi.detach = MagicMock()
    pi.events = MagicMock()
    return pi


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
        "pi_client": None,
        "container_id": None,
        "event_task": None,
        "agent_running": False,
        "terminal_session": None,
        "terminal_task": None,
    }


# --- send_error ---


class TestSendError:
    async def test_sends_error_json(self):
        ws = _mock_ws()
        await send_error(ws, "bad thing")
        ws.send_json.assert_awaited_once_with(
            {"type": "error", "message": "bad thing"}
        )


# --- derive_hosting_info ---


class TestDeriveHostingInfo:
    def test_env_vars_take_precedence(self, monkeypatch):
        monkeypatch.setenv("BARK_HOSTING_HOSTNAME", "env.example.com")
        monkeypatch.setenv("BARK_HOSTING_PROTO", "https")
        monkeypatch.setenv("BARK_HOSTING_BASE_PATH", "/app")
        ws = _mock_ws(headers={"host": "header.example.com"})
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "env.example.com"
        assert p == "https"
        assert b == "/app"

    def test_forwarded_host_used_as_is(self, monkeypatch):
        """Behind external reverse proxy — trust X-Forwarded-Host."""
        monkeypatch.delenv("BARK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("BARK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("BARK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.setenv("BARK_NGINX_PORT", "8995")
        ws = _mock_ws(
            headers={
                "x-forwarded-host": "arctor.repoze.org",
                "x-forwarded-proto": "https",
                "x-forwarded-prefix": "/bark",
            }
        )
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "arctor.repoze.org"
        assert p == "https"
        assert b == "/bark"

    def test_host_header_with_nginx_port(self, monkeypatch):
        """Direct access (local dev) — substitute nginx port."""
        monkeypatch.delenv("BARK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("BARK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("BARK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.setenv("BARK_NGINX_PORT", "8995")
        ws = _mock_ws(headers={"host": "myhost:8997"})
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "myhost:8995"
        assert p == "http"
        assert b == ""

    def test_host_header_no_nginx_port(self, monkeypatch):
        monkeypatch.delenv("BARK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("BARK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("BARK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.delenv("BARK_NGINX_PORT", raising=False)
        ws = _mock_ws(headers={"host": "myhost:8997"})
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "myhost:8997"
        assert p == "http"
        assert b == ""

    def test_defaults_with_nginx_port(self, monkeypatch):
        monkeypatch.delenv("BARK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("BARK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("BARK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.setenv("BARK_NGINX_PORT", "8995")
        ws = _mock_ws(headers={})
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "localhost:8995"
        assert p == "http"
        assert b == ""

    def test_defaults_no_nginx_port(self, monkeypatch):
        monkeypatch.delenv("BARK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("BARK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("BARK_HOSTING_BASE_PATH", raising=False)
        monkeypatch.delenv("BARK_NGINX_PORT", raising=False)
        ws = _mock_ws(headers={})
        h, p, b = derive_hosting_info(ws.headers)
        assert h == "localhost"
        assert p == "http"
        assert b == ""


# --- handle_steer ---


class TestHandleSteer:
    async def test_steer(self):
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi
        state["container_id"] = "cid"
        container_manager.registry.track_activity("cid", "ws")

        await handle_steer(state, {"text": "go left"})

        pi.steer.assert_awaited_once_with("go left")
        container_manager.registry.states.pop("ws", None)

    async def test_steer_no_client(self):
        state = _base_state()
        await handle_steer(state, {"text": "go left"})
        assert state["pi_client"] is None

    async def test_steer_pi_dead(self):
        pi = _mock_pi_client()
        pi.steer = AsyncMock(side_effect=PiDeadError("dead"))
        state = _base_state()
        state["pi_client"] = pi
        state["container_id"] = "cid"
        container_manager.registry.track_activity("cid", "ws")
        await handle_steer(state, {"text": "go left"})  # should not raise
        container_manager.registry.states.pop("ws", None)


# --- handle_follow_up ---


class TestHandleFollowUp:
    async def test_follow_up(self):
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi
        state["container_id"] = "cid"
        container_manager.registry.track_activity("cid", "ws")

        await handle_follow_up(state, {"text": "and then?"})

        pi.follow_up.assert_awaited_once_with("and then?")
        container_manager.registry.states.pop("ws", None)

    async def test_follow_up_no_client(self):
        state = _base_state()
        await handle_follow_up(state, {"text": "and then?"})
        assert state["pi_client"] is None

    async def test_follow_up_pi_dead(self):
        pi = _mock_pi_client()
        pi.follow_up = AsyncMock(side_effect=PiDeadError("dead"))
        state = _base_state()
        state["pi_client"] = pi
        state["container_id"] = "cid"
        container_manager.registry.track_activity("cid", "ws")
        await handle_follow_up(state, {"text": "x"})  # should not raise
        container_manager.registry.states.pop("ws", None)


# --- handle_abort ---


class TestHandleAbort:
    async def test_abort(self):
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi
        await handle_abort(state)
        pi.abort.assert_awaited_once()

    async def test_abort_no_client(self):
        state = _base_state()
        await handle_abort(state)
        assert state["pi_client"] is None

    async def test_abort_pi_dead(self):
        pi = _mock_pi_client()
        pi.abort = AsyncMock(side_effect=PiDeadError("dead"))
        state = _base_state()
        state["pi_client"] = pi
        await handle_abort(state)  # should not raise


# --- handle_extension_ui_response ---


class TestHandleExtensionUiResponse:
    async def test_forwards_value(self):
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi

        await handle_extension_ui_response(
            state,
            {
                "id": "ext-1",
                "value": "result text",
            },
        )
        cmd = pi.send_command.call_args[0][0]
        assert cmd["type"] == "extension_ui_response"
        assert cmd["id"] == "ext-1"
        assert cmd["value"] == "result text"

    async def test_forwards_cancelled(self):
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi

        await handle_extension_ui_response(
            state,
            {
                "id": "ext-1",
                "cancelled": True,
            },
        )
        cmd = pi.send_command.call_args[0][0]
        assert cmd["cancelled"] is True

    async def test_forwards_confirmed(self):
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi

        await handle_extension_ui_response(
            state,
            {
                "id": "ext-1",
                "confirmed": True,
            },
        )
        cmd = pi.send_command.call_args[0][0]
        assert cmd["confirmed"] is True

    async def test_no_client(self):
        state = _base_state()
        await handle_extension_ui_response(state, {"id": "ext-1"})
        assert state["pi_client"] is None

    async def test_pi_dead(self):
        pi = _mock_pi_client()
        pi.send_command = AsyncMock(side_effect=PiDeadError("dead"))
        state = _base_state()
        state["pi_client"] = pi
        await handle_extension_ui_response(
            state, {"id": "ext-1", "value": "x"}
        )  # should not raise


# --- handle_terminal_input ---


class TestHandleTerminalInput:
    async def test_writes_data(self):
        t = _mock_terminal()
        state = _base_state()
        state["terminal_session"] = t
        state["container_id"] = "cid"
        container_manager.registry.track_activity("cid", "ws")

        await handle_terminal_input(state, {"data": "ls\n"})

        t.write.assert_awaited_once_with("ls\n")
        container_manager.registry.states.pop("ws", None)

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
        assert state["terminal_session"] is None
        assert state["terminal_task"] is None

    async def test_no_session(self):
        state = _base_state()
        await handle_terminal_stop(state)
        assert state["terminal_session"] is None
        assert state["terminal_task"] is None


# --- handle_terminal_start ---


class TestHandleTerminalStart:
    async def test_starts_session(self):
        ws = _mock_ws()
        state = _base_state()
        state["container_id"] = "cid"
        container_manager.registry.track_activity("cid", "ws")

        with patch.object(ws_handler, "TerminalSession") as MockTS:
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield  # make it an async generator

            mock_session.output = fake_output

            await handle_terminal_start(ws, state, {"cols": 100, "rows": 30})

        MockTS.assert_called_once_with("cid")
        mock_session.start.assert_awaited_once_with(100, 30)
        assert state["terminal_session"] is mock_session
        assert state["terminal_task"] is not None

        # Clean up
        state["terminal_task"].cancel()
        try:
            await state["terminal_task"]
        except asyncio.CancelledError:
            pass
        container_manager.registry.states.pop("ws", None)

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
        container_manager.registry.track_activity("ctr-fwd", "ws-fwd")

        async def fake_output():
            yield "line1"
            yield "line2"

        t.output = fake_output

        await forward_terminal_output(ws, t, state)

        calls = ws.send_json.call_args_list
        assert calls[0][0][0] == {"type": "terminal_output", "data": "line1"}
        assert calls[1][0][0] == {"type": "terminal_output", "data": "line2"}
        # Stream ended — container_stopped event sent
        assert calls[2][0][0]["type"] == "event"
        assert calls[2][0][0]["event"]["name"] == "container_stopped"
        # Activity was bumped on each output chunk
        assert "ws-fwd" in container_manager.registry.states
        container_manager.registry.states.pop("ws-fwd", None)

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
        ws.send_json = AsyncMock(side_effect=RuntimeError("ws closed"))
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

        ws.send_json = AsyncMock(side_effect=ConnectionError("ws dead"))

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
    session.pi_client = pi
    session.container_id = container_id
    session.subscribers = {ws}
    ws_handler._sessions[workspace_id] = session


def _teardown_workspace_state(workspace_id):
    ws_handler._sessions.pop(workspace_id, None)
    container_manager.registry.states.pop(workspace_id, None)


class TestForwardEvents:
    async def test_forwards_pi_events(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        _setup_workspace_state(workspace["id"], ws, pi)

        events = [
            {"type": "agent_start"},
            {"type": "message_start", "message": {"id": "m1"}},
            {
                "type": "message_update",
                "message": {"id": "m1"},
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "hello",
                },
            },
            {"type": "message_end", "message": {"id": "m1"}},
            {"type": "agent_end"},
        ]

        async def fake_events():
            for e in events:
                yield e

        pi.events = fake_events
        await forward_events(pi, workspace["id"])

        assert ws.send_json.call_count >= 5
        session = ws_handler._sessions.get(workspace["id"])
        assert session is None or session.agent_running is False
        _teardown_workspace_state(workspace["id"])

    async def test_saves_assistant_text(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        _setup_workspace_state(workspace["id"], ws, pi)

        events = [
            {"type": "message_start", "message": {"id": "m1"}},
            {
                "type": "message_update",
                "message": {"id": "m1"},
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "hello world",
                },
            },
            {"type": "message_end", "message": {"id": "m1"}},
        ]

        async def fake_events():
            for e in events:
                yield e

        pi.events = fake_events
        await forward_events(pi, workspace["id"])

        msgs = await user_store.get_messages(workspace["id"])
        assert any(m["content"] == "hello world" for m in msgs)
        _teardown_workspace_state(workspace["id"])

    async def test_saves_tool_call(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        _setup_workspace_state(workspace["id"], ws, pi)

        events = [
            {
                "type": "tool_execution_start",
                "toolCallId": "tc1",
                "toolName": "bash",
                "args": {"command": "ls"},
            },
            {
                "type": "tool_execution_end",
                "toolCallId": "tc1",
                "toolName": "bash",
                "result": {"content": [{"type": "text", "text": "file.txt"}]},
            },
        ]

        async def fake_events():
            for e in events:
                yield e

        pi.events = fake_events
        await forward_events(pi, workspace["id"])

        msgs = await user_store.get_messages(workspace["id"])
        tool_msgs = [m for m in msgs if m["entry_type"] == "tool_call"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "bash"
        _teardown_workspace_state(workspace["id"])

    async def test_saves_error(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        _setup_workspace_state(workspace["id"], ws, pi)

        events = [{"type": "error", "message": "something broke", "code": 500}]

        async def fake_events():
            for e in events:
                yield e

        pi.events = fake_events
        await forward_events(pi, workspace["id"])

        msgs = await user_store.get_messages(workspace["id"])
        assert any(m["entry_type"] == "error" for m in msgs)
        _teardown_workspace_state(workspace["id"])

    async def test_tracks_agent_running(self):
        ws = _mock_ws()
        pi = _mock_pi_client()
        _setup_workspace_state("ws-track", ws, pi)

        async def fake_events():
            yield {"type": "agent_start"}
            yield {"type": "agent_end", "messages": []}

        pi.events = fake_events
        await forward_events(pi, "ws-track")

        session = ws_handler._sessions.get("ws-track")
        assert session is None or session.agent_running is False
        _teardown_workspace_state("ws-track")

    async def test_records_activity_on_events(self):
        ws = _mock_ws()
        pi = _mock_pi_client()
        _setup_workspace_state("ws-activity", ws, pi, container_id="cid-act")
        container_manager.registry.track_activity("cid-act", "ws-activity")
        container_manager.registry.states["ws-activity"].last_activity = 0

        async def fake_events():
            yield {"type": "agent_start"}

        pi.events = fake_events
        await forward_events(pi, "ws-activity")

        assert (
            container_manager.registry.states["ws-activity"].last_activity > 0
        )
        _teardown_workspace_state("ws-activity")

    async def test_ws_error_removes_dead_subscriber(self):
        ws = _mock_ws()
        ws.send_json = AsyncMock(side_effect=RuntimeError("ws closed"))
        pi = _mock_pi_client()
        _setup_workspace_state("ws-dead", ws, pi)

        async def fake_events():
            yield {"type": "agent_start"}

        pi.events = fake_events
        await forward_events(pi, "ws-dead")

        # Dead subscriber should be removed
        session = ws_handler._sessions.get("ws-dead")
        assert session is None or ws not in session.subscribers
        _teardown_workspace_state("ws-dead")

    async def test_broadcasts_to_multiple_subscribers(self):
        ws1 = _mock_ws()
        ws2 = _mock_ws()
        pi = _mock_pi_client()
        _setup_workspace_state("ws-multi", ws1, pi)
        ws_handler._sessions["ws-multi"].subscribers.add(ws2)

        async def fake_events():
            yield {"type": "agent_start"}

        pi.events = fake_events
        await forward_events(pi, "ws-multi")

        # Both subscribers should have received the event
        assert ws1.send_json.call_count >= 1
        assert ws2.send_json.call_count >= 1
        _teardown_workspace_state("ws-multi")


# --- cleanup_connection ---


class TestCleanupConnection:
    async def test_cleanup_full_last_connection(self):
        ws = _mock_ws()
        pi = _mock_pi_client()
        t = _mock_terminal()
        state = _base_state()
        state["pi_client"] = pi
        state["container_id"] = "ctr-full"
        state["workspace_id"] = "ws-cleanup-1"
        state["_idle_cb"] = lambda ws: None
        state["terminal_session"] = t
        state["terminal_task"] = asyncio.create_task(asyncio.sleep(10))

        # Simulate: one connection, shared Pi state
        container_manager.registry.track_activity("ctr-full", "ws-cleanup-1")
        container_manager.registry.add_connection("ws-cleanup-1")
        session = WorkspaceSession("ws-cleanup-1")
        session.pi_client = pi
        session.event_task = asyncio.create_task(asyncio.sleep(10))
        ws_handler._sessions["ws-cleanup-1"] = session
        container_manager.registry.states[
            "ws-cleanup-1"
        ].idle_callbacks.append(state["_idle_cb"])

        with patch.object(
            container_manager.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ) as mock_stop:
            await cleanup_connection(ws, state)

        pi.disconnect.assert_awaited_once()
        t.stop.assert_awaited_once()
        mock_stop.assert_awaited_once_with("ctr-full")
        assert state["_idle_cb"] is None
        assert state["terminal_session"] is None
        assert "ws-cleanup-1" not in ws_handler._sessions

        container_manager.registry.states.pop("ws-cleanup-1", None)

    async def test_cleanup_not_last_connection(self):
        """When other connections remain, Pi and container survive."""
        ws = _mock_ws()
        pi = _mock_pi_client()
        t = _mock_terminal()
        state = _base_state()
        state["pi_client"] = pi
        state["container_id"] = "ctr-shared"
        state["workspace_id"] = "ws-cleanup-2"
        state["_idle_cb"] = lambda ws: None
        state["terminal_session"] = t
        state["terminal_task"] = asyncio.create_task(asyncio.sleep(10))

        # Two connections
        container_manager.registry.track_activity("ctr-shared", "ws-cleanup-2")
        container_manager.registry.add_connection("ws-cleanup-2")
        container_manager.registry.add_connection("ws-cleanup-2")
        session = WorkspaceSession("ws-cleanup-2")
        session.pi_client = pi
        session.event_task = asyncio.create_task(asyncio.sleep(10))
        ws_handler._sessions["ws-cleanup-2"] = session
        container_manager.registry.states[
            "ws-cleanup-2"
        ].idle_callbacks.append(state["_idle_cb"])

        with patch.object(
            container_manager.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ) as mock_stop:
            await cleanup_connection(ws, state)

        # Pi should NOT be disconnected — other connection still using it
        pi.disconnect.assert_not_awaited()
        mock_stop.assert_not_awaited()
        # Terminal for THIS connection should be stopped
        t.stop.assert_awaited_once()
        # Shared state still present
        assert "ws-cleanup-2" in ws_handler._sessions

        # Cleanup
        container_manager.registry.states.pop("ws-cleanup-2", None)
        session = ws_handler._sessions.pop("ws-cleanup-2", None)
        if session and session.event_task:
            session.event_task.cancel()

    async def test_cleanup_minimal(self):
        ws = _mock_ws()
        state = _base_state()
        await cleanup_connection(ws, state)
        assert state["terminal_session"] is None

    async def test_cleanup_stops_container_last_conn(self):
        ws = _mock_ws()
        state = _base_state()
        state["container_id"] = "ctr-1"
        state["workspace_id"] = "ws-cleanup-3"
        container_manager.registry.add_connection("ws-cleanup-3")
        with patch.object(
            container_manager.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ) as mock_stop:
            await cleanup_connection(ws, state)
        mock_stop.assert_awaited_once_with("ctr-1")
        container_manager.registry.states.pop("ws-cleanup-3", None)


# --- handle_prompt ---


class TestHandlePrompt:
    async def test_empty_prompt(self):
        ws = _mock_ws()
        state = _base_state()
        await handle_prompt(ws, state, {"text": ""})
        ws.send_json.assert_awaited_once()
        assert "Empty prompt" in ws.send_json.call_args[0][0]["message"]

    async def test_no_workspace(self):
        ws = _mock_ws()
        state = _base_state()
        await handle_prompt(ws, state, {"text": "hello"})
        assert "Not connected" in ws.send_json.call_args[0][0]["message"]

    async def test_prompt_success(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)
        state["pi_client"] = pi
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid"
        container_manager.registry.track_activity("cid", workspace["id"])

        await handle_prompt(ws, state, {"text": "hello world"})

        pi.prompt.assert_awaited_once_with("hello world")
        msgs = await user_store.get_messages(workspace["id"])
        assert any(m["content"] == "hello world" for m in msgs)
        container_manager.registry.states.pop(workspace["id"], None)

    async def test_prompt_queued(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)
        state["pi_client"] = pi
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid"
        session = WorkspaceSession(workspace["id"])
        session.agent_running = True
        ws_handler._sessions[workspace["id"]] = session
        container_manager.registry.track_activity("cid", workspace["id"])

        await handle_prompt(ws, state, {"text": "queued msg"})

        pi.follow_up.assert_awaited_once_with("queued msg")
        calls = [c[0][0] for c in ws.send_json.call_args_list]
        queued_events = [
            c
            for c in calls
            if c.get("type") == "event"
            and c.get("event", {}).get("name") == "prompt_queued"
        ]
        assert len(queued_events) == 1
        container_manager.registry.states.pop(workspace["id"], None)
        ws_handler._sessions.pop(workspace["id"], None)

    async def test_prompt_auto_restart(self, db):
        ws = _mock_ws()
        pi_new = _mock_pi_client(alive=True)
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)
        state["pi_client"] = None  # dead
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid"
        state["workspace"] = {
            "id": workspace["id"],
            "name": "ws",
            "user_id": user["id"],
            "container_id": "cid",
            "num_ports": 5,
        }

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "new-cid"
            state["pi_client"] = pi_new

        with (
            patch.object(
                ws_handler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            container_manager.registry.track_activity(
                "new-cid", workspace["id"]
            )
            await handle_prompt(ws, state, {"text": "hello after restart"})
            await state["_retry_task"]

        pi_new.prompt.assert_awaited_once_with("hello after restart")
        container_manager.registry.states.pop(workspace["id"], None)

    async def test_prompt_auto_restart_workspace_gone(self, db):
        ws = _mock_ws()
        state = _base_state()
        state["pi_client"] = None
        state["workspace_id"] = "ws-gone"
        state["container_id"] = "cid"
        state["workspace"] = None

        await handle_prompt(ws, state, {"text": "hello"})

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("Workspace not found" in str(c) for c in calls)

    async def test_prompt_auto_restart_pi_still_dead(self, db):
        ws = _mock_ws()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)
        state["pi_client"] = None
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid"
        state["workspace"] = {
            "id": workspace["id"],
            "name": "ws",
            "user_id": user["id"],
            "container_id": "cid",
            "num_ports": 5,
        }

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "new-cid"
            state["pi_client"] = _mock_pi_client(alive=False)

        with (
            patch.object(
                ws_handler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await handle_prompt(ws, state, {"text": "hello"})
            await state["_retry_task"]

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("Failed to restart" in str(c) for c in calls)

    async def test_prompt_restart_cleanup_error(self, db):
        ws = _mock_ws()
        pi_new = _mock_pi_client(alive=True)
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)
        state["pi_client"] = None
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid"
        state["workspace"] = {
            "id": workspace["id"],
            "name": "ws",
            "user_id": user["id"],
            "container_id": "cid",
            "num_ports": 5,
        }

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "new-cid"
            state["pi_client"] = pi_new

        with (
            patch.object(
                ws_handler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                ws_handler,
                "cleanup_connection",
                side_effect=RuntimeError("cleanup boom"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            container_manager.registry.track_activity(
                "new-cid", workspace["id"]
            )
            await handle_prompt(ws, state, {"text": "hello"})
            await state["_retry_task"]

        pi_new.prompt.assert_awaited_once()
        container_manager.registry.states.pop(workspace["id"], None)

    async def test_prompt_restart_ws_send_fails(self, db):
        ws = _mock_ws()
        pi_new = _mock_pi_client(alive=True)
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)
        state["pi_client"] = None
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid"
        state["workspace"] = {
            "id": workspace["id"],
            "name": "ws",
            "user_id": user["id"],
            "container_id": "cid",
            "num_ports": 5,
        }

        call_count = 0
        original_send = ws.send_json

        async def send_fails_first(data):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("ws dead")
            return await original_send(data)

        ws.send_json = send_fails_first

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "new-cid"
            state["pi_client"] = pi_new

        with (
            patch.object(
                ws_handler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            container_manager.registry.track_activity(
                "new-cid", workspace["id"]
            )
            await handle_prompt(ws, state, {"text": "hello"})
            await state["_retry_task"]

        pi_new.prompt.assert_awaited_once()
        container_manager.registry.states.pop(workspace["id"], None)


# --- handle_workspace_connect ---


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
        workspace = await workspace_manager.create_workspace(
            user["id"], "test-ws"
        )
        state = _base_state(user=user)

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "cid"
            state["pi_client"] = _mock_pi_client()

        with (
            patch.object(
                ws_handler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container_manager.registry,
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
        # Integer timeout (default 30m) should show as "30m" not "30.0m"
        assert "30m" in state["pending_status_msg"]

    async def test_connect_fractional_timeout(self, user):
        ws = _mock_ws()
        workspace = await workspace_manager.create_workspace(
            user["id"], "frac-ws"
        )
        state = _base_state(user=user)

        original_timeout = container_manager.IDLE_TIMEOUT_SECONDS
        container_manager.IDLE_TIMEOUT_SECONDS = 90  # 1.5 minutes

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "cid"
            state["pi_client"] = _mock_pi_client()
            state["resume_session"] = "/some/session.jsonl"

        try:
            with (
                patch.object(
                    ws_handler,
                    "start_workspace_container",
                    side_effect=fake_start,
                ),
                patch.object(
                    container_manager.registry,
                    "get_workspace_ports",
                    return_value=[],
                ),
            ):
                await handle_workspace_connect(
                    ws, state, {"workspaceId": workspace["id"]}
                )

            assert "1.5m" in state["pending_status_msg"]
            assert "session resumed" in state["pending_status_msg"]
        finally:
            container_manager.IDLE_TIMEOUT_SECONDS = original_timeout


# --- handle_workspace_disconnect ---


class TestHandleWorkspaceDisconnect:
    async def test_disconnect(self):
        ws = _mock_ws()
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi
        state["container_id"] = "cid"
        state["workspace_id"] = "ws-1"

        with patch.object(
            container_manager.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            await handle_workspace_disconnect(ws, state)

        assert state["workspace_id"] is None
        assert state["container_id"] is None
        assert state["pi_client"] is None


# --- handle_restart_container ---


class TestHandleRestartContainer:
    async def test_no_workspace(self):
        ws = _mock_ws()
        state = _base_state()
        await handle_restart_container(ws, state)
        assert "Not connected" in ws.send_json.call_args[0][0]["message"]

    async def test_workspace_gone(self, user):
        ws = _mock_ws()
        state = _base_state(user=user)
        state["workspace_id"] = "gone-ws"
        state["workspace"] = None

        await handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("not found" in str(c) for c in calls)

    async def test_restart_success(self, user):
        ws = _mock_ws()
        workspace = await workspace_manager.create_workspace(
            user["id"], "restart-ws"
        )
        state = _base_state(user=user)
        state["workspace_id"] = workspace["id"]
        state["workspace"] = workspace

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "new-cid"
            state["pi_client"] = _mock_pi_client()

        container_manager.registry.track_activity("new-cid", workspace["id"])

        with (
            patch.object(
                ws_handler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container_manager.registry,
                "get_workspace_ports",
                return_value=[9000],
            ),
        ):
            await handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        ready = [
            c
            for c in calls
            if c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(ready) == 1
        container_manager.registry.states.pop(workspace["id"], None)


# --- start_workspace_container ---


class TestStartWorkspaceContainer:
    async def test_new_session(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
        workspace = await workspace_manager.create_workspace(
            user["id"], "start-ws"
        )
        pi = _mock_pi_client()

        async def fake_events():
            return
            yield

        pi.events = fake_events

        async def fake_start(*a, **kw):
            container_manager.registry.track_activity("cid-1", workspace["id"])
            return ("cid-1", "created")

        with (
            patch.object(
                container_manager.registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch.object(ws_handler, "PiRpcClient", return_value=pi),
            patch("glob.glob", return_value=[]),
        ):
            await start_workspace_container(
                ws, state, workspace["id"], workspace
            )

        assert state["container_id"] == "cid-1"
        assert state["pi_client"] is pi
        assert state["workspace"] == workspace
        assert state["resume_session"] is None
        assert workspace["id"] in ws_handler._sessions
        assert state["_idle_cb"] is not None

        session = ws_handler._sessions.pop(workspace["id"], None)
        if session and session.event_task:
            session.event_task.cancel()
            try:
                await session.event_task
            except asyncio.CancelledError:
                pass
        container_manager.registry.states.pop(workspace["id"], None)

    async def test_second_connection_shares_pi(self, user):
        ws1 = _mock_ws(headers={"host": "localhost:8997"})
        ws2 = _mock_ws(headers={"host": "localhost:8997"})
        state1 = _base_state(user=user)
        state2 = _base_state(user=user)
        workspace = await workspace_manager.create_workspace(
            user["id"], "shared-ws"
        )
        pi = _mock_pi_client()

        async def fake_events():
            return
            yield

        pi.events = fake_events

        async def fake_start_created(*a, **kw):
            container_manager.registry.track_activity(
                "cid-shared", workspace["id"]
            )
            return ("cid-shared", "created")

        async def fake_start_connected(*a, **kw):
            container_manager.registry.track_activity(
                "cid-shared", workspace["id"]
            )
            return ("cid-shared", "connected")

        with (
            patch.object(
                container_manager.registry,
                "start_container",
                side_effect=fake_start_created,
            ),
            patch.object(ws_handler, "PiRpcClient", return_value=pi),
            patch("glob.glob", return_value=[]),
        ):
            await start_workspace_container(
                ws1, state1, workspace["id"], workspace
            )

        # First connection owns Pi
        assert state1["pi_client"] is pi
        assert workspace["id"] in ws_handler._sessions

        with (
            patch.object(
                container_manager.registry,
                "start_container",
                side_effect=fake_start_connected,
            ),
            patch("glob.glob", return_value=[]),
        ):
            await start_workspace_container(
                ws2, state2, workspace["id"], workspace
            )

        # Second connection shares the same Pi client
        assert state2["pi_client"] is pi
        assert (
            container_manager.registry.connection_count(workspace["id"]) == 2
        )

        # Cleanup
        session = ws_handler._sessions.pop(workspace["id"], None)
        if session and session.event_task:
            session.event_task.cancel()
            try:
                await session.event_task
            except asyncio.CancelledError:
                pass
        container_manager.registry.states.pop(workspace["id"], None)

    async def test_resume_session(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
        workspace = await workspace_manager.create_workspace(
            user["id"], "resume-ws"
        )
        pi = _mock_pi_client()

        async def fake_events():
            return
            yield

        pi.events = fake_events

        home_path = str(
            workspace_manager.get_home_host_path(user["id"], workspace["id"])
        )

        async def fake_start(*a, **kw):
            container_manager.registry.track_activity("cid-2", workspace["id"])
            return ("cid-2", "connected")

        with (
            patch.object(
                container_manager.registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch.object(ws_handler, "PiRpcClient", return_value=pi),
            patch(
                "glob.glob",
                return_value=[f"{home_path}/.pi/sessions/session.jsonl"],
            ),
        ):
            await start_workspace_container(
                ws, state, workspace["id"], workspace
            )

        assert state["resume_session"] is not None
        assert "/home/bark/.pi/sessions" in state["resume_session"]
        assert state["container_status"] == "connected"

        session = ws_handler._sessions.pop(workspace["id"], None)
        if session and session.event_task:
            session.event_task.cancel()
            try:
                await session.event_task
            except asyncio.CancelledError:
                pass
        container_manager.registry.states.pop(workspace["id"], None)

    async def test_idle_callback_ws_error(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
        workspace = await workspace_manager.create_workspace(
            user["id"], "idle-ws"
        )
        pi = _mock_pi_client()

        async def fake_events():
            return
            yield

        pi.events = fake_events

        async def fake_start(*a, **kw):
            container_manager.registry.track_activity("cid-3", workspace["id"])
            return ("cid-3", "created")

        with (
            patch.object(
                container_manager.registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch.object(ws_handler, "PiRpcClient", return_value=pi),
            patch("glob.glob", return_value=[]),
        ):
            await start_workspace_container(
                ws, state, workspace["id"], workspace
            )

        # Test idle callback when WS send fails
        ws.send_json = AsyncMock(side_effect=RuntimeError("ws closed"))
        idle_cb = state["_idle_cb"]
        await idle_cb(workspace["id"])  # should not raise
        assert ws.send_json.call_count == 1

        session = ws_handler._sessions.pop(workspace["id"], None)
        if session and session.event_task:
            session.event_task.cancel()
            try:
                await session.event_task
            except asyncio.CancelledError:
                pass
        container_manager.registry.states.pop(workspace["id"], None)


# --- handle_websocket dispatch branches ---


class TestHandleWebsocketDispatch:
    """Test all command dispatch branches through the main handler."""

    async def _run_commands(self, user, commands):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        msgs = [json.dumps(c) for c in commands] + [WebSocketDisconnect()]
        ws.receive_text = AsyncMock(side_effect=msgs)
        await handle_websocket(ws)
        return ws

    async def test_dispatch_steer(self, user):
        ws = await self._run_commands(user, [{"cmd": "steer", "text": "left"}])
        ws.accept.assert_awaited_once()

    async def test_dispatch_follow_up(self, user):
        ws = await self._run_commands(
            user, [{"cmd": "follow_up", "text": "more"}]
        )
        ws.accept.assert_awaited_once()

    async def test_dispatch_abort(self, user):
        ws = await self._run_commands(user, [{"cmd": "abort"}])
        ws.accept.assert_awaited_once()

    async def test_dispatch_extension_ui_response(self, user):
        ws = await self._run_commands(
            user, [{"cmd": "extension_ui_response", "id": "e1"}]
        )
        ws.accept.assert_awaited_once()

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

    async def test_dispatch_prompt(self, user):
        ws = await self._run_commands(user, [{"cmd": "prompt", "text": "hi"}])
        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_container_stopped_on_disconnect(self, user):
        """Container should be stopped and removed on disconnect."""
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})

        workspace = await workspace_manager.create_workspace(
            user["id"], "stop-ws"
        )
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
            state["pi_client"] = _mock_pi_client()
            container_manager.registry.add_connection(wid)

        with (
            patch.object(
                ws_handler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container_manager.registry,
                "get_workspace_ports",
                return_value=[],
            ),
            patch.object(
                container_manager.registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ) as mock_stop,
        ):
            await handle_websocket(ws)

        mock_stop.assert_awaited_once_with("cid-stop")


# --- handle_restart_container additional coverage ---


class TestHandleRestartContainerExtra:
    async def test_restart_cleanup_error(self, user):
        ws = _mock_ws()
        workspace = await workspace_manager.create_workspace(
            user["id"], "restart-err"
        )
        state = _base_state(user=user)
        state["workspace_id"] = workspace["id"]
        state["workspace"] = workspace

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "new-cid"
            state["pi_client"] = _mock_pi_client()

        container_manager.registry.track_activity("new-cid", workspace["id"])

        with (
            patch.object(
                ws_handler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                ws_handler,
                "cleanup_connection",
                side_effect=RuntimeError("cleanup boom"),
            ),
            patch.object(
                container_manager.registry,
                "get_workspace_ports",
                return_value=[9000],
            ),
        ):
            await handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        ready = [
            c
            for c in calls
            if c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(ready) == 1
        container_manager.registry.states.pop(workspace["id"], None)

    async def test_restart_fractional_timeout_with_resume(
        self, user, monkeypatch
    ):
        ws = _mock_ws()
        workspace = await workspace_manager.create_workspace(
            user["id"], "restart-frac"
        )
        state = _base_state(user=user)
        state["workspace_id"] = workspace["id"]
        state["workspace"] = workspace

        # Set fractional timeout (e.g. 90 seconds = 1.5 minutes)
        original_timeout = container_manager.IDLE_TIMEOUT_SECONDS
        container_manager.IDLE_TIMEOUT_SECONDS = 90

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "new-cid"
            state["pi_client"] = _mock_pi_client()
            state["resume_session"] = "/some/session.jsonl"

        container_manager.registry.track_activity("new-cid", workspace["id"])

        try:
            with (
                patch.object(
                    ws_handler,
                    "start_workspace_container",
                    side_effect=fake_start,
                ),
                patch.object(
                    container_manager.registry,
                    "get_workspace_ports",
                    return_value=[],
                ),
            ):
                await handle_restart_container(ws, state)

            calls = [c[0][0] for c in ws.send_json.call_args_list]
            ready = [
                c
                for c in calls
                if c.get("type") == "event"
                and c.get("event", {}).get("name") == "container_ready"
            ]
            assert len(ready) == 1
            assert "1.5m" in ready[0]["event"]["value"]["reason"]
            assert "session resumed" in ready[0]["event"]["value"]["reason"]
        finally:
            container_manager.IDLE_TIMEOUT_SECONDS = original_timeout
            container_manager.registry.states.pop(workspace["id"], None)


# --- handle_websocket (integration) ---


class TestHandleWebsocket:
    async def test_missing_token(self):
        ws = _mock_ws(query_params={})
        await handle_websocket(ws)
        ws.close.assert_awaited_once_with(code=4001, reason="Missing token")

    async def test_invalid_token(self, db):
        ws = _mock_ws(query_params={"token": "bad"})
        await handle_websocket(ws)
        ws.close.assert_awaited_once_with(code=4001, reason="Invalid token")

    async def test_valid_token_then_disconnect(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect())

        await handle_websocket(ws)

        ws.accept.assert_awaited_once()

    async def test_invalid_json(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=["not json", WebSocketDisconnect()]
        )

        await handle_websocket(ws)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("Invalid JSON" in str(c) for c in calls)

    async def test_unknown_command(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
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
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        workspace = await workspace_manager.create_workspace(
            user["id"], "ui-ready-ws"
        )

        async def fake_start(ws_arg, state, wid, ws_obj):
            state["container_id"] = "cid"
            state["pi_client"] = _mock_pi_client()

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
                ws_handler,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                container_manager.registry,
                "get_workspace_ports",
                return_value=[],
            ),
            patch.object(
                container_manager.registry,
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
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
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
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(side_effect=RuntimeError("unexpected"))

        await handle_websocket(ws)

        ws.accept.assert_awaited_once()
        assert ws not in ws_handler._connections


class TestExecHandlers:
    async def test_exec_start_no_container(self):
        ws = _mock_ws()
        state = {"container_id": None, "exec_session": None, "exec_task": None}
        await handle_exec_start(ws, state, {"command": ["ls"]})
        assert state["exec_session"] is None

    async def test_exec_start_no_command(self):
        ws = _mock_ws()
        state = {
            "container_id": "cid",
            "exec_session": None,
            "exec_task": None,
        }
        await handle_exec_start(ws, state, {"command": []})
        ws.send_json.assert_awaited()
        assert "command" in ws.send_json.call_args[0][0].get("message", "")

    async def test_exec_start_success(self):
        ws = _mock_ws()
        state = {
            "container_id": "cid",
            "exec_session": None,
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
            "bark_backend.ws_handler.ExecSession",
            return_value=mock_session,
        ):
            with patch.object(container_manager.registry, "record_activity"):
                await handle_exec_start(ws, state, {"command": ["ls"]})
        assert state["exec_session"] is mock_session
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
            "exec_session": session,
        }
        data = base64.b64encode(b"hello").decode()
        with patch.object(container_manager.registry, "record_activity"):
            await handle_exec_input(state, {"data": data})
        session.write.assert_awaited_with(b"hello")

    async def test_exec_input_no_session(self):
        state = {"container_id": "cid", "exec_session": None}
        await handle_exec_input(state, {"data": ""})  # should not raise

    async def test_exec_close_stdin(self):
        session = AsyncMock()
        state = {"exec_session": session}
        await handle_exec_close_stdin(state)
        session.close_stdin.assert_awaited_once()

    async def test_exec_close_stdin_no_session(self):
        state = {"exec_session": None}
        await handle_exec_close_stdin(state)  # should not raise

    async def test_exec_stop(self):
        session = AsyncMock()
        task = asyncio.create_task(asyncio.sleep(10))
        state = {"exec_session": session, "exec_task": task}
        await handle_exec_stop(state)
        assert state["exec_session"] is None
        assert state["exec_task"] is None

    async def test_stop_exec_no_session(self):
        state = {"exec_session": None, "exec_task": None}
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
        state = {"container_id": "cid"}
        with patch.object(container_manager.registry, "record_activity"):
            await forward_exec_output(ws, session, state)
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
        ws.send_json = AsyncMock(side_effect=RuntimeError("ws dead"))
        state = {"container_id": "cid"}
        with patch.object(container_manager.registry, "record_activity"):
            await forward_exec_output(ws, session, state)
        # Should not raise

    async def test_cleanup_connection_stops_exec(self):
        session = AsyncMock()
        task = asyncio.create_task(asyncio.sleep(10))
        state = {
            "user": {"email": "test"},
            "workspace_id": None,
            "container_id": None,
            "pi_client": None,
            "event_task": None,
            "terminal_session": None,
            "terminal_task": None,
            "exec_session": session,
            "exec_task": task,
            "_idle_cb": None,
        }
        ws = _mock_ws()
        await cleanup_connection(ws, state)
        session.stop.assert_awaited_once()
        assert state["exec_session"] is None


class TestExecDispatch:
    async def test_dispatch_exec_start(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_start", "command": ["ls"]}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            ws_handler, "handle_exec_start", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(ws)
        mock.assert_awaited_once()

    async def test_dispatch_exec_input(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_input", "data": "AA=="}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            ws_handler, "handle_exec_input", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(ws)
        mock.assert_awaited_once()

    async def test_dispatch_exec_stop(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_stop"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            ws_handler, "handle_exec_stop", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(ws)
        mock.assert_awaited_once()

    async def test_dispatch_exec_close_stdin(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "exec_close_stdin"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            ws_handler, "handle_exec_close_stdin", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(ws)
        mock.assert_awaited_once()

    async def test_dispatch_heartbeat(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "heartbeat"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            ws_handler, "handle_heartbeat", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(ws)
        mock.assert_awaited_once()


class TestHandleHeartbeat:
    async def test_records_activity(self):
        state = {"container_id": "cid-hb"}
        container_manager.registry.track_activity("cid-hb", "ws-hb")
        container_manager.registry.states["ws-hb"].last_activity = 0.0

        await ws_handler.handle_heartbeat(state)

        assert container_manager.registry.states["ws-hb"].last_activity > 0.0
        container_manager.registry.states.pop("ws-hb", None)
        container_manager.registry._cid_to_wsid.pop("cid-hb", None)

    async def test_no_container_id(self):
        state = {}
        # Should not raise
        await ws_handler.handle_heartbeat(state)


class TestResetWorkspaceState:
    async def test_resets_pi_and_refcount(self):
        pi = _mock_pi_client()
        session = WorkspaceSession("ws-reset")
        session.pi_client = pi
        session.event_task = asyncio.create_task(asyncio.sleep(10))
        ws_handler._sessions["ws-reset"] = session
        container_manager.registry.track_activity("cid-reset", "ws-reset")
        container_manager.registry.states["ws-reset"].connection_count = 2

        await reset_workspace_state("ws-reset")

        assert "ws-reset" not in ws_handler._sessions
        assert "ws-reset" not in container_manager.registry.states
        pi.disconnect.assert_awaited_once()

    async def test_noop_for_unknown_workspace(self):
        await reset_workspace_state("ws-unknown")  # should not raise

    async def test_no_event_task(self):
        pi = _mock_pi_client()
        session = WorkspaceSession("ws-no-task")
        session.pi_client = pi
        ws_handler._sessions["ws-no-task"] = session
        await reset_workspace_state("ws-no-task")
        pi.disconnect.assert_awaited_once()
