"""Tests for ws_handler: WebSocket command dispatch, event forwarding, terminal, cleanup."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, patch, PropertyMock

from fastapi import WebSocketDisconnect

from bark_backend import (
    ws_handler,
    container_manager,
    workspace_manager,
)
from bark_backend.ws_handler import (
    WorkspaceSession,
    derive_hosting_info,
    start_workspace_container,
    handle_workspace_connect,
    handle_workspace_disconnect,
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
    session.container_id = container_id
    session.subscribers = {ws}
    ws_handler._sessions[workspace_id] = session


def _teardown_workspace_state(workspace_id):
    ws_handler._sessions.pop(workspace_id, None)
    container_manager.registry.states.pop(workspace_id, None)


class TestCleanupConnection:
    async def test_cleanup_full_last_connection(self):
        ws = _mock_ws()
        t = _mock_terminal()
        state = _base_state()
        state["container_id"] = "ctr-full"
        state["workspace_id"] = "ws-cleanup-1"
        state["_idle_cb"] = lambda ws: None
        state["terminal_session"] = t
        state["terminal_task"] = asyncio.create_task(asyncio.sleep(10))

        # Simulate: one connection, shared Pi state
        container_manager.registry.track_activity("ctr-full", "ws-cleanup-1")
        container_manager.registry.add_connection("ws-cleanup-1")
        session = WorkspaceSession("ws-cleanup-1")
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

        t.stop.assert_awaited_once()
        mock_stop.assert_awaited_once_with("ctr-full")
        assert state["_idle_cb"] is None
        assert state["terminal_session"] is None
        assert "ws-cleanup-1" not in ws_handler._sessions

        container_manager.registry.states.pop("ws-cleanup-1", None)

    async def test_cleanup_not_last_connection(self):
        """When other connections remain, Pi and container survive."""
        ws = _mock_ws()
        t = _mock_terminal()
        state = _base_state()
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
        mock_stop.assert_not_awaited()
        # Terminal for THIS connection should be stopped
        t.stop.assert_awaited_once()
        # Shared state still present
        assert "ws-cleanup-2" in ws_handler._sessions

        # Cleanup
        container_manager.registry.states.pop("ws-cleanup-2", None)
        session = ws_handler._sessions.pop("ws-cleanup-2", None)

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


class TestHandleWorkspaceDisconnect:
    async def test_disconnect(self):
        ws = _mock_ws()
        state = _base_state()
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


# --- handle_restart_container ---


class TestStartWorkspaceContainer:
    async def test_new_session(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
        workspace = await workspace_manager.create_workspace(
            user["id"], "start-ws"
        )

        async def fake_start(*a, **kw):
            container_manager.registry.track_activity("cid-1", workspace["id"])
            return ("cid-1", "created")

        with (
            patch.object(
                container_manager.registry,
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
        assert workspace["id"] in ws_handler._sessions
        assert state["_idle_cb"] is not None

        ws_handler._sessions.pop(workspace["id"], None)
        container_manager.registry.states.pop(workspace["id"], None)

    async def test_idle_callback_ws_error(self, user):
        ws = _mock_ws(headers={"host": "localhost:8997"})
        state = _base_state(user=user)
        workspace = await workspace_manager.create_workspace(
            user["id"], "idle-ws"
        )

        async def fake_start(*a, **kw):
            container_manager.registry.track_activity("cid-3", workspace["id"])
            return ("cid-3", "created")

        with (
            patch.object(
                container_manager.registry,
                "start_container",
                side_effect=fake_start,
            ),
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

        ws_handler._sessions.pop(workspace["id"], None)
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


class TestBrowserBridge:
    async def test_dispatch_browser_response(self, user):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        ws = _mock_ws(query_params={"token": token})
        ws.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "browser_response", "id": "req-1"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            ws_handler,
            "handle_browser_response",
            wraps=ws_handler.handle_browser_response,
        ) as mock:
            await handle_websocket(ws)
        mock.assert_called_once()

    async def test_handle_browser_response_resolves_future(self):
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        ws_handler._pending_browser_requests["req-1"] = future

        ws_handler.handle_browser_response(
            {"id": "req-1", "status": 200, "body": "hello"}
        )

        assert future.done()
        result = future.result()
        assert result["body"] == "hello"

    async def test_handle_browser_response_missing_id(self):
        # Should not raise
        ws_handler.handle_browser_response({})

    async def test_handle_browser_response_unknown_id(self):
        # Should not raise
        ws_handler.handle_browser_response({"id": "unknown"})

    async def test_dispatch_browser_request_no_session(self):
        result = await ws_handler.dispatch_browser_request(
            "nonexistent-ws", {"action": "fetch", "url": "http://example.com"}
        )
        assert "error" in result
        assert "No browser client" in result["error"]

    async def test_dispatch_browser_request_no_subscribers(self):
        ws_handler.get_or_create_session("ws-empty")
        try:
            result = await ws_handler.dispatch_browser_request(
                "ws-empty", {"action": "fetch", "url": "http://example.com"}
            )
            assert "error" in result
            assert "No browser client" in result["error"]
        finally:
            ws_handler._sessions.pop("ws-empty", None)

    async def test_dispatch_browser_request_success(self):
        session = ws_handler.get_or_create_session("ws-bridge")
        mock_ws = AsyncMock()
        session.subscribers.add(mock_ws)

        async def respond_later():
            await asyncio.sleep(0.1)
            # Find the pending request and resolve it
            for req_id, future in ws_handler._pending_browser_requests.items():
                if not future.done():
                    future.set_result(
                        {"id": req_id, "status": 200, "body": "response-data"}
                    )
                    break

        task = asyncio.create_task(respond_later())
        try:
            result = await ws_handler.dispatch_browser_request(
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
            ws_handler._sessions.pop("ws-bridge", None)

    async def test_dispatch_browser_request_timeout(self):
        session = ws_handler.get_or_create_session("ws-timeout")
        mock_ws = AsyncMock()
        session.subscribers.add(mock_ws)
        try:
            result = await ws_handler.dispatch_browser_request(
                "ws-timeout",
                {"action": "fetch", "url": "http://example.com"},
                timeout=0.1,
            )
            assert "error" in result
            assert "timeout" in result["error"].lower()
        finally:
            ws_handler._sessions.pop("ws-timeout", None)


class TestResetWorkspaceState:
    async def test_noop_for_unknown_workspace(self):
        await reset_workspace_state("ws-unknown")  # should not raise
