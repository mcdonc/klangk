"""Tests for ws_handler: WebSocket command dispatch, event forwarding, terminal, cleanup."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from fastapi import WebSocketDisconnect

from bark_backend import ws_handler, container_manager, user_store, workspace_manager
from bark_backend.ws_handler import (
    _derive_hosting_info,
    _start_workspace_container,
    _handle_workspace_connect,
    _handle_workspace_disconnect,
    _handle_prompt,
    _handle_steer,
    _handle_follow_up,
    _handle_abort,
    _handle_extension_ui_response,
    _handle_restart_container,
    _handle_terminal_start,
    _handle_terminal_input,
    _handle_terminal_resize,
    _handle_terminal_stop,
    _forward_terminal_output,
    _forward_events,
    _cleanup_connection,
    _send_error,
    handle_websocket,
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
        "user": user or {"id": "uid", "username": "testuser"},
        "pi_client": None,
        "container_id": None,
        "event_task": None,
        "agent_running": False,
        "terminal_session": None,
        "terminal_task": None,
    }


# --- _send_error ---


class TestSendError:
    async def test_sends_error_json(self):
        ws = _mock_ws()
        await _send_error(ws, "bad thing")
        ws.send_json.assert_awaited_once_with({"type": "error", "message": "bad thing"})


# --- _derive_hosting_info ---


class TestDeriveHostingInfo:
    def test_env_vars_take_precedence(self, monkeypatch):
        monkeypatch.setenv("BARK_HOSTING_HOSTNAME", "env.example.com")
        monkeypatch.setenv("BARK_HOSTING_PROTO", "https")
        monkeypatch.setenv("BARK_HOSTING_BASE_PATH", "/app")
        ws = _mock_ws(headers={"host": "header.example.com"})
        h, p, b = _derive_hosting_info(ws)
        assert h == "env.example.com"
        assert p == "https"
        assert b == "/app"

    def test_falls_back_to_headers(self, monkeypatch):
        monkeypatch.delenv("BARK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("BARK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("BARK_HOSTING_BASE_PATH", raising=False)
        ws = _mock_ws(
            headers={
                "x-forwarded-host": "fwd.example.com",
                "x-forwarded-proto": "https",
                "x-forwarded-prefix": "/bark",
            }
        )
        h, p, b = _derive_hosting_info(ws)
        assert h == "fwd.example.com"
        assert p == "https"
        assert b == "/bark"

    def test_falls_back_to_host_header(self, monkeypatch):
        monkeypatch.delenv("BARK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("BARK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("BARK_HOSTING_BASE_PATH", raising=False)
        ws = _mock_ws(headers={"host": "myhost:8997"})
        h, p, b = _derive_hosting_info(ws)
        assert h == "myhost:8997"
        assert p == "http"
        assert b == ""

    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("BARK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("BARK_HOSTING_PROTO", raising=False)
        monkeypatch.delenv("BARK_HOSTING_BASE_PATH", raising=False)
        ws = _mock_ws(headers={})
        h, p, b = _derive_hosting_info(ws)
        assert h == "localhost"
        assert p == "http"
        assert b == ""


# --- _handle_steer ---


class TestHandleSteer:
    async def test_steer(self):
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi
        state["container_id"] = "cid"
        container_manager._containers["cid"] = {
            "last_activity": 0,
            "workspace_id": "ws",
        }

        await _handle_steer(state, {"text": "go left"})

        pi.steer.assert_awaited_once_with("go left")
        container_manager._containers.pop("cid", None)

    async def test_steer_no_client(self):
        state = _base_state()
        await _handle_steer(state, {"text": "go left"})
        assert state["pi_client"] is None


# --- _handle_follow_up ---


class TestHandleFollowUp:
    async def test_follow_up(self):
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi
        state["container_id"] = "cid"
        container_manager._containers["cid"] = {
            "last_activity": 0,
            "workspace_id": "ws",
        }

        await _handle_follow_up(state, {"text": "and then?"})

        pi.follow_up.assert_awaited_once_with("and then?")
        container_manager._containers.pop("cid", None)

    async def test_follow_up_no_client(self):
        state = _base_state()
        await _handle_follow_up(state, {"text": "and then?"})
        assert state["pi_client"] is None


# --- _handle_abort ---


class TestHandleAbort:
    async def test_abort(self):
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi
        await _handle_abort(state)
        pi.abort.assert_awaited_once()

    async def test_abort_no_client(self):
        state = _base_state()
        await _handle_abort(state)
        assert state["pi_client"] is None


# --- _handle_extension_ui_response ---


class TestHandleExtensionUiResponse:
    async def test_forwards_value(self):
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi

        await _handle_extension_ui_response(
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

        await _handle_extension_ui_response(
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

        await _handle_extension_ui_response(
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
        await _handle_extension_ui_response(state, {"id": "ext-1"})
        assert state["pi_client"] is None


# --- _handle_terminal_input ---


class TestHandleTerminalInput:
    async def test_writes_data(self):
        t = _mock_terminal()
        state = _base_state()
        state["terminal_session"] = t
        state["container_id"] = "cid"
        container_manager._containers["cid"] = {
            "last_activity": 0,
            "workspace_id": "ws",
        }

        await _handle_terminal_input(state, {"data": "ls\n"})

        t.write.assert_awaited_once_with("ls\n")
        container_manager._containers.pop("cid", None)

    async def test_no_session(self):
        state = _base_state()
        await _handle_terminal_input(state, {"data": "ls\n"})
        assert state["terminal_session"] is None

    async def test_dead_session(self):
        t = _mock_terminal(alive=False)
        state = _base_state()
        state["terminal_session"] = t
        await _handle_terminal_input(state, {"data": "ls\n"})
        t.write.assert_not_awaited()


# --- _handle_terminal_resize ---


class TestHandleTerminalResize:
    async def test_resize(self):
        t = _mock_terminal()
        state = _base_state()
        state["terminal_session"] = t

        await _handle_terminal_resize(state, {"cols": 120, "rows": 40})

        t.resize.assert_awaited_once_with(120, 40)

    async def test_resize_defaults(self):
        t = _mock_terminal()
        state = _base_state()
        state["terminal_session"] = t

        await _handle_terminal_resize(state, {})

        t.resize.assert_awaited_once_with(80, 24)

    async def test_no_session(self):
        state = _base_state()
        await _handle_terminal_resize(state, {"cols": 120, "rows": 40})
        assert state["terminal_session"] is None


# --- _handle_terminal_stop ---


class TestHandleTerminalStop:
    async def test_stops_session(self):
        t = _mock_terminal()
        state = _base_state()
        state["terminal_session"] = t
        state["terminal_task"] = asyncio.create_task(asyncio.sleep(10))

        await _handle_terminal_stop(state)

        t.stop.assert_awaited_once()
        assert state["terminal_session"] is None
        assert state["terminal_task"] is None

    async def test_no_session(self):
        state = _base_state()
        await _handle_terminal_stop(state)
        assert state["terminal_session"] is None
        assert state["terminal_task"] is None


# --- _handle_terminal_start ---


class TestHandleTerminalStart:
    async def test_starts_session(self):
        ws = _mock_ws()
        state = _base_state()
        state["container_id"] = "cid"
        container_manager._containers["cid"] = {
            "last_activity": 0,
            "workspace_id": "ws",
        }

        with patch.object(ws_handler, "TerminalSession") as MockTS:
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield  # make it an async generator

            mock_session.output = fake_output

            await _handle_terminal_start(ws, state, {"cols": 100, "rows": 30})

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
        container_manager._containers.pop("cid", None)

    async def test_no_container(self):
        ws = _mock_ws()
        state = _base_state()
        await _handle_terminal_start(ws, state, {})
        assert state["terminal_session"] is None


# --- _forward_terminal_output ---


class TestForwardTerminalOutput:
    async def test_forwards_output(self):
        ws = _mock_ws()
        t = _mock_terminal()
        state = _base_state()

        async def fake_output():
            yield "line1"
            yield "line2"

        t.output = fake_output

        await _forward_terminal_output(ws, t, state)

        calls = ws.send_json.call_args_list
        assert calls[0][0][0] == {"type": "terminal_output", "data": "line1"}
        assert calls[1][0][0] == {"type": "terminal_output", "data": "line2"}
        # Stream ended — container_stopped event sent
        assert calls[2][0][0]["type"] == "event"
        assert calls[2][0][0]["event"]["name"] == "container_stopped"

    async def test_cancelled_error_propagates(self):
        ws = _mock_ws()
        t = _mock_terminal()
        state = _base_state()

        async def cancel_output():
            raise asyncio.CancelledError()
            yield  # noqa

        t.output = cancel_output

        with pytest.raises(asyncio.CancelledError):
            await _forward_terminal_output(ws, t, state)

    async def test_ws_error_logged(self):
        ws = _mock_ws()
        ws.send_json = AsyncMock(side_effect=RuntimeError("ws closed"))
        t = _mock_terminal()
        state = _base_state()

        async def fake_output():
            yield "data"

        t.output = fake_output

        await _forward_terminal_output(ws, t, state)
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

        await _forward_terminal_output(ws, t, state)
        # Both sends failed — verify both were attempted
        assert ws.send_json.call_count == 2


# --- _forward_events ---


class TestForwardEvents:
    async def test_forwards_pi_events(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)

        events = [
            {"type": "agent_start"},
            {"type": "message_start", "message": {"id": "m1"}},
            {
                "type": "message_update",
                "message": {"id": "m1"},
                "assistantMessageEvent": {"type": "text_delta", "delta": "hello"},
            },
            {"type": "message_end", "message": {"id": "m1"}},
            {"type": "agent_end"},
        ]

        async def fake_events():
            for e in events:
                yield e

        pi.events = fake_events

        await _forward_events(ws, pi, workspace["id"], state)

        assert ws.send_json.call_count >= 5
        assert state["agent_running"] is False

    async def test_saves_assistant_text(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)

        events = [
            {"type": "message_start", "message": {"id": "m1"}},
            {
                "type": "message_update",
                "message": {"id": "m1"},
                "assistantMessageEvent": {"type": "text_delta", "delta": "hello world"},
            },
            {"type": "message_end", "message": {"id": "m1"}},
        ]

        async def fake_events():
            for e in events:
                yield e

        pi.events = fake_events
        await _forward_events(ws, pi, workspace["id"], state)

        msgs = await user_store.get_messages(workspace["id"])
        assert any(m["content"] == "hello world" for m in msgs)

    async def test_saves_tool_call(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)

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
        await _forward_events(ws, pi, workspace["id"], state)

        msgs = await user_store.get_messages(workspace["id"])
        tool_msgs = [m for m in msgs if m["entry_type"] == "tool_call"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "bash"

    async def test_saves_error(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)

        events = [{"type": "error", "message": "something broke", "code": 500}]

        async def fake_events():
            for e in events:
                yield e

        pi.events = fake_events
        await _forward_events(ws, pi, workspace["id"], state)

        msgs = await user_store.get_messages(workspace["id"])
        assert any(m["entry_type"] == "error" for m in msgs)

    async def test_tracks_agent_running(self):
        ws = _mock_ws()
        pi = _mock_pi_client()
        state = _base_state()

        async def fake_events():
            yield {"type": "agent_start"}

        pi.events = fake_events
        await _forward_events(ws, pi, "ws-1", state)

        assert state["agent_running"] is True

    async def test_ws_error_logged(self):
        ws = _mock_ws()
        ws.send_json = AsyncMock(side_effect=RuntimeError("ws closed"))
        pi = _mock_pi_client()
        state = _base_state()

        async def fake_events():
            yield {"type": "agent_start"}

        pi.events = fake_events
        await _forward_events(ws, pi, "ws-1", state)

        ws.send_json.assert_awaited_once()


# --- _cleanup_connection ---


class TestCleanupConnection:
    async def test_cleanup_full(self):
        ws = _mock_ws()
        pi = _mock_pi_client()
        t = _mock_terminal()
        state = _base_state()
        state["pi_client"] = pi
        state["workspace_id"] = "ws-1"
        state["_idle_cb"] = lambda ws: None
        state["event_task"] = asyncio.create_task(asyncio.sleep(10))
        state["terminal_session"] = t
        state["terminal_task"] = asyncio.create_task(asyncio.sleep(10))

        container_manager._idle_callbacks.setdefault("ws-1", []).append(
            state["_idle_cb"]
        )

        await _cleanup_connection(ws, state)

        pi.disconnect.assert_awaited_once()
        t.stop.assert_awaited_once()
        assert state["_idle_cb"] is None
        assert state["terminal_session"] is None
        assert state["terminal_task"] is None

        container_manager._idle_callbacks.pop("ws-1", None)

    async def test_cleanup_minimal(self):
        ws = _mock_ws()
        state = _base_state()
        await _cleanup_connection(ws, state)
        assert state["pi_client"] is None
        assert state["terminal_session"] is None


# --- _handle_prompt ---


class TestHandlePrompt:
    async def test_empty_prompt(self):
        ws = _mock_ws()
        state = _base_state()
        await _handle_prompt(ws, state, {"text": ""})
        ws.send_json.assert_awaited_once()
        assert "Empty prompt" in ws.send_json.call_args[0][0]["message"]

    async def test_no_workspace(self):
        ws = _mock_ws()
        state = _base_state()
        await _handle_prompt(ws, state, {"text": "hello"})
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
        container_manager._containers["cid"] = {
            "last_activity": 0,
            "workspace_id": workspace["id"],
        }

        await _handle_prompt(ws, state, {"text": "hello world"})

        pi.prompt.assert_awaited_once_with("hello world")
        msgs = await user_store.get_messages(workspace["id"])
        assert any(m["content"] == "hello world" for m in msgs)
        container_manager._containers.pop("cid", None)

    async def test_prompt_queued(self, db):
        ws = _mock_ws()
        pi = _mock_pi_client()
        user = await user_store.create_user("u", "h")
        workspace = await user_store.create_workspace(user["id"], "ws")
        state = _base_state(user=user)
        state["pi_client"] = pi
        state["workspace_id"] = workspace["id"]
        state["container_id"] = "cid"
        state["agent_running"] = True
        container_manager._containers["cid"] = {
            "last_activity": 0,
            "workspace_id": workspace["id"],
        }

        await _handle_prompt(ws, state, {"text": "queued msg"})

        pi.follow_up.assert_awaited_once_with("queued msg")
        calls = [c[0][0] for c in ws.send_json.call_args_list]
        queued_events = [
            c
            for c in calls
            if c.get("type") == "event"
            and c.get("event", {}).get("name") == "prompt_queued"
        ]
        assert len(queued_events) == 1
        container_manager._containers.pop("cid", None)

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
                ws_handler, "_start_workspace_container", side_effect=fake_start
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            container_manager._containers["new-cid"] = {
                "last_activity": 0,
                "workspace_id": workspace["id"],
            }
            await _handle_prompt(ws, state, {"text": "hello after restart"})

        pi_new.prompt.assert_awaited_once_with("hello after restart")
        container_manager._containers.pop("new-cid", None)

    async def test_prompt_auto_restart_workspace_gone(self, db):
        ws = _mock_ws()
        state = _base_state()
        state["pi_client"] = None
        state["workspace_id"] = "ws-gone"
        state["container_id"] = "cid"
        state["workspace"] = None

        await _handle_prompt(ws, state, {"text": "hello"})

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
                ws_handler, "_start_workspace_container", side_effect=fake_start
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            await _handle_prompt(ws, state, {"text": "hello"})

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
                ws_handler, "_start_workspace_container", side_effect=fake_start
            ),
            patch.object(
                ws_handler,
                "_cleanup_connection",
                side_effect=RuntimeError("cleanup boom"),
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            container_manager._containers["new-cid"] = {
                "last_activity": 0,
                "workspace_id": workspace["id"],
            }
            await _handle_prompt(ws, state, {"text": "hello"})

        pi_new.prompt.assert_awaited_once()
        container_manager._containers.pop("new-cid", None)

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
                ws_handler, "_start_workspace_container", side_effect=fake_start
            ),
            patch("asyncio.sleep", new_callable=AsyncMock),
        ):
            container_manager._containers["new-cid"] = {
                "last_activity": 0,
                "workspace_id": workspace["id"],
            }
            await _handle_prompt(ws, state, {"text": "hello"})

        pi_new.prompt.assert_awaited_once()
        container_manager._containers.pop("new-cid", None)


# --- _handle_workspace_connect ---


class TestHandleWorkspaceConnect:
    async def test_missing_workspace_id(self):
        ws = _mock_ws()
        state = _base_state()
        await _handle_workspace_connect(ws, state, {})
        assert "Missing" in ws.send_json.call_args[0][0]["message"]

    async def test_workspace_not_found(self, user):
        ws = _mock_ws()
        state = _base_state(user=user)
        await _handle_workspace_connect(ws, state, {"workspaceId": "fake"})
        assert "not found" in ws.send_json.call_args[0][0]["message"]

    async def test_connect_success(self, user):
        ws = _mock_ws()
        workspace = await workspace_manager.create_workspace(user["id"], "test-ws")
        state = _base_state(user=user)

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "cid"
            state["pi_client"] = _mock_pi_client()

        with (
            patch.object(
                ws_handler, "_start_workspace_container", side_effect=fake_start
            ),
            patch.object(
                container_manager, "get_workspace_ports", return_value=[9000, 9001]
            ),
        ):
            await _handle_workspace_connect(ws, state, {"workspaceId": workspace["id"]})

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        ready = [c for c in calls if c.get("type") == "workspace_ready"]
        assert len(ready) == 1
        assert ready[0]["workspaceId"] == workspace["id"]
        # Integer timeout (default 30m) should show as "30m" not "30.0m"
        assert "30m" in state["pending_status_msg"]

    async def test_connect_fractional_timeout(self, user):
        ws = _mock_ws()
        workspace = await workspace_manager.create_workspace(user["id"], "frac-ws")
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
                    ws_handler, "_start_workspace_container", side_effect=fake_start
                ),
                patch.object(container_manager, "get_workspace_ports", return_value=[]),
            ):
                await _handle_workspace_connect(
                    ws, state, {"workspaceId": workspace["id"]}
                )

            assert "1.5m" in state["pending_status_msg"]
            assert "session resumed" in state["pending_status_msg"]
        finally:
            container_manager.IDLE_TIMEOUT_SECONDS = original_timeout


# --- _handle_workspace_disconnect ---


class TestHandleWorkspaceDisconnect:
    async def test_disconnect(self):
        ws = _mock_ws()
        pi = _mock_pi_client()
        state = _base_state()
        state["pi_client"] = pi
        state["container_id"] = "cid"
        state["workspace_id"] = "ws-1"

        with patch.object(
            container_manager, "stop_and_remove_container", new_callable=AsyncMock
        ):
            await _handle_workspace_disconnect(ws, state)

        assert state["workspace_id"] is None
        assert state["container_id"] is None
        assert state["pi_client"] is None


# --- _handle_restart_container ---


class TestHandleRestartContainer:
    async def test_no_workspace(self):
        ws = _mock_ws()
        state = _base_state()
        await _handle_restart_container(ws, state)
        assert "Not connected" in ws.send_json.call_args[0][0]["message"]

    async def test_workspace_gone(self, user):
        ws = _mock_ws()
        state = _base_state(user=user)
        state["workspace_id"] = "gone-ws"
        state["workspace"] = None

        await _handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("not found" in str(c) for c in calls)

    async def test_restart_success(self, user):
        ws = _mock_ws()
        workspace = await workspace_manager.create_workspace(user["id"], "restart-ws")
        state = _base_state(user=user)
        state["workspace_id"] = workspace["id"]
        state["workspace"] = workspace

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "new-cid"
            state["pi_client"] = _mock_pi_client()

        container_manager._containers["new-cid"] = {
            "last_activity": 0,
            "workspace_id": workspace["id"],
        }

        with (
            patch.object(
                ws_handler, "_start_workspace_container", side_effect=fake_start
            ),
            patch.object(container_manager, "get_workspace_ports", return_value=[9000]),
        ):
            await _handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        ready = [
            c
            for c in calls
            if c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(ready) == 1
        container_manager._containers.pop("new-cid", None)


# --- _start_workspace_container ---


class TestStartWorkspaceContainer:
    async def test_new_session(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
        workspace = await workspace_manager.create_workspace(user["id"], "start-ws")
        pi = _mock_pi_client()

        async def fake_events():
            return
            yield

        pi.events = fake_events

        with (
            patch.object(
                container_manager,
                "start_container",
                new_callable=AsyncMock,
                return_value=("cid-1", "created"),
            ),
            patch.object(ws_handler, "PiRpcClient", return_value=pi),
            patch("glob.glob", return_value=[]),
        ):
            await _start_workspace_container(ws, state, workspace["id"], workspace)

        assert state["container_id"] == "cid-1"
        assert state["pi_client"] is pi
        assert state["workspace"] == workspace
        assert state["resume_session"] is None
        assert state["event_task"] is not None
        assert state["_idle_cb"] is not None

        state["event_task"].cancel()
        try:
            await state["event_task"]
        except asyncio.CancelledError:
            pass
        container_manager._idle_callbacks.pop(workspace["id"], None)

    async def test_resume_session(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
        workspace = await workspace_manager.create_workspace(user["id"], "resume-ws")
        pi = _mock_pi_client()

        async def fake_events():
            return
            yield

        pi.events = fake_events

        sessions_path = str(
            workspace_manager.get_sessions_host_path(user["id"], workspace["id"])
        )

        with (
            patch.object(
                container_manager,
                "start_container",
                new_callable=AsyncMock,
                return_value=("cid-2", "connected"),
            ),
            patch.object(ws_handler, "PiRpcClient", return_value=pi),
            patch("glob.glob", return_value=[f"{sessions_path}/session.jsonl"]),
        ):
            await _start_workspace_container(ws, state, workspace["id"], workspace)

        assert state["resume_session"] is not None
        assert "/home/bark/.pi/sessions" in state["resume_session"]
        assert state["container_status"] == "connected"

        state["event_task"].cancel()
        try:
            await state["event_task"]
        except asyncio.CancelledError:
            pass
        container_manager._idle_callbacks.pop(workspace["id"], None)

    async def test_idle_callback_ws_error(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
        workspace = await workspace_manager.create_workspace(user["id"], "idle-ws")
        pi = _mock_pi_client()

        async def fake_events():
            return
            yield

        pi.events = fake_events

        with (
            patch.object(
                container_manager,
                "start_container",
                new_callable=AsyncMock,
                return_value=("cid-3", "created"),
            ),
            patch.object(ws_handler, "PiRpcClient", return_value=pi),
            patch("glob.glob", return_value=[]),
        ):
            await _start_workspace_container(ws, state, workspace["id"], workspace)

        # Test idle callback when WS send fails
        ws.send_json = AsyncMock(side_effect=RuntimeError("ws closed"))
        idle_cb = state["_idle_cb"]
        await idle_cb(workspace["id"])  # should not raise
        assert ws.send_json.call_count == 1

        state["event_task"].cancel()
        try:
            await state["event_task"]
        except asyncio.CancelledError:
            pass
        container_manager._idle_callbacks.pop(workspace["id"], None)


# --- handle_websocket dispatch branches ---


class TestHandleWebsocketDispatch:
    """Test all command dispatch branches through the main handler."""

    async def _run_commands(self, user, commands):
        from bark_backend import auth as auth_mod

        token = auth_mod._create_token(user["id"], user["username"])
        ws = _mock_ws(query_params={"token": token})
        msgs = [json.dumps(c) for c in commands] + [WebSocketDisconnect()]
        ws.receive_text = AsyncMock(side_effect=msgs)
        await handle_websocket(ws)
        return ws

    async def test_dispatch_steer(self, user):
        ws = await self._run_commands(user, [{"cmd": "steer", "text": "left"}])
        ws.accept.assert_awaited_once()

    async def test_dispatch_follow_up(self, user):
        ws = await self._run_commands(user, [{"cmd": "follow_up", "text": "more"}])
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
        ws = await self._run_commands(user, [{"cmd": "terminal_input", "data": "x"}])
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
        from bark_backend import auth as auth_mod

        token = auth_mod._create_token(user["id"], user["username"])
        ws = _mock_ws(query_params={"token": token})

        # Connect to a workspace, then disconnect
        workspace = await workspace_manager.create_workspace(user["id"], "stop-ws")
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps(
                    {"cmd": "workspace_connect", "workspaceId": workspace["id"]}
                ),
                WebSocketDisconnect(),
            ]
        )

        async def fake_start(ws_arg, state, wid, ws_obj):
            state["container_id"] = "cid-stop"
            state["pi_client"] = _mock_pi_client()

        with (
            patch.object(
                ws_handler, "_start_workspace_container", side_effect=fake_start
            ),
            patch.object(container_manager, "get_workspace_ports", return_value=[]),
            patch.object(
                container_manager, "stop_and_remove_container", new_callable=AsyncMock
            ) as mock_stop,
        ):
            await handle_websocket(ws)

        mock_stop.assert_awaited()
        assert any(call.args == ("cid-stop",) for call in mock_stop.call_args_list)

    async def test_container_stop_error_on_disconnect(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod._create_token(user["id"], user["username"])
        ws = _mock_ws(query_params={"token": token})

        workspace = await workspace_manager.create_workspace(user["id"], "err-ws")
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps(
                    {"cmd": "workspace_connect", "workspaceId": workspace["id"]}
                ),
                WebSocketDisconnect(),
            ]
        )

        async def fake_start(ws_arg, state, wid, ws_obj):
            state["container_id"] = "cid-err"
            state["pi_client"] = _mock_pi_client()

        with (
            patch.object(
                ws_handler, "_start_workspace_container", side_effect=fake_start
            ),
            patch.object(container_manager, "get_workspace_ports", return_value=[]),
            patch.object(
                container_manager,
                "stop_and_remove_container",
                new_callable=AsyncMock,
                side_effect=OSError("stop failed"),
            ),
        ):
            await handle_websocket(ws)

        assert ws not in ws_handler._connections


# --- _handle_restart_container additional coverage ---


class TestHandleRestartContainerExtra:
    async def test_restart_cleanup_error(self, user):
        ws = _mock_ws()
        workspace = await workspace_manager.create_workspace(user["id"], "restart-err")
        state = _base_state(user=user)
        state["workspace_id"] = workspace["id"]
        state["workspace"] = workspace

        async def fake_start(ws, state, wid, workspace):
            state["container_id"] = "new-cid"
            state["pi_client"] = _mock_pi_client()

        container_manager._containers["new-cid"] = {
            "last_activity": 0,
            "workspace_id": workspace["id"],
        }

        with (
            patch.object(
                ws_handler, "_start_workspace_container", side_effect=fake_start
            ),
            patch.object(
                ws_handler,
                "_cleanup_connection",
                side_effect=RuntimeError("cleanup boom"),
            ),
            patch.object(container_manager, "get_workspace_ports", return_value=[9000]),
        ):
            await _handle_restart_container(ws, state)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        ready = [
            c
            for c in calls
            if c.get("type") == "event"
            and c.get("event", {}).get("name") == "container_ready"
        ]
        assert len(ready) == 1
        container_manager._containers.pop("new-cid", None)

    async def test_restart_fractional_timeout_with_resume(self, user, monkeypatch):
        ws = _mock_ws()
        workspace = await workspace_manager.create_workspace(user["id"], "restart-frac")
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

        container_manager._containers["new-cid"] = {
            "last_activity": 0,
            "workspace_id": workspace["id"],
        }

        try:
            with (
                patch.object(
                    ws_handler, "_start_workspace_container", side_effect=fake_start
                ),
                patch.object(container_manager, "get_workspace_ports", return_value=[]),
            ):
                await _handle_restart_container(ws, state)

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
            container_manager._containers.pop("new-cid", None)


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

        token = auth_mod._create_token(user["id"], user["username"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(side_effect=WebSocketDisconnect())

        await handle_websocket(ws)

        ws.accept.assert_awaited_once()

    async def test_invalid_json(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod._create_token(user["id"], user["username"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(side_effect=["not json", WebSocketDisconnect()])

        await handle_websocket(ws)

        calls = [c[0][0] for c in ws.send_json.call_args_list]
        assert any("Invalid JSON" in str(c) for c in calls)

    async def test_unknown_command(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod._create_token(user["id"], user["username"])
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

        token = auth_mod._create_token(user["id"], user["username"])
        ws = _mock_ws(query_params={"token": token})
        workspace = await workspace_manager.create_workspace(user["id"], "ui-ready-ws")

        async def fake_start(ws_arg, state, wid, ws_obj):
            state["container_id"] = "cid"
            state["pi_client"] = _mock_pi_client()

        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps(
                    {"cmd": "workspace_connect", "workspaceId": workspace["id"]}
                ),
                json.dumps({"cmd": "ui_ready"}),
                WebSocketDisconnect(),
            ]
        )

        with (
            patch.object(
                ws_handler, "_start_workspace_container", side_effect=fake_start
            ),
            patch.object(container_manager, "get_workspace_ports", return_value=[]),
            patch.object(
                container_manager, "stop_and_remove_container", new_callable=AsyncMock
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

        token = auth_mod._create_token(user["id"], user["username"])
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

        token = auth_mod._create_token(user["id"], user["username"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(side_effect=RuntimeError("unexpected"))

        await handle_websocket(ws)

        ws.accept.assert_awaited_once()
        assert ws not in ws_handler._connections
