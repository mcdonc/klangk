"""Tests for wshandler: WebSocket command dispatch, event forwarding, terminal, cleanup."""

import asyncio
import json
import time

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from fastapi import WebSocketDisconnect

from klangk_backend import (
    model,
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
    t.read_only = False
    return t


def _base_conn(user=None, ws=None):
    if ws is None:
        ws = _mock_sock()
    if user is None:
        user = {
            "id": "uid",
            "email": "testuser@example.com",
            "handle": "testuser",
        }
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

    async def test_read_only_blocks_typing(self):
        t = _mock_terminal()
        t.read_only = True
        conn = _base_conn()
        conn.terminal_session = t
        conn.container_id = "cid"
        container.registry.track_activity("cid", "ws")

        await conn.handle_terminal_input({"data": "ls\n"})
        t.write.assert_not_awaited()
        container.registry.states.pop("ws", None)

    async def test_read_only_allows_escape_sequences(self):
        t = _mock_terminal()
        t.read_only = True
        conn = _base_conn()
        conn.terminal_session = t
        conn.container_id = "cid"
        container.registry.track_activity("cid", "ws")

        # DA response: ESC [ ? 6 c
        await conn.handle_terminal_input({"data": "\x1b[?6c"})
        t.write.assert_awaited_once_with("\x1b[?6c")
        container.registry.states.pop("ws", None)

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
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        container.registry.track_activity("cid", "ws")

        # Create a session with shared windows so the shared_terminals
        # broadcast path (lines 977-978) is exercised.
        session = wshandler.state.get_or_create_session("ws")
        session.terminal_windows["other-uid"] = [
            {"name": "dev", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        wshandler.state.connections[sock] = conn

        with (
            patch.object(wshandler, "TerminalSession") as MockTS,
            patch(
                "klangk_backend.terminal.list_windows",
                return_value=[
                    {"id": "@0", "index": 0, "name": "bash", "active": True}
                ],
            ),
            patch(
                "klangk_backend.terminal.load_workspace_state",
                return_value={},
            ),
            patch("klangk_backend.terminal.restore_windows"),
            patch.object(wshandler, "attach_browser"),
        ):
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield  # make it an async generator

            mock_session.output = fake_output

            await conn.handle_terminal_start(
                {"cols": 100, "rows": 30, "browser_id": "test-browser-id"}
            )
            # Let the background task run
            await asyncio.sleep(0)

        MockTS.assert_called_once_with(
            "cid",
            session_name="uid",
            user_home="/home/testuser",
            user_id="uid",
            user_handle="testuser",
        )
        # Should have sent terminal_windows and shared_terminals
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and m.get("type") == "terminal_windows"
            for m in sent
        )
        assert any(
            isinstance(m, dict) and m.get("type") == "shared_terminals"
            for m in sent
        )

        # browser_id should be registered and stored on the connection
        start_kwargs = mock_session.start.call_args
        assert start_kwargs[1]["command_override"] is None
        assert conn._browser_id == "test-browser-id"
        assert conn.terminal_session is mock_session
        assert conn.terminal_task is not None
        # Should have sent terminal_started ack (followed by terminal_windows)
        assert any(
            isinstance(m, dict) and m.get("type") == "terminal_started"
            for m in sent
        )

        # _sync_terminal_windows should have populated terminal_windows
        ws_session = wshandler.state.sessions.get("ws")
        assert ws_session is not None
        assert "uid" in ws_session.terminal_windows
        assert ws_session.terminal_windows["uid"][0]["name"] == "bash"

        # Clean up
        wshandler.state.sessions.pop("ws", None)
        wshandler.state.connections.pop(sock, None)
        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_starts_session_restores_saved_state(self):
        """On first terminal_start, saved state is loaded and restored."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        container.registry.track_activity("cid", "ws")

        session = wshandler.state.get_or_create_session("ws")
        # Do NOT pre-populate terminal_windows — simulates restart
        await session.add_subscriber(sock, "cid")
        wshandler.state.connections[sock] = conn

        saved_state = {
            "uid": [
                {"name": "1", "index": 0, "id": "@0", "shared": False},
                {"name": "build", "index": 1, "id": "@1", "shared": True},
            ],
            "other-uid": [
                {"name": "1", "shared": False},
                {"name": "dev", "shared": True},
            ],
        }

        with (
            patch.object(wshandler, "TerminalSession") as MockTS,
            patch(
                "klangk_backend.terminal.list_windows",
                return_value=[
                    {"id": "@0", "index": 0, "name": "1", "active": True},
                    {"id": "@1", "index": 1, "name": "build", "active": False},
                ],
            ),
            patch("klangk_backend.terminal.tmux_command", return_value=""),
            patch(
                "klangk_backend.terminal.load_workspace_state",
                return_value=saved_state,
            ),
            patch("klangk_backend.terminal.restore_windows") as mock_restore,
            patch("klangk_backend.terminal.save_workspace_state"),
        ):
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield

            mock_session.output = fake_output

            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        # restore_windows called with saved windows
        mock_restore.assert_awaited_once_with(
            "cid",
            "uid",
            saved_state["uid"],
        )
        # terminal_windows populated from saved state
        ws_session = wshandler.state.sessions["ws"]
        assert "uid" in ws_session.terminal_windows
        # "build" should retain shared=True from saved state
        build_win = [
            w
            for w in ws_session.terminal_windows["uid"]
            if w["name"] == "build"
        ]
        assert len(build_win) == 1
        assert build_win[0]["shared"] is True
        # Other user's state also restored
        assert "other-uid" in ws_session.terminal_windows
        assert ws_session.terminal_windows["other-uid"][1]["shared"] is True

        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        wshandler.state.sessions.pop("ws", None)
        wshandler.state.connections.pop(sock, None)
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_terminal_start_requires_handle(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        # _user_home not set

        await conn.handle_terminal_start({"cols": 80, "rows": 24})
        sent = sock.send_json.call_args_list
        assert any(
            call.args[0].get("type") == "error"
            and "Handle" in call.args[0].get("message", "")
            for call in sent
        )

    async def test_terminal_start_without_code_in_isolation_sends_started(
        self,
    ):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/spectator"

        async def deny_isolation(perm):
            return perm != "code-in-isolation"

        conn._has_perm = deny_isolation
        await conn.handle_terminal_start({"cols": 80, "rows": 24})
        sent = sock.send_json.call_args_list
        # Should send terminal_started (no error) so the pane renders
        assert any(
            call.args[0].get("type") == "terminal_started" for call in sent
        )
        # But no actual session created
        assert conn.terminal_session is None

    async def test_rapid_terminal_start_debounced(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm
        import time

        conn._last_terminal_start = time.monotonic()
        await conn.handle_terminal_start({"cols": 80, "rows": 24})
        # Should be silently ignored (debounced)
        assert conn.terminal_session is None

        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_start_rename_failure_non_fatal(self):
        """If renaming the initial bash window fails, tabs still work."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        container.registry.track_activity("cid", "ws")

        with (
            patch.object(wshandler, "TerminalSession") as MockTS,
            patch(
                "klangk_backend.terminal.list_windows",
                return_value=[
                    {"id": "@0", "index": 0, "name": "bash", "active": True}
                ],
            ),
            patch(
                "klangk_backend.terminal.tmux_command",
                side_effect=RuntimeError("rename failed"),
            ),
        ):
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield

            mock_session.output = fake_output
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and m.get("type") == "terminal_started"
            for m in sent
        )
        # terminal_windows still sent even though rename failed
        assert any(
            isinstance(m, dict) and m.get("type") == "terminal_windows"
            for m in sent
        )
        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_start_window_list_failure_non_fatal(self):
        """If list_windows fails after terminal start, terminal still works."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        container.registry.track_activity("cid", "ws")

        with (
            patch.object(wshandler, "TerminalSession") as MockTS,
            patch(
                "klangk_backend.terminal.list_windows",
                side_effect=RuntimeError("tmux not ready"),
            ),
        ):
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield

            mock_session.output = fake_output
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        # terminal_started still sent despite list_windows failure
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and m.get("type") == "terminal_started"
            for m in sent
        )
        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_start_shared_list_failure_non_fatal(self):
        """If list_shared_terminals fails after start, terminal still works."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm
        container.registry.track_activity("cid", "ws")

        with (
            patch.object(wshandler, "TerminalSession") as MockTS,
            patch(
                "klangk_backend.terminal.list_windows",
                return_value=[
                    {"id": "@0", "index": 0, "name": "1", "active": True}
                ],
            ),
            patch("klangk_backend.terminal.tmux_command", return_value=""),
        ):
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield

            mock_session.output = fake_output
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and m.get("type") == "terminal_started"
            for m in sent
        )
        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_restart_revokes_old_browser_registration(self):
        """Starting a second terminal revokes the previous browser registration."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        container.registry.track_activity("cid", "ws")

        with (
            patch.object(wshandler, "TerminalSession") as MockTS,
            patch.object(wshandler, "attach_browser"),
        ):
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield

            mock_session.output = fake_output

            # First terminal start with browser_id
            await conn.handle_terminal_start(
                {"cols": 80, "rows": 24, "browser_id": "bid-1"}
            )
            await asyncio.sleep(0)
            assert conn._browser_id == "bid-1"
            assert container.registry.resolve_browser("bid-1") is not None

            conn.terminal_task.cancel()
            try:
                await conn.terminal_task
            except asyncio.CancelledError:
                pass

            # Second terminal start with same browser_id — re-registers
            await conn.handle_terminal_start(
                {"cols": 80, "rows": 24, "browser_id": "bid-1"}
            )
            await asyncio.sleep(0)
            assert conn._browser_id == "bid-1"
            assert container.registry.resolve_browser("bid-1") is not None

            conn.terminal_task.cancel()
            try:
                await conn.terminal_task
            except asyncio.CancelledError:
                pass
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_browser_reattach_updates_registration(self):
        """browser_reattach re-registers the browser ID and calls attach_browser."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"

        container.registry.register_browser("bid-old", "ws", sock)
        conn._browser_id = "bid-old"

        with patch.object(wshandler, "attach_browser") as mock_attach:
            await conn.handle_browser_reattach({"browser_id": "bid-new"})

        assert conn._browser_id == "bid-new"
        assert container.registry.resolve_browser("bid-new") == ("ws", sock)
        assert container.registry.resolve_browser("bid-old") is None
        mock_attach.assert_awaited_once_with("cid", "bid-new")

        container.registry.revoke_workspace_browsers("ws")

    async def test_browser_reattach_no_browser_id_is_noop(self):
        """browser_reattach with no browser_id does nothing."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._browser_id = "bid-existing"

        with patch.object(wshandler, "attach_browser") as mock_attach:
            await conn.handle_browser_reattach({})

        assert conn._browser_id == "bid-existing"
        mock_attach.assert_not_awaited()

    async def test_browser_reattach_no_container_is_noop(self):
        """browser_reattach without a container does nothing."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = None
        conn.workspace_id = "ws"

        with patch.object(wshandler, "attach_browser") as mock_attach:
            await conn.handle_browser_reattach({"browser_id": "bid-new"})

        assert conn._browser_id is None
        mock_attach.assert_not_awaited()

    async def test_passes_command_override(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.is_alive = True
        MockTS = MagicMock(return_value=mock_session)
        with (
            patch("klangk_backend.wshandler.TerminalSession", MockTS),
            patch.object(wshandler, "attach_browser"),
        ):
            await conn.handle_terminal_start(
                {
                    "cols": 80,
                    "rows": 24,
                    "commandOverride": "bash",
                    "browser_id": "bid-cmd",
                }
            )
            # Let the background task run
            await asyncio.sleep(0)

        mock_session.start.assert_awaited_once_with(
            80, 24, command_override="bash"
        )

        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_start_failure_sends_error(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
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
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_start_slow_client_cleans_up(self):
        """SlowClientError during start cleans up without sending error."""

        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.start = AsyncMock(side_effect=SlowClientError())
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk_backend.wshandler.TerminalSession", MockTS):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        mock_session.stop.assert_awaited_once()
        # No error message sent (client is gone)
        sent = sock.send_json.call_args_list
        assert not any(call.args[0].get("type") == "error" for call in sent)
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_start_failure_send_error_ws_dead(self):
        """If send_error itself fails with a WS error, it's swallowed."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        container.registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.start = AsyncMock(side_effect=ValueError("bad config"))
        # send_json raises RuntimeError (a _WS_ERRORS member) when
        # trying to send the error message
        sock.send_json = MagicMock(side_effect=RuntimeError("ws gone"))
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk_backend.wshandler.TerminalSession", MockTS):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        mock_session.stop.assert_awaited_once()
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_cancellation_during_start_cleans_up(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
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
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_session_replaced_during_start_aborts(self):
        """If stop_terminal replaces the session while start() is running,
        the startup task stops the orphaned session and does not send
        terminal_started."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
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
        container.registry.revoke_workspace_browsers("ws")
        container.registry.states.pop("ws", None)

    async def test_no_container(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_terminal_start({})
        assert conn.terminal_session is None


# --- handle management ---


class TestHandleSetHandle:
    async def test_set_handle_success(self, user, temp_data_dir):
        from klangk_backend import workspaces

        ws = await workspaces.create_workspace(user["id"], "handle-test")
        sock = _mock_sock()
        conn = _base_conn(
            user={"id": user["id"], "email": user["email"]}, ws=sock
        )
        conn.workspace_id = ws["id"]
        conn.workspace = {"user_id": user["id"]}
        conn.container_id = "cid"

        with patch(
            "klangk_backend.workspaces.populate_home_skel",
            new_callable=AsyncMock,
        ) as mock_skel:
            await conn.handle_set_handle({"handle": "alice"})
        mock_skel.assert_awaited_once_with("cid", user["id"])
        sent = sock.send_json.call_args_list
        assert any(
            call.args[0].get("type") == "handle_set"
            and call.args[0].get("handle") == "alice"
            for call in sent
        )
        assert conn._user_home == "/home/alice"

    async def test_set_handle_conflict(self, user, temp_data_dir):
        # Create another user that already has handle "alice"
        other = await model.create_user(
            "alice@example.com", "hash", verified=True
        )
        assert other["handle"] == "alice"

        sock = _mock_sock()
        conn = _base_conn(
            user={"id": user["id"], "email": user["email"]}, ws=sock
        )
        conn.workspace_id = "ws-1"

        await conn.handle_set_handle({"handle": "alice"})
        sent = sock.send_json.call_args_list
        assert any(call.args[0].get("type") == "handle_error" for call in sent)

    async def test_set_handle_no_workspace(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_set_handle({"handle": "alice"})
        sent = sock.send_json.call_args_list
        assert any(call.args[0].get("type") == "error" for call in sent)

    async def test_handle_auto_created_on_connect(self, user):
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock)
        workspace = await ws_mod.create_workspace(user["id"], "auto-handle")

        async def fake_start(*a, **kw):
            container.registry.track_activity("cid-ah", workspace["id"])
            return ("cid-ah", "created")

        with (
            patch.object(
                container.registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch("glob.glob", return_value=[]),
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        # Handle is derived from email at user creation time
        assert conn._user_home is not None
        assert conn._user_home.startswith("/home/")

        wshandler.state.sessions.pop(workspace["id"], None)
        container.registry.states.pop(workspace["id"], None)

    async def test_handle_resolved_on_start(self, user, temp_data_dir):
        from klangk_backend import workspaces

        ws = await workspaces.create_workspace(user["id"], "handle-test4")
        sock = _mock_sock()
        conn = _base_conn(
            user={"id": user["id"], "email": user["email"]}, ws=sock
        )
        conn.workspace_id = ws["id"]

        # Handle is already in the DB from create_user
        handle = await model.get_user_handle(user["id"])
        assert handle is not None
        assert handle == user["handle"]


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
        # Stream ended — no container_stopped event (terminal exit != container death)
        assert len(calls) == 2
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

        with patch.object(
            model,
            "add_chat_message",
            new_callable=AsyncMock,
            return_value={"id": "fake", "message": "left", "message_type": 2},
        ):
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

    async def test_connect_success(self, user, agent_user):
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

    async def test_connect_sends_default_command(self, user, agent_user):
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
        # Handle auto-created from email on connect
        assert conn._user_home is not None
        assert conn._user_home.startswith("/home/")

        wshandler.state.sessions.pop(workspace["id"], None)
        container.registry.states.pop(workspace["id"], None)

    async def test_resolves_existing_handle(self, user):
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock)
        workspace = await ws_mod.create_workspace(user["id"], "handle-ws")
        # Set a custom handle in the DB
        await model.set_user_handle(user["id"], "chris")

        async def fake_start(*a, **kw):
            container.registry.track_activity("cid-h", workspace["id"])
            return ("cid-h", "created")

        with (
            patch.object(
                container.registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch("glob.glob", return_value=[]),
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        assert conn._user_home == "/home/chris"

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

    async def test_dispatch_terminal_window_commands(self, user):
        for cmd in (
            "terminal_new_window",
            "terminal_select_window",
            "terminal_close_window",
            "terminal_rename_window",
            "terminal_list_windows",
        ):
            websocket = await self._run_commands(user, [{"cmd": cmd}])
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

    async def test_dispatch_set_handle(self, user):
        websocket = await self._run_commands(
            user, [{"cmd": "set_handle", "handle": "alice"}]
        )
        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_dispatch_workspace_disconnect(self, user):
        websocket = await self._run_commands(
            user, [{"cmd": "workspace_disconnect"}]
        )
        websocket.accept.assert_awaited_once()

    async def test_dispatch_browser_reattach(self, user):
        websocket = await self._run_commands(
            user, [{"cmd": "browser_reattach", "browser_id": "bid-x"}]
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
            self_arg._user_home = "/home/testuser"
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


class TestChatLoadMoreDispatch:
    async def test_dispatch_chat_load_more(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps(
                    {"cmd": "chat_load_more", "before_id": "x", "limit": 10}
                ),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_chat_load_more", new_callable=AsyncMock
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


class TestCleanupRevokesBrowser:
    async def test_cleanup_revokes_browser_registration(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.workspace_id = "ws-revoke"
        conn.container_id = "cid-revoke"

        # Register a browser ID for this connection
        container.registry.register_browser("bid-revoke", "ws-revoke", sock)
        conn._browser_id = "bid-revoke"

        container.registry.track_activity("cid-revoke", "ws-revoke")
        session = WorkspaceSession("ws-revoke")
        session.subscribers.add(sock)
        wshandler.state.sessions["ws-revoke"] = session

        await conn.cleanup()

        assert container.registry.resolve_browser("bid-revoke") is None
        assert conn._browser_id is None

        container.registry.revoke_workspace_browsers("ws-revoke")
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

    async def test_reset_cleans_agent_state(self):
        """reset_workspace removes agent conversations and cancels agent tasks."""
        ws_id = "ws-agent-cleanup"
        wshandler._agent_conversations[ws_id] = {"user_id": "u1"}

        async def noop():
            await asyncio.sleep(999)

        task = asyncio.create_task(noop())
        wshandler._agent_tasks[ws_id] = task
        try:
            await reset_workspace_state(ws_id)
            assert ws_id not in wshandler._agent_conversations
            assert ws_id not in wshandler._agent_tasks
            # Let cancellation propagate
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert task.cancelled()
        finally:
            wshandler._agent_conversations.pop(ws_id, None)
            wshandler._agent_tasks.pop(ws_id, None)
            if not task.done():
                task.cancel()


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
        with patch.object(
            model,
            "add_chat_message",
            new_callable=AsyncMock,
            return_value={"id": "fake", "message": "left", "message_type": 2},
        ):
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

    async def test_restart_updates_other_connections_container_id(self, user):
        sock1 = _mock_sock(headers={"host": "localhost:8997"})
        sock2 = _mock_sock()
        workspace = await ws_mod.create_workspace(user["id"], "restart-cid")
        conn1 = _base_conn(user=user, ws=sock1)
        conn2 = _base_conn(user=user, ws=sock2)
        conn1.workspace_id = workspace["id"]
        conn1.container_id = "old-cid"
        conn1.workspace = workspace
        conn2.workspace_id = workspace["id"]
        conn2.container_id = "old-cid"

        wshandler.state.connections[sock1] = conn1
        wshandler.state.connections[sock2] = conn2

        async def fake_start(self_arg, wid, ws_obj):
            self_arg.container_id = "new-cid"
            self_arg.workspace_id = wid
            wshandler.state.get_or_create_session(wid)

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                autospec=True,
                side_effect=fake_start,
            ),
            patch.object(container.registry, "record_activity"),
            patch.object(
                container.registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            await conn1.handle_restart_container()

        assert conn2.container_id == "new-cid"

        wshandler.state.connections.pop(sock1, None)
        wshandler.state.connections.pop(sock2, None)
        wshandler.state.sessions.pop(workspace["id"], None)


class TestHandleShutdownContainer:
    async def test_shutdown_not_connected(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_shutdown_container()
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and "Not connected" in m.get("message", "")
            for m in sent
        )

    async def test_shutdown_no_container(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.workspace_id = "ws"
        conn.container_id = None
        await conn.handle_shutdown_container()
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and "No container" in m.get("message", "")
            for m in sent
        )

    async def test_shutdown_broadcasts_stopped(self, user):
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        conn = _base_conn(user=user, ws=sock1)
        ws = await _create_workspace_with_acl(user["id"], "shutdown-ws")
        conn.workspace_id = ws["id"]
        conn.container_id = "cid"

        session = wshandler.state.get_or_create_session(ws["id"])
        await session.add_subscriber(sock1, "cid")
        await session.add_subscriber(sock2, "cid")

        with patch.object(
            container.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            await conn.handle_shutdown_container()

        # Both subscribers should receive container_stopped
        for sock in (sock1, sock2):
            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                isinstance(m, dict)
                and m.get("type") == "event"
                and m.get("event", {}).get("name") == "container_stopped"
                and "shut down"
                in m.get("event", {}).get("value", {}).get("reason", "")
                for m in sent
            ), "container_stopped not sent to subscriber"

        wshandler.state.sessions.pop(ws["id"], None)

    async def test_shutdown_clears_other_connections_container_id(self, user):
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        conn1 = _base_conn(user=user, ws=sock1)
        conn2 = _base_conn(user=user, ws=sock2)
        ws = await _create_workspace_with_acl(user["id"], "shutdown-cid")
        conn1.workspace_id = ws["id"]
        conn1.container_id = "old-cid"
        conn2.workspace_id = ws["id"]
        conn2.container_id = "old-cid"

        session = wshandler.state.get_or_create_session(ws["id"])
        await session.add_subscriber(sock1, "old-cid")
        await session.add_subscriber(sock2, "old-cid")
        wshandler.state.connections[sock1] = conn1
        wshandler.state.connections[sock2] = conn2

        with patch.object(
            container.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            await conn1.handle_shutdown_container()

        assert conn2.container_id is None

        wshandler.state.connections.pop(sock1, None)
        wshandler.state.connections.pop(sock2, None)
        wshandler.state.sessions.pop(ws["id"], None)

    async def test_shutdown_saves_terminal_state(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        ws = await _create_workspace_with_acl(user["id"], "shutdown-save")
        conn.workspace_id = ws["id"]
        conn.container_id = "cid"

        session = wshandler.state.get_or_create_session(ws["id"])
        session.terminal_windows[user["id"]] = [
            {"name": "bash", "index": 0, "id": "@0", "shared": False},
        ]
        await session.add_subscriber(sock, "cid")

        with (
            patch.object(
                container.registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
            patch(
                "klangk_backend.terminal.save_workspace_state",
                new_callable=AsyncMock,
            ) as mock_save,
        ):
            await conn.handle_shutdown_container()

        mock_save.assert_awaited_once()
        saved_snapshot = mock_save.call_args[0][1]
        assert user["id"] in saved_snapshot
        wshandler.state.sessions.pop(ws["id"], None)

    async def test_shutdown_state_save_failure_does_not_block(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        ws = await _create_workspace_with_acl(user["id"], "shutdown-savefail")
        conn.workspace_id = ws["id"]
        conn.container_id = "cid"

        session = wshandler.state.get_or_create_session(ws["id"])
        session.terminal_windows[user["id"]] = [
            {"name": "bash", "index": 0, "id": "@0", "shared": False},
        ]
        await session.add_subscriber(sock, "cid")

        with (
            patch.object(
                container.registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
            patch(
                "klangk_backend.terminal.save_workspace_state",
                new_callable=AsyncMock,
                side_effect=RuntimeError("write failed"),
            ),
        ):
            await conn.handle_shutdown_container()

        # Should not raise; container_stopped event still sent
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict)
            and m.get("event", {}).get("name") == "container_stopped"
            for m in sent
        )
        wshandler.state.sessions.pop(ws["id"], None)

    async def test_shutdown_handles_stop_error(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        ws = await _create_workspace_with_acl(user["id"], "shutdown-err")
        conn.workspace_id = ws["id"]
        conn.container_id = "cid"

        session = wshandler.state.get_or_create_session(ws["id"])
        await session.add_subscriber(sock, "cid")

        with patch.object(
            container.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
            side_effect=RuntimeError("podman broke"),
        ):
            await conn.handle_shutdown_container()

        # Should not raise; container_stopped event still sent
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict)
            and m.get("event", {}).get("name") == "container_stopped"
            for m in sent
        )
        wshandler.state.sessions.pop(ws["id"], None)

    async def test_shutdown_dispatch(self, user):
        from klangk_backend import auth as auth_mod

        token = auth_mod.create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "shutdown_container"}),
                WebSocketDisconnect(),
            ]
        )
        await handle_websocket(websocket)
        sent = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and "Not connected" in str(m) for m in sent
        )


class TestTerminalWindowHandlers:
    async def test_new_window_no_container(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = None
        await conn.handle_terminal_new_window({})
        assert sock.send_json.call_count == 0

    async def test_new_window_success(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch(
            "klangk_backend.terminal.new_window",
            return_value=[
                {"id": "@0", "index": 0, "name": "bash", "active": False},
                {"id": "@1", "index": 1, "name": "bash", "active": True},
            ],
        ):
            await conn.handle_terminal_new_window({})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "terminal_windows"
        assert len(sent["windows"]) == 2

    async def test_new_window_with_name(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch(
            "klangk_backend.terminal.new_window",
            return_value=[
                {"id": "@0", "index": 0, "name": "bash", "active": False},
                {"id": "@1", "index": 1, "name": "build", "active": True},
            ],
        ) as mock_new:
            await conn.handle_terminal_new_window({"name": "build"})
        mock_new.assert_called_once_with("cid", "uid", name="build")

    async def test_new_window_error(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch(
            "klangk_backend.terminal.new_window",
            side_effect=ValueError("already exists"),
        ):
            await conn.handle_terminal_new_window({"name": "dup"})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"

    async def test_select_window(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch(
            "klangk_backend.terminal.select_window",
        ) as mock_sel:
            await conn.handle_terminal_select_window({"index": 2})
        mock_sel.assert_called_once_with("cid", "uid", 2)

    async def test_select_window_error(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch(
            "klangk_backend.terminal.select_window",
            side_effect=RuntimeError("no such window"),
        ):
            await conn.handle_terminal_select_window({"index": 99})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"

    async def test_close_window(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch(
            "klangk_backend.terminal.close_window",
            return_value=[
                {"id": "@0", "index": 0, "name": "bash", "active": True}
            ],
        ):
            await conn.handle_terminal_close_window({"index": 1})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "terminal_windows"

    async def test_close_shared_window_broadcasts(self, user):
        """Closing a shared window broadcasts updated shared_terminals."""
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[user["id"]] = [
            {"name": "bash", "index": 0, "id": "@0", "shared": True},
            {"name": "1", "index": 1, "id": "@1", "shared": False},
        ]
        await session.add_subscriber(sock, "cid")
        wshandler.state.connections[sock] = conn
        try:
            with patch(
                "klangk_backend.terminal.close_window",
                return_value=[
                    {"id": "@1", "index": 1, "name": "1", "active": True}
                ],
            ):
                await conn.handle_terminal_close_window({"index": 0})
            # shared "bash" was removed — broadcast should have fired
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            shared_msgs = [
                c for c in calls if c.get("type") == "shared_terminals"
            ]
            assert len(shared_msgs) >= 1
            # The remaining window "1" is not shared
            assert shared_msgs[-1]["terminals"] == []
        finally:
            wshandler.state.sessions.pop("ws-1", None)
            wshandler.state.connections.pop(sock, None)

    async def test_close_window_error(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch(
            "klangk_backend.terminal.close_window",
            side_effect=RuntimeError("no such window"),
        ):
            await conn.handle_terminal_close_window({"index": 99})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"

    async def test_rename_window(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with (
            patch(
                "klangk_backend.terminal.rename_window",
            ),
            patch(
                "klangk_backend.terminal.list_windows",
                return_value=[
                    {"id": "@0", "index": 0, "name": "build", "active": True}
                ],
            ),
        ):
            await conn.handle_terminal_rename_window(
                {"index": 0, "name": "build"}
            )
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "terminal_windows"

    async def test_rename_window_no_name(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        await conn.handle_terminal_rename_window({"index": 0})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"
        assert "Name" in sent["message"]

    async def test_rename_window_error(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch(
            "klangk_backend.terminal.rename_window",
            side_effect=ValueError("already exists"),
        ):
            await conn.handle_terminal_rename_window(
                {"index": 0, "name": "dup"}
            )
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"

    async def test_list_windows(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch(
            "klangk_backend.terminal.list_windows",
            return_value=[
                {"id": "@0", "index": 0, "name": "bash", "active": True}
            ],
        ):
            await conn.handle_terminal_list_windows()
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "terminal_windows"
        assert len(sent["windows"]) == 1

    async def test_list_windows_error(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch(
            "klangk_backend.terminal.list_windows",
            side_effect=RuntimeError("tmux not running"),
        ):
            await conn.handle_terminal_list_windows()
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"


class TestShareWindowHandlers:
    """Tests for the unified share/unshare/join terminal handlers."""

    async def test_share_window_broadcasts(self, user, temp_data_dir):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": False},
            {"name": "2", "index": 1, "id": "@1", "shared": False},
        ]
        await session.add_subscriber(sock, "cid")
        try:
            with patch(
                "klangk_backend.acl.check_permission", return_value=True
            ):
                await conn.handle_share_window({"window_id": "@1"})
            assert session.terminal_windows[user["id"]][1]["shared"] is True
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            shared_msgs = [
                c for c in calls if c.get("type") == "shared_terminals"
            ]
            assert len(shared_msgs) >= 1
        finally:
            wshandler.state.sessions.pop("ws-1", None)

    async def test_share_window_permission_denied(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        with patch("klangk_backend.acl.check_permission", return_value=False):
            await conn.handle_share_window({"window_id": "@0"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission" in c.get("message", "") for c in calls)

    async def test_unshare_window_kicks_joiners(self, user, temp_data_dir):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        try:
            with (
                patch(
                    "klangk_backend.acl.check_permission", return_value=True
                ),
                patch(
                    "klangk_backend.terminal.kill_joiner_sessions"
                ) as mock_kill,
            ):
                await conn.handle_unshare_window({"window_id": "@0"})
            assert session.terminal_windows[user["id"]][0]["shared"] is False
            mock_kill.assert_awaited_once()
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            deleted = [
                c for c in calls if c.get("type") == "shared_terminal_deleted"
            ]
            assert len(deleted) == 1
        finally:
            wshandler.state.sessions.pop("ws-1", None)

    async def test_list_shared_terminals(self, user, temp_data_dir):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": False},
            {"name": "build", "index": 1, "id": "@1", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        wshandler.state.connections[sock] = conn
        try:
            with patch(
                "klangk_backend.acl.check_permission", return_value=True
            ):
                await conn.handle_list_shared_terminals()
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            shared = [c for c in calls if c.get("type") == "shared_terminals"]
            assert len(shared) == 1
            terminals = shared[0]["terminals"]
            assert len(terminals) == 1
            assert terminals[0]["window_name"] == "build"
            assert terminals[0]["user_id"] == user["id"]
            assert terminals[0]["handle"] == user["handle"]
        finally:
            wshandler.state.sessions.pop("ws-1", None)
            wshandler.state.connections.pop(sock, None)

    async def test_shared_terminals_include_viewers(self, user, temp_data_dir):
        """shared_terminals response includes viewer list."""
        owner_sock = _mock_sock()
        owner_conn = _base_conn(user=user, ws=owner_sock)
        owner_conn.workspace_id = "ws-v"
        owner_conn.container_id = "cid"
        owner_conn._user_home = "/home/admin"

        viewer_user = {
            "id": "viewer-1",
            "email": "viewer@test.com",
            "handle": "viewer",
        }
        viewer_sock = _mock_sock()
        viewer_conn = _base_conn(user=viewer_user, ws=viewer_sock)
        viewer_conn.workspace_id = "ws-v"
        viewer_conn._viewing_shared = {
            "user_id": user["id"],
            "window_id": "@0",
        }

        session = wshandler.state.get_or_create_session("ws-v")
        session.terminal_windows[user["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(owner_sock, "cid")
        await session.add_subscriber(viewer_sock, "cid")
        wshandler.state.connections[owner_sock] = owner_conn
        wshandler.state.connections[viewer_sock] = viewer_conn
        try:
            with patch(
                "klangk_backend.acl.check_permission", return_value=True
            ):
                await owner_conn.handle_list_shared_terminals()
            calls = [c[0][0] for c in owner_sock.send_json.call_args_list]
            shared = [c for c in calls if c.get("type") == "shared_terminals"]
            assert len(shared) == 1
            terminal = shared[0]["terminals"][0]
            assert len(terminal["viewers"]) == 1
            assert terminal["viewers"][0]["user_id"] == "viewer-1"
            assert terminal["viewers"][0]["email"] == "viewer@test.com"
        finally:
            wshandler.state.sessions.pop("ws-v", None)
            wshandler.state.connections.pop(owner_sock, None)
            wshandler.state.connections.pop(viewer_sock, None)

    async def test_stop_terminal_broadcasts_viewer_change(self, user):
        """Stopping a terminal that was viewing shared broadcasts update."""
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-sv"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn._viewing_shared = {"user_id": "owner-1", "window_id": "@0"}

        session = wshandler.state.get_or_create_session("ws-sv")
        await session.add_subscriber(sock, "cid")
        wshandler.state.connections[sock] = conn
        try:
            await conn.stop_terminal()
            assert conn._viewing_shared is None
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            shared = [c for c in calls if c.get("type") == "shared_terminals"]
            assert len(shared) == 1
        finally:
            wshandler.state.sessions.pop("ws-sv", None)
            wshandler.state.connections.pop(sock, None)

    async def test_create_shared_terminal_legacy(self, user, temp_data_dir):
        """Legacy create_shared_terminal creates a window and marks it shared."""
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": False}
        ]
        await session.add_subscriber(sock, "cid")
        wshandler.state.connections[sock] = conn
        try:
            with (
                patch(
                    "klangk_backend.acl.check_permission", return_value=True
                ),
                patch(
                    "klangk_backend.terminal.new_window",
                    return_value=[
                        {"id": "@0", "index": 0, "name": "1", "active": False},
                        {
                            "id": "@1",
                            "index": 1,
                            "name": "dev",
                            "active": True,
                        },
                    ],
                ),
            ):
                await conn.handle_create_shared_terminal({"name": "dev"})
            windows = session.terminal_windows[user["id"]]
            assert len(windows) == 2
            dev = next(w for w in windows if w["name"] == "dev")
            assert dev["shared"] is True
            assert dev["id"] == "@1"
            orig = next(w for w in windows if w["name"] == "1")
            assert orig["shared"] is False
        finally:
            wshandler.state.sessions.pop("ws-1", None)
            wshandler.state.connections.pop(sock, None)

    async def test_share_window_no_container(self, user):
        conn = _base_conn(user=user)
        await conn.handle_share_window({"window_id": "@0"})
        # No error sent — early return

    async def test_share_window_missing_id(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        with patch("klangk_backend.acl.check_permission", return_value=True):
            await conn.handle_share_window({})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("id" in c.get("message", "").lower() for c in calls)

    async def test_share_window_not_found(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": False}
        ]
        try:
            with patch(
                "klangk_backend.acl.check_permission", return_value=True
            ):
                await conn.handle_share_window({"window_id": "@99"})
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                "not found" in c.get("message", "").lower() for c in calls
            )
        finally:
            wshandler.state.sessions.pop("ws-1", None)

    async def test_share_window_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "no-session-ws"
        with patch("klangk_backend.acl.check_permission", return_value=True):
            await conn.handle_share_window({"window_id": "@0"})
        # No error — early return

    async def test_unshare_window_no_container(self, user):
        conn = _base_conn(user=user)
        await conn.handle_unshare_window({"window_id": "@0"})

    async def test_unshare_window_permission_denied(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        with patch("klangk_backend.acl.check_permission", return_value=False):
            await conn.handle_unshare_window({"window_id": "@0"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission" in c.get("message", "") for c in calls)

    async def test_unshare_window_missing_id(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        with patch("klangk_backend.acl.check_permission", return_value=True):
            await conn.handle_unshare_window({})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("id" in c.get("message", "").lower() for c in calls)

    async def test_unshare_window_not_found(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": True}
        ]
        try:
            with patch(
                "klangk_backend.acl.check_permission", return_value=True
            ):
                await conn.handle_unshare_window({"window_id": "@99"})
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                "not found" in c.get("message", "").lower() for c in calls
            )
        finally:
            wshandler.state.sessions.pop("ws-1", None)

    async def test_unshare_window_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "no-session-ws"
        with patch("klangk_backend.acl.check_permission", return_value=True):
            await conn.handle_unshare_window({"window_id": "@0"})

    async def test_unshare_kill_error_handled(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": True}
        ]
        await session.add_subscriber(sock, "cid")
        try:
            with (
                patch(
                    "klangk_backend.acl.check_permission", return_value=True
                ),
                patch(
                    "klangk_backend.terminal.kill_joiner_sessions",
                    side_effect=RuntimeError("no sessions"),
                ),
            ):
                await conn.handle_unshare_window({"window_id": "@0"})
            assert session.terminal_windows[user["id"]][0]["shared"] is False
        finally:
            wshandler.state.sessions.pop("ws-1", None)

    async def test_join_shared_terminal(self, user, temp_data_dir):
        owner = await model.create_user(
            "owner@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        container.registry.track_activity("cid", "ws-1")
        try:
            with (
                patch(
                    "klangk_backend.acl.check_permission", return_value=True
                ),
                patch.object(wshandler, "TerminalSession") as MockTS,
                patch("klangk_backend.terminal.select_window"),
                patch("klangk_backend.terminal.tmux_command", return_value=""),
            ):
                mock_sess = _mock_terminal()
                MockTS.return_value = mock_sess

                async def fake_output():
                    return
                    yield

                mock_sess.output = fake_output

                await conn.handle_join_shared_terminal(
                    {"user_id": owner["id"], "window_id": "@0"}
                )
                await asyncio.sleep(0)

            MockTS.assert_called_once()
            call_kwargs = MockTS.call_args[1]
            assert call_kwargs["join_session"] == owner["id"]
            # Verify terminal_started was sent with shared info
            sent = [c[0][0] for c in sock.send_json.call_args_list]
            started = [s for s in sent if s.get("type") == "terminal_started"]
            assert len(started) == 1
            assert started[0]["shared_user_id"] == owner["id"]
            assert started[0]["shared_window"] == "build"
        finally:
            wshandler.state.sessions.pop("ws-1", None)
            container.registry.states.pop("ws-1", None)

    async def test_join_shared_terminal_no_container(self, user):
        conn = _base_conn(user=user)
        await conn.handle_join_shared_terminal(
            {"user_id": "x", "window_id": "@99"}
        )

    async def test_join_shared_terminal_permission_denied(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/x"
        with patch("klangk_backend.acl.check_permission", return_value=False):
            await conn.handle_join_shared_terminal(
                {"user_id": "x", "window_id": "@99"}
            )
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission" in c.get("message", "") for c in calls)

    async def test_join_shared_terminal_missing_fields(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/x"
        conn.workspace_id = "ws-1"
        with patch("klangk_backend.acl.check_permission", return_value=True):
            await conn.handle_join_shared_terminal({})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("required" in c.get("message", "").lower() for c in calls)

    async def test_join_shared_terminal_superseded(self, user, temp_data_dir):
        """If session is superseded during start, _activate_session returns False."""
        owner = await model.create_user(
            "owner-sup@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        container.registry.track_activity("cid", "ws-1")

        async def fake_start(*a, **kw):
            # Supersede the session before _activate_session runs
            conn.terminal_session = None

        try:
            with (
                patch(
                    "klangk_backend.acl.check_permission", return_value=True
                ),
                patch.object(wshandler, "TerminalSession") as MockTS,
                patch(
                    "klangk_backend.terminal.tmux_command",
                    new_callable=AsyncMock,
                ),
            ):
                mock_sess = _mock_terminal()
                mock_sess.start = AsyncMock(side_effect=fake_start)
                MockTS.return_value = mock_sess

                await conn.handle_join_shared_terminal(
                    {"user_id": owner["id"], "window_id": "@0"}
                )
                await asyncio.sleep(0)

            # Session was stopped because it was superseded
            mock_sess.stop.assert_awaited()
        finally:
            wshandler.state.sessions.pop("ws-1", None)
            container.registry.states.pop("ws-1", None)

    async def test_join_shared_terminal_select_fallback(
        self, user, temp_data_dir
    ):
        """Falls back to bare @N when joiner session select fails."""
        owner = await model.create_user(
            "owner-fb@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        wshandler.state.connections[sock] = conn
        container.registry.track_activity("cid", "ws-1")

        try:
            with (
                patch(
                    "klangk_backend.acl.check_permission", return_value=True
                ),
                patch.object(wshandler, "TerminalSession") as MockTS,
                patch(
                    "klangk_backend.terminal.tmux_command",
                    new_callable=AsyncMock,
                    side_effect=RuntimeError("can't find session"),
                ),
                patch(
                    "klangk_backend.terminal.select_window",
                    new_callable=AsyncMock,
                ) as mock_select,
            ):
                mock_sess = _mock_terminal()
                mock_sess._tmux_session_name = "joiner-abc"

                async def fake_output():
                    return
                    yield  # make it an async generator

                mock_sess.output = fake_output
                MockTS.return_value = mock_sess

                await conn.handle_join_shared_terminal(
                    {"user_id": owner["id"], "window_id": "@0"}
                )
                await asyncio.sleep(0)

            # Fell back to select_window with bare @N
            mock_select.assert_awaited_once_with("cid", owner["id"], "@0")
        finally:
            wshandler.state.sessions.pop("ws-1", None)
            wshandler.state.connections.pop(sock, None)
            container.registry.states.pop("ws-1", None)

    async def test_join_shared_terminal_no_joiner_session(
        self, user, temp_data_dir
    ):
        """Falls back to bare @N when joiner session name is None."""
        owner = await model.create_user(
            "owner-nj@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        wshandler.state.connections[sock] = conn
        container.registry.track_activity("cid", "ws-1")

        try:
            with (
                patch(
                    "klangk_backend.acl.check_permission", return_value=True
                ),
                patch.object(wshandler, "TerminalSession") as MockTS,
                patch(
                    "klangk_backend.terminal.select_window",
                    new_callable=AsyncMock,
                ) as mock_select,
            ):
                mock_sess = _mock_terminal()
                # No joiner session name
                mock_sess._tmux_session_name = None

                async def fake_output():
                    return
                    yield

                mock_sess.output = fake_output
                MockTS.return_value = mock_sess

                await conn.handle_join_shared_terminal(
                    {"user_id": owner["id"], "window_id": "@0"}
                )
                await asyncio.sleep(0)

            mock_select.assert_awaited_once_with("cid", owner["id"], "@0")
        finally:
            wshandler.state.sessions.pop("ws-1", None)
            wshandler.state.connections.pop(sock, None)
            container.registry.states.pop("ws-1", None)

    async def test_join_shared_terminal_start_error(self, user, temp_data_dir):
        """If session.start() fails, error is sent and session stopped."""
        owner = await model.create_user(
            "owner-err@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        try:
            with (
                patch(
                    "klangk_backend.acl.check_permission", return_value=True
                ),
                patch.object(wshandler, "TerminalSession") as MockTS,
            ):
                mock_sess = _mock_terminal()
                mock_sess.start = AsyncMock(
                    side_effect=RuntimeError("start failed")
                )
                MockTS.return_value = mock_sess

                await conn.handle_join_shared_terminal(
                    {"user_id": owner["id"], "window_id": "@0"}
                )
                await asyncio.sleep(0)

            mock_sess.stop.assert_awaited()
            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert any("Failed" in c.get("message", "") for c in sent)
        finally:
            wshandler.state.sessions.pop("ws-1", None)

    async def test_join_shared_terminal_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/x"
        conn.workspace_id = "no-session"
        with patch("klangk_backend.acl.check_permission", return_value=True):
            await conn.handle_join_shared_terminal(
                {"user_id": "x", "window_id": "@99"}
            )
        # Early return, no error sent

    async def test_join_shared_terminal_not_found(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/x"
        conn.workspace_id = "ws-1"
        wshandler.state.get_or_create_session("ws-1")
        try:
            with patch(
                "klangk_backend.acl.check_permission", return_value=True
            ):
                await conn.handle_join_shared_terminal(
                    {"user_id": "nobody", "window_id": "@99"}
                )
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                "not found" in c.get("message", "").lower() for c in calls
            )
        finally:
            wshandler.state.sessions.pop("ws-1", None)

    async def test_delete_shared_terminal(self, user, temp_data_dir):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": False},
            {"name": "build", "index": 1, "id": "@1", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        wshandler.state.connections[sock] = conn
        try:
            with (
                patch(
                    "klangk_backend.acl.check_permission", return_value=True
                ),
                patch("klangk_backend.terminal.kill_joiner_sessions"),
                patch("klangk_backend.terminal.close_window", return_value=[]),
            ):
                await conn.handle_delete_shared_terminal(
                    {"user_id": user["id"], "window_id": "@1"}
                )
            windows = session.terminal_windows[user["id"]]
            assert len(windows) == 1
            assert windows[0]["name"] == "1"
        finally:
            wshandler.state.sessions.pop("ws-1", None)
            wshandler.state.connections.pop(sock, None)

    async def test_delete_shared_terminal_no_container(self, user):
        conn = _base_conn(user=user)
        await conn.handle_delete_shared_terminal(
            {"user_id": "x", "window_id": "@99"}
        )

    async def test_delete_shared_terminal_permission_denied(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        with patch("klangk_backend.acl.check_permission", return_value=False):
            await conn.handle_delete_shared_terminal(
                {"user_id": "x", "window_id": "@99"}
            )
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission" in c.get("message", "") for c in calls)

    async def test_delete_shared_terminal_missing_fields(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws-1"
        with patch("klangk_backend.acl.check_permission", return_value=True):
            await conn.handle_delete_shared_terminal({})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("required" in c.get("message", "").lower() for c in calls)

    async def test_delete_shared_terminal_not_found(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws-1"
        wshandler.state.get_or_create_session("ws-1")
        try:
            with patch(
                "klangk_backend.acl.check_permission", return_value=True
            ):
                await conn.handle_delete_shared_terminal(
                    {"user_id": "nobody", "window_id": "@99"}
                )
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                "not found" in c.get("message", "").lower() for c in calls
            )
        finally:
            wshandler.state.sessions.pop("ws-1", None)

    async def test_delete_shared_terminal_error(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        session = wshandler.state.get_or_create_session("ws-1")
        session.terminal_windows[user["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        try:
            with (
                patch(
                    "klangk_backend.acl.check_permission", return_value=True
                ),
                patch(
                    "klangk_backend.terminal.kill_joiner_sessions",
                    side_effect=RuntimeError("boom"),
                ),
            ):
                await conn.handle_delete_shared_terminal(
                    {"user_id": user["id"], "window_id": "@0"}
                )
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any("Failed" in c.get("message", "") for c in calls)
        finally:
            wshandler.state.sessions.pop("ws-1", None)

    async def test_create_shared_terminal_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "no-session"
        with (
            patch("klangk_backend.acl.check_permission", return_value=True),
            patch("klangk_backend.terminal.new_window", return_value=[]),
        ):
            await conn.handle_create_shared_terminal({"name": "dev"})
        # Early return after new_window — no crash

    async def test_delete_shared_terminal_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "no-session"
        with patch("klangk_backend.acl.check_permission", return_value=True):
            await conn.handle_delete_shared_terminal(
                {"user_id": "x", "window_id": "@99"}
            )
        # Early return — no crash

    async def test_create_shared_terminal_no_container(self, user):
        conn = _base_conn(user=user)
        await conn.handle_create_shared_terminal({"name": "x"})

    async def test_create_shared_terminal_permission_denied(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        with patch("klangk_backend.acl.check_permission", return_value=False):
            await conn.handle_create_shared_terminal({"name": "x"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission" in c.get("message", "") for c in calls)

    async def test_create_shared_terminal_empty_name(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        with patch("klangk_backend.acl.check_permission", return_value=True):
            await conn.handle_create_shared_terminal({"name": ""})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Name" in c.get("message", "") for c in calls)

    async def test_create_shared_terminal_error(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        with (
            patch("klangk_backend.acl.check_permission", return_value=True),
            patch(
                "klangk_backend.terminal.new_window",
                side_effect=RuntimeError("fail"),
            ),
        ):
            await conn.handle_create_shared_terminal({"name": "dev"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Failed" in c.get("message", "") for c in calls)

    async def test_list_shared_terminals_no_workspace(self):
        conn = _base_conn()
        await conn.handle_list_shared_terminals()

    async def test_list_shared_terminals_permission_denied(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        with patch("klangk_backend.acl.check_permission", return_value=False):
            await conn.handle_list_shared_terminals()
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission" in c.get("message", "") for c in calls)

    async def test_list_shared_terminals_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "no-session"
        with patch("klangk_backend.acl.check_permission", return_value=True):
            await conn.handle_list_shared_terminals()
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        shared = [c for c in calls if c.get("type") == "shared_terminals"]
        assert shared[0]["terminals"] == []

    async def test_has_perm_checks_acl(self, user, temp_data_dir):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        ws = await _create_workspace_with_acl(user["id"], "perm-ws")
        conn.workspace_id = ws["id"]
        assert await conn._has_perm("view")

    async def test_has_perm_no_workspace(self):
        conn = _base_conn()
        assert not await conn._has_perm("view")


class TestFractionalTimeout:
    async def test_fractional_timeout_display(
        self, user, monkeypatch, agent_user
    ):
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


class TestMentionsAgent:
    async def test_detects_mention(self, agent_user):
        from klangk_backend.wshandler import _mentions_agent

        assert await _mentions_agent("@MrBoops hello")
        assert await _mentions_agent("hey @MrBoops what's up")
        assert await _mentions_agent("@MRBOOPS help")
        assert await _mentions_agent("@MrBoops@klangk.local hello")
        assert await _mentions_agent("hey @MrBoops@klangk.local")

    async def test_no_false_positives(self, agent_user):
        from klangk_backend.wshandler import _mentions_agent

        assert not await _mentions_agent("hello everyone")
        assert not await _mentions_agent("@someone else")
        assert not await _mentions_agent("MrBoops without at sign")
        assert not await _mentions_agent("@MrBoopsy partial match")


class TestAddressesOtherUser:
    async def test_starts_with_other_mention(self, agent_user):
        from klangk_backend.wshandler import _addresses_other_user

        assert await _addresses_other_user("@bob hello")
        assert await _addresses_other_user(
            "@alice@test.com what do you think?"
        )

    async def test_starts_with_agent_mention(self, agent_user):
        from klangk_backend.wshandler import _addresses_other_user

        assert not await _addresses_other_user("@MrBoops hello")
        assert not await _addresses_other_user("@MRBOOPS help")

    async def test_no_mention(self, agent_user):
        from klangk_backend.wshandler import _addresses_other_user

        assert not await _addresses_other_user("hello everyone")

    async def test_mention_in_middle(self, agent_user):
        from klangk_backend.wshandler import _addresses_other_user

        assert not await _addresses_other_user("I think @bob is right")


class TestChatFollowUp:
    async def test_same_user_no_interjection(
        self, workspace, user, agent_user
    ):
        """Same user's follow-up routes without timer."""
        from klangk_backend.wshandler import state, _agent_conversations

        mock_session = AsyncMock()
        mock_session.send_prompt = AsyncMock(return_value="reply")

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = state.get_or_create_session(workspace["id"])
        await session.add_subscriber(sock, "cid")
        try:
            _agent_conversations[workspace["id"]] = {
                "user_id": user["id"],
                "time": time.monotonic(),
                "interjected": False,
            }
            with patch(
                "klangk_backend.agent.get_session",
                return_value=mock_session,
            ):
                await conn.handle_chat_send({"message": "what about now?"})
                await asyncio.sleep(0.1)
            agent_msgs = [
                c[0][0]
                for c in sock.send_json.call_args_list
                if c[0][0].get("type") == "chat_message"
                and c[0][0].get("message_type") == 1
            ]
            assert len(agent_msgs) == 1
        finally:
            _agent_conversations.pop(workspace["id"], None)
            await session.remove_subscriber(sock)

    async def test_interjection_within_window(
        self, workspace, user, agent_user
    ):
        """After interjection, follow-up within 30s still routes."""
        from klangk_backend.wshandler import state, _agent_conversations

        mock_session = AsyncMock()
        mock_session.send_prompt = AsyncMock(return_value="reply")

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = state.get_or_create_session(workspace["id"])
        await session.add_subscriber(sock, "cid")
        try:
            _agent_conversations[workspace["id"]] = {
                "user_id": user["id"],
                "time": time.monotonic(),
                "interjected": True,
            }
            with patch(
                "klangk_backend.agent.get_session",
                return_value=mock_session,
            ):
                await conn.handle_chat_send({"message": "still here?"})
                await asyncio.sleep(0.1)
            agent_msgs = [
                c[0][0]
                for c in sock.send_json.call_args_list
                if c[0][0].get("type") == "chat_message"
                and c[0][0].get("message_type") == 1
            ]
            assert len(agent_msgs) == 1
        finally:
            _agent_conversations.pop(workspace["id"], None)
            await session.remove_subscriber(sock)

    async def test_interjection_expired(self, workspace, user, agent_user):
        """After interjection + 30s, follow-up does NOT route."""
        from klangk_backend.wshandler import state, _agent_conversations

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = state.get_or_create_session(workspace["id"])
        await session.add_subscriber(sock, "cid")
        try:
            _agent_conversations[workspace["id"]] = {
                "user_id": user["id"],
                "time": time.monotonic() - 60,
                "interjected": True,
            }
            await conn.handle_chat_send({"message": "hello?"})
            await asyncio.sleep(0.1)
            agent_msgs = [
                c[0][0]
                for c in sock.send_json.call_args_list
                if c[0][0].get("type") == "chat_message"
                and c[0][0].get("message_type") == 1
            ]
            assert len(agent_msgs) == 0
        finally:
            _agent_conversations.pop(workspace["id"], None)
            await session.remove_subscriber(sock)

    async def test_different_user_marks_interjection(
        self, workspace, user, agent_user
    ):
        """A different user's message marks interjection."""
        from klangk_backend.wshandler import state, _agent_conversations

        sock = _mock_sock()
        other_user = {"id": "other-uid", "email": "other@test.com"}
        conn = _base_conn(user=other_user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = state.get_or_create_session(workspace["id"])
        await session.add_subscriber(sock, "cid")
        try:
            _agent_conversations[workspace["id"]] = {
                "user_id": user["id"],
                "time": time.monotonic(),
                "interjected": False,
            }
            await conn.handle_chat_send({"message": "hey everyone"})
            await asyncio.sleep(0.1)
            assert _agent_conversations[workspace["id"]]["interjected"]
        finally:
            _agent_conversations.pop(workspace["id"], None)
            await session.remove_subscriber(sock)

    async def test_addressed_to_other_breaks(
        self, workspace, user, agent_user
    ):
        """Message starting with @someone else breaks conversation."""
        from klangk_backend.wshandler import state, _agent_conversations

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = state.get_or_create_session(workspace["id"])
        await session.add_subscriber(sock, "cid")
        try:
            _agent_conversations[workspace["id"]] = {
                "user_id": user["id"],
                "time": time.monotonic(),
                "interjected": False,
            }
            await conn.handle_chat_send({"message": "@bob hey"})
            await asyncio.sleep(0.1)
            assert workspace["id"] not in _agent_conversations
        finally:
            _agent_conversations.pop(workspace["id"], None)
            await session.remove_subscriber(sock)


class TestChatSend:
    async def test_chat_send_broadcasts(self, workspace, user, agent_user):
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
            assert sent["mentions"] == []
        finally:
            wshandler.state.sessions.pop(workspace["id"], None)

    async def test_chat_send_with_mention(self, workspace, user, agent_user):
        """Broadcast includes mention user IDs when @email is used."""
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]

        session = wshandler.state.get_or_create_session(workspace["id"])
        session.subscribers.add(sock)
        try:
            await conn.handle_chat_send({"message": f"hey @{user['email']}"})
            sent = sock.send_json.call_args[0][0]
            assert sent["mentions"] == [user["id"]]
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

    async def test_chat_send_agent_mention(self, workspace, user, agent_user):
        """@MrBoops sends thinking event + agent response."""
        from klangk_backend.wshandler import state

        # Seed a prior agent message so context filtering is exercised
        agent_email = await model.agent_email()
        await model.add_chat_message(
            workspace["id"],
            model.AGENT_USER_ID,
            agent_email,
            "I was here before",
            message_type=model.MSG_AGENT,
        )
        # Add a message from another user (interjection context)
        await model.add_chat_message(
            workspace["id"],
            "other-uid",
            "bob@test.com",
            "hey everyone check this out",
        )

        mock_session = AsyncMock()
        mock_session.send_prompt = AsyncMock(return_value="The time is now.")

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = state.get_or_create_session(workspace["id"])
        await session.add_subscriber(sock, "cid")
        try:
            with patch(
                "klangk_backend.agent.get_session",
                return_value=mock_session,
            ):
                await conn.handle_chat_send(
                    {"message": "@MrBoops what time is it?"}
                )
                await asyncio.sleep(0.1)
            # Prompt was sent with the user's question
            mock_session.send_prompt.assert_awaited_once()
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            # Should have: user msg, thinking=True, thinking=False, agent msg
            thinking_on = [
                c
                for c in calls
                if c.get("type") == "agent_thinking" and c.get("thinking")
            ]
            thinking_off = [
                c
                for c in calls
                if c.get("type") == "agent_thinking" and not c.get("thinking")
            ]
            assert len(thinking_on) == 1
            assert len(thinking_off) == 1
            agent_msgs = [
                c
                for c in calls
                if c.get("type") == "chat_message"
                and c.get("message_type") == 1
            ]
            assert len(agent_msgs) == 1
            assert agent_msgs[0]["message"] == "The time is now."
        finally:
            await session.remove_subscriber(sock)

    async def test_chat_send_agent_mention_empty_prompt(
        self, workspace, user, agent_user
    ):
        """@MrBoops with no prompt uses default greeting."""
        from klangk_backend.wshandler import state

        mock_session = AsyncMock()
        mock_session.send_prompt = AsyncMock(return_value="Hi there!")

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = state.get_or_create_session(workspace["id"])
        await session.add_subscriber(sock, "cid")
        try:
            with patch(
                "klangk_backend.agent.get_session",
                return_value=mock_session,
            ):
                await conn.handle_chat_send({"message": "@MrBoops"})
                await asyncio.sleep(0.1)
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            agent_msgs = [
                c
                for c in calls
                if c.get("type") == "chat_message"
                and c.get("message_type") == 1
            ]
            assert len(agent_msgs) == 1
            assert agent_msgs[0]["message"] == "Hi there!"
        finally:
            await session.remove_subscriber(sock)

    async def test_chat_send_agent_error(self, workspace, user, agent_user):
        """Agent error posts error message."""
        from klangk_backend.wshandler import state

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = state.get_or_create_session(workspace["id"])
        await session.add_subscriber(sock, "cid")
        try:
            with patch(
                "klangk_backend.agent.get_session",
                side_effect=RuntimeError("boom"),
            ):
                await conn.handle_chat_send({"message": "@MrBoops help"})
                await asyncio.sleep(0.1)
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            agent_msgs = [
                c
                for c in calls
                if c.get("type") == "chat_message"
                and c.get("message_type") == 1
            ]
            assert len(agent_msgs) == 1
            assert "error" in agent_msgs[0]["message"].lower()
        finally:
            await session.remove_subscriber(sock)

    async def test_chat_send_agent_process_died(
        self, workspace, user, agent_user
    ):
        """Agent process death posts system message."""
        from klangk_backend.wshandler import state
        from klangk_backend.agent import AgentProcessDied

        mock_session = AsyncMock()
        mock_session.send_prompt = AsyncMock(
            side_effect=AgentProcessDied("exited")
        )

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = state.get_or_create_session(workspace["id"])
        await session.add_subscriber(sock, "cid")
        try:
            with patch(
                "klangk_backend.agent.get_session",
                return_value=mock_session,
            ):
                await conn.handle_chat_send({"message": "@MrBoops hello"})
                await asyncio.sleep(0.1)
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            sys_msgs = [
                c
                for c in calls
                if c.get("type") == "chat_message"
                and c.get("message_type") == 2
            ]
            assert len(sys_msgs) == 1
            assert "disconnected" in sys_msgs[0]["message"]
        finally:
            await session.remove_subscriber(sock)

    async def test_chat_send_no_agent_mention(
        self, workspace, user, agent_user
    ):
        """Messages without @MrBoops don't trigger agent response."""
        from klangk_backend.wshandler import state

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        session = state.get_or_create_session(workspace["id"])
        await session.add_subscriber(sock, "cid")
        try:
            await conn.handle_chat_send({"message": "hello everyone"})
            await asyncio.sleep(0.1)
            calls = sock.send_json.call_args_list
            agent_msgs = [
                c[0][0]
                for c in calls
                if c[0][0].get("type") == "chat_message"
                and c[0][0].get("message_type") == 1
            ]
            assert len(agent_msgs) == 0
        finally:
            await session.remove_subscriber(sock)

    async def test_chat_history_on_connect(self, user, agent_user):
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

    async def test_chat_load_more(self, workspace, user):
        from klangk_backend import model

        msgs = []
        for i in range(5):
            msgs.append(
                await model.add_chat_message(
                    workspace["id"], "uid", "u@test.com", f"msg{i}"
                )
            )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        await conn.handle_chat_load_more(
            {"before_id": msgs[4]["id"], "limit": 2}
        )
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "chat_history_page"
        assert len(sent["messages"]) == 2
        assert sent["messages"][0]["message"] == "msg2"
        assert sent["has_more"] is True

    async def test_chat_load_more_no_workspace(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_chat_load_more({"before_id": "x"})
        sock.send_json.assert_not_called()

    async def test_chat_load_more_no_before_id(self, workspace, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        await conn.handle_chat_load_more({})
        sock.send_json.assert_not_called()

    async def test_workspace_members_on_connect(self, user, agent_user):
        """Workspace members list is sent on connect."""
        workspace = await _create_workspace_with_acl(user["id"], "members-ws")

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
        members_msgs = [
            c for c in calls if c.get("type") == "workspace_members"
        ]
        assert len(members_msgs) == 1
        member_ids = [m["id"] for m in members_msgs[0]["members"]]
        assert user["id"] in member_ids


class TestPresence:
    async def test_presence_list_on_connect(self, user, agent_user):
        """Joining user receives presence_list with current users."""
        workspace = await _create_workspace_with_acl(user["id"], "pres-ws")

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        wshandler.state.connections[sock] = conn

        async def fake_start(wid, ws_obj):
            conn.container_id = "cid"
            session = wshandler.state.get_or_create_session(wid)
            await session.add_subscriber(sock, "cid")

        try:
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
            plist = [c for c in calls if c.get("type") == "presence_list"]
            assert len(plist) == 1
            assert any(u["user_id"] == user["id"] for u in plist[0]["users"])
        finally:
            wshandler.state.sessions.pop(workspace["id"], None)
            wshandler.state.connections.pop(sock, None)

    async def test_presence_join_broadcast(self, user, agent_user):
        """Existing subscribers receive presence_join when someone connects."""
        from klangk_backend import model

        workspace = await _create_workspace_with_acl(
            user["id"], "pres-join-ws"
        )

        # First user connects
        sock1 = _mock_sock()
        conn1 = _base_conn(user=user, ws=sock1)

        session = wshandler.state.get_or_create_session(workspace["id"])
        session.subscribers.add(sock1)
        wshandler.state.connections[sock1] = conn1
        conn1.workspace_id = workspace["id"]

        # Second user connects
        other = await model.create_user(
            "other@test.com", "hash", verified=True
        )
        await model.add_acl_entry(
            f"/workspaces/{workspace['id']}",
            1,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=other["id"],
        )
        sock2 = _mock_sock()
        conn2 = _base_conn(
            user={
                "id": other["id"],
                "email": "other@test.com",
                "handle": other.get("handle", ""),
            },
            ws=sock2,
        )
        wshandler.state.connections[sock2] = conn2

        async def fake_start(wid, ws_obj):
            conn2.container_id = "cid"
            session = wshandler.state.get_or_create_session(wid)
            await session.add_subscriber(sock2, "cid")

        try:
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
                await conn2.handle_workspace_connect(
                    {"workspaceId": workspace["id"]}
                )

            # sock1 should have received presence_join for other user
            calls1 = [c[0][0] for c in sock1.send_json.call_args_list]
            joins = [c for c in calls1 if c.get("type") == "presence_join"]
            assert len(joins) == 1
            assert joins[0]["user_id"] == other["id"]
            assert joins[0]["user_email"] == "other@test.com"
            assert "user_handle" in joins[0]
        finally:
            wshandler.state.sessions.pop(workspace["id"], None)
            wshandler.state.connections.pop(sock1, None)
            wshandler.state.connections.pop(sock2, None)

    async def test_presence_leave_broadcast(self, user):
        """Remaining subscribers receive presence_leave on disconnect."""
        from klangk_backend import model

        workspace = await _create_workspace_with_acl(user["id"], "pres-lv-ws")

        sock1 = _mock_sock()
        sock2 = _mock_sock()
        conn1 = _base_conn(user=user, ws=sock1)
        other = await model.create_user("lv@test.com", "hash", verified=True)
        conn2 = _base_conn(
            user={"id": other["id"], "email": "lv@test.com"}, ws=sock2
        )
        conn1.workspace_id = workspace["id"]
        conn2.workspace_id = workspace["id"]

        session = WorkspaceSession(workspace["id"])
        session.subscribers.add(sock1)
        session.subscribers.add(sock2)
        wshandler.state.sessions[workspace["id"]] = session
        wshandler.state.connections[sock1] = conn1
        wshandler.state.connections[sock2] = conn2

        try:
            # conn2 disconnects
            await conn2.cleanup()

            calls1 = [c[0][0] for c in sock1.send_json.call_args_list]
            leaves = [c for c in calls1 if c.get("type") == "presence_leave"]
            assert len(leaves) == 1
            assert leaves[0]["user_id"] == other["id"]
        finally:
            wshandler.state.sessions.pop(workspace["id"], None)
            wshandler.state.connections.pop(sock1, None)
            wshandler.state.connections.pop(sock2, None)

    async def test_presence_leave_multi_tab(self, user):
        """No presence_leave if user has another connection in workspace."""
        from klangk_backend import model

        workspace = await _create_workspace_with_acl(user["id"], "pres-mt-ws")

        sock1 = _mock_sock()
        sock2 = _mock_sock()
        sock3 = _mock_sock()
        # sock1 and sock2 are same user, sock3 is another user
        conn1 = _base_conn(user=user, ws=sock1)
        conn2 = _base_conn(user=user, ws=sock2)
        other = await model.create_user("mt@test.com", "hash", verified=True)
        conn3 = _base_conn(
            user={"id": other["id"], "email": "mt@test.com"}, ws=sock3
        )
        conn1.workspace_id = workspace["id"]
        conn2.workspace_id = workspace["id"]
        conn3.workspace_id = workspace["id"]

        session = WorkspaceSession(workspace["id"])
        session.subscribers.add(sock1)
        session.subscribers.add(sock2)
        session.subscribers.add(sock3)
        wshandler.state.sessions[workspace["id"]] = session
        wshandler.state.connections[sock1] = conn1
        wshandler.state.connections[sock2] = conn2
        wshandler.state.connections[sock3] = conn3

        try:
            # sock1 disconnects, but sock2 (same user) remains
            await conn1.cleanup()

            calls3 = [c[0][0] for c in sock3.send_json.call_args_list]
            leaves = [c for c in calls3 if c.get("type") == "presence_leave"]
            assert len(leaves) == 0
        finally:
            wshandler.state.sessions.pop(workspace["id"], None)
            wshandler.state.connections.pop(sock1, None)
            wshandler.state.connections.pop(sock2, None)
            wshandler.state.connections.pop(sock3, None)


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


class TestHandleAutoCreateFailure:
    async def test_no_handle_in_db_sets_user_home_none(
        self, user, temp_data_dir
    ):
        """If get_user_handle returns None, _user_home is None."""
        ws = await ws_mod.create_workspace(user["id"], "handle-fail")
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(
            user={"id": user["id"], "email": user["email"]}, ws=sock
        )

        async def fake_start(*a, **kw):
            container.registry.track_activity("cid-hf", ws["id"])
            return ("cid-hf", "created")

        with (
            patch.object(
                container.registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch("glob.glob", return_value=[]),
            patch(
                "klangk_backend.model.get_user_handle",
                return_value=None,
            ),
        ):
            await conn.start_workspace_container(ws["id"], ws)

        assert conn._user_home is None
        assert conn.container_id == "cid-hf"

        wshandler.state.sessions.pop(ws["id"], None)
        container.registry.states.pop(ws["id"], None)


class TestUiReadySharedTerminals:
    async def test_ui_ready_sends_shared_terminals(self, user, temp_data_dir):
        from klangk_backend import workspaces

        ws = await workspaces.create_workspace(user["id"], "ui-shared")
        sock = _mock_sock()
        conn = _base_conn(
            user={"id": user["id"], "email": user["email"]}, ws=sock
        )
        conn.workspace_id = ws["id"]
        conn.container_id = "cid"
        conn._user_home = "/home/testuser"
        conn.pending_status_msg = "ready"

        # Set up in-memory shared state
        session = wshandler.state.get_or_create_session(ws["id"])
        session.terminal_windows[user["id"]] = [
            {"name": "dev", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        wshandler.state.connections[sock] = conn
        try:
            await conn.handle_ui_ready()

            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                isinstance(m, dict) and m.get("type") == "shared_terminals"
                for m in sent
            )
        finally:
            wshandler.state.sessions.pop(ws["id"], None)
            wshandler.state.connections.pop(sock, None)

    async def test_ui_ready_sends_container_ready(self, user, temp_data_dir):
        from klangk_backend import workspaces

        ws = await workspaces.create_workspace(user["id"], "ui-ready-cr")
        sock = _mock_sock()
        conn = _base_conn(
            user={"id": user["id"], "email": user["email"]}, ws=sock
        )
        conn.workspace_id = ws["id"]
        conn.container_id = "cid"
        conn._user_home = "/home/testuser"
        conn.pending_status_msg = "ready"

        session = wshandler.state.get_or_create_session(ws["id"])
        await session.add_subscriber(sock, "cid")
        try:
            await conn.handle_ui_ready()

            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                isinstance(m, dict)
                and m.get("type") == "event"
                and m.get("event", {}).get("name") == "container_ready"
                for m in sent
            )
        finally:
            wshandler.state.sessions.pop(ws["id"], None)
