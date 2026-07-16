"""Tests for wshandler: WebSocket command dispatch, event forwarding, terminal, cleanup."""

import asyncio
import base64
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import types
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from fastapi import WebSocketDisconnect

from klangk_backend import (
    agent as agent_mod,
    acl as acl_mod,
    auth as auth_mod,
    emailsvc as emailsvc_mod,
    files as files_mod,
    util as util_mod,
    model,
    wshandler,
    container,
    workspaces as ws_mod,
)
from klangk_backend.exceptions import TerminalError
from _helpers import make_settings
from klangk_backend.wshandler import (
    constants as _ws_constants,
    controllers as _ws_controllers,
)
from klangk_backend.wshandler import (
    Connection,
    ExecController,
    SafeWebSocket,
    SharedTerminalController,
    SlowClientError,
    SshAgentForwarder,
    TerminalController,
    WebSocketState,
    WorkspaceSession,
    clear_agent_mention_state,
    disconnect_all_websockets,
    send_error,
    handle_websocket,
    reset_workspace_state,
    log_ws_msg,
    SEND_QUEUE_SIZE,
    get_presence_list,
)


def _util(env=None):
    """Build a Util instance from explicit env."""
    settings = make_settings(env)
    return util_mod.Util(types.SimpleNamespace(settings=settings))


_mock_pod = MagicMock()
_mock_pod.exec_container = AsyncMock(return_value=(0, "", ""))

# #1480: shared mock Terminal whose methods tests patch via
# patch.object(_mock_term, ...).
_mock_term = MagicMock()
_mock_term.podman = _mock_pod
_mock_term.ensure_base_session = AsyncMock()
_mock_term.attach_browser = AsyncMock()
_mock_term.set_workspace_token = AsyncMock()
_mock_term.list_windows = AsyncMock(return_value=[])
_mock_term.ensure_service_session = AsyncMock()
_mock_term.tmux_command = AsyncMock(return_value="")
_mock_term.new_window = AsyncMock(return_value=[])
_mock_term.select_window = AsyncMock()
_mock_term.close_window = AsyncMock(return_value=[])
_mock_term.rename_window = AsyncMock()
_mock_term.kill_joiner_sessions = AsyncMock()
_mock_term.has_tmux_session = AsyncMock(return_value=False)
_mock_term.service_cmd_window_exists = AsyncMock(return_value=False)


def _make_app_state(registry=None, sockets=None):
    """Build a minimal app_state for tests."""
    from klangk_backend.container import ContainerRegistry

    settings = make_settings({})
    # Two-phase: shell first so owned instances (sockets, registry, etc.)
    # can take app_state at construction (#1426).
    app_state = types.SimpleNamespace(settings=settings)
    if sockets is None:
        sockets = WebSocketState(app_state)
    app_state.sockets = sockets
    if registry is None:
        registry = ContainerRegistry(app_state)
    app_state.container_registry = registry
    app_state.podman = _mock_pod
    # #1480: shared mock Terminal wired onto app_state. Tests patch
    # its methods via patch.object(_mock_term, ...).
    app_state.terminal = _mock_term
    app_state.workspaces = ws_mod.Workspaces(app_state)
    app_state.files = files_mod.Files(app_state)
    app_state.agents = agent_mod.Agents(app_state)
    app_state.email = emailsvc_mod.EmailService(app_state)
    app_state.util = util_mod.Util(app_state)

    app_state.auth = auth_mod.Auth(app_state)
    # #1572: ContainerRegistry reaches app_state.model.ports; Auth reaches
    # app_state.model.{tokens,login_attempts}.
    from _helpers import wire_db_and_model

    wire_db_and_model(app_state)
    return app_state


def _auth():
    """A standalone Auth for token forging (same default secret as the
    app fixture, so tokens round-trip through app.state.auth.decode_*)."""
    from _helpers import wire_db_and_model

    state = types.SimpleNamespace(settings=make_settings({}))
    wire_db_and_model(state)
    return auth_mod.Auth(state)


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

    # output() is an async generator (terminal.py). Auto-AsyncMock returns a
    # coroutine, not an async iterator, so a test that iterates it without
    # overriding .output would produce an un-awaited coroutine (RuntimeWarning
    # at GC). Default to an empty async generator to match the real signature.
    async def _empty_output():
        if False:  # pragma: no cover - empty generator
            yield

    t.output = _empty_output
    return t


async def _empty_async_generator():
    """An async generator that yields nothing — the safe default for
    ``session.output`` on a bare ``AsyncMock()`` session (see
    ``_mock_terminal`` for why)."""
    if False:  # pragma: no cover - empty generator
        yield


async def _await_agent_run(workspace_id: str) -> None:
    """Wait for a workspace's in-flight agent run to finish.

    ``handle_chat_send`` schedules ``handle_agent_mention`` as a
    fire-and-forget ``asyncio.create_task`` and returns immediately, so a
    test can't observe the run's side effects (the ``send_prompt`` call,
    the broadcasts) until that task has executed.  Awaiting the registered
    task directly — instead of ``asyncio.sleep`` — removes the wall-clock
    race that made the agent-mention tests flaky under ``-n auto``, where
    CPU contention from sibling xdist workers let the 0.1s sleep elapse
    before the task reached ``send_prompt`` (#1581).
    """
    task = _ws_constants.agent_tasks.get(workspace_id)
    if task is not None:
        await task


def _base_conn(user=None, ws=None, app_state=None):
    if ws is None:
        ws = _mock_sock()
    if user is None:
        user = {
            "id": "uid",
            "email": "testuser@example.com",
            "handle": "testuser",
        }
    if app_state is None:
        app_state = _make_app_state()
    return Connection(ws, user, app_state)


@asynccontextmanager
async def _conn_in_workspace(
    user,
    workspace_id: str = "ws-1",
    *,
    container_id: str = "cid",
    user_home: str | None = None,
    app_state=None,
):
    """Yield ``(sock, conn, session, app_state)`` registered in workspace state.

    Creates a mock socket and Connection, registers it as a subscriber
    of a fresh WorkspaceSession, and tears the registration down on exit.
    The yielded ``session`` may be mutated (e.g. ``terminal_windows``)
    by the caller before use.
    """
    if app_state is None:
        app_state = _make_app_state()
    sockets = app_state.sockets
    sock = _mock_sock()
    conn = _base_conn(user=user, ws=sock, app_state=app_state)
    conn.workspace_id = workspace_id
    conn.container_id = container_id
    conn._user_home = user_home
    session = sockets.get_or_create_session(workspace_id, app_state)
    await session.add_subscriber(sock, container_id)
    sockets.connections[sock] = conn
    try:
        yield sock, conn, session, app_state
    finally:
        await session.remove_subscriber(sock)
        sockets.connections.pop(sock, None)
        sockets.sessions.pop(workspace_id, None)


async def _create_workspace_with_acl(app_state, user_id, name, **kwargs):
    """Create a workspace whose owner has full access.

    The service-layer ``create_workspace`` now seeds the owner ACE and role
    groups atomically (see model.create_workspace_with_acl, #128), so this
    is a thin alias kept for call-site readability.
    """
    return await app_state.workspaces.create_workspace(user_id, name, **kwargs)


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

    async def test_client_delegates(self):
        raw = AsyncMock()
        # Starlette WebSocket.client is an Address with .host, or None.
        raw.client = type("Addr", (), {"host": "127.0.0.1"})()
        sw = SafeWebSocket(raw)
        assert sw.client.host == "127.0.0.1"

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


# --- disconnect_all (SIGHUP runtime restart) ---


class TestDisconnectAll:
    async def test_clears_connections_sessions_and_sockets(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        sockets.connections[sock] = conn
        sockets.get_or_create_session("ws-disc", app_state)
        # A pending presence-leave task to confirm it gets cancelled.
        leave_task = asyncio.create_task(asyncio.sleep(100))
        sockets._pending_leaves[("ws-disc", "u1")] = leave_task
        # A pending browser-delegate request with an unresolved future.
        br_future = asyncio.get_running_loop().create_future()
        sockets.pending_browser_requests["req-1"] = (br_future, sock)
        # A streaming browser request.
        stream_q = asyncio.Queue()
        sockets.streaming_browser_requests["req-2"] = (stream_q, sock)

        await sockets.disconnect_all()

        assert sockets.connections == {}
        assert sockets.sessions == {}
        assert sockets._pending_leaves == {}
        assert sockets.pending_browser_requests == {}
        assert sockets.streaming_browser_requests == {}
        # Leave task was cancelled; await it so the cancellation completes.
        with pytest.raises(asyncio.CancelledError):
            await leave_task
        assert leave_task.cancelled()
        assert br_future.cancelled()
        sock.close.assert_awaited_once_with(code=1012)

    async def test_swallows_close_errors(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        bad_sock = _mock_sock()
        bad_sock.close = AsyncMock(side_effect=RuntimeError("boom"))
        sockets.connections[bad_sock] = _base_conn(
            ws=bad_sock, app_state=app_state
        )

        # Must not raise even though close() blows up.
        await sockets.disconnect_all()

        assert sockets.connections == {}

    async def test_empty_is_noop(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        await sockets.disconnect_all()
        assert sockets.connections == {}
        assert sockets.sessions == {}

    async def test_disconnect_all_websockets_wrapper(self):
        """The package-level wrapper delegates to state.disconnect_all."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        sockets.connections[sock] = _base_conn(ws=sock, app_state=app_state)
        await disconnect_all_websockets(sockets)
        assert sockets.connections == {}
        sock.close.assert_awaited_once_with(code=1012)


class TestClearAgentMentionState:
    async def test_cancels_tasks_and_clears_conversations(self):
        task = asyncio.create_task(asyncio.sleep(100))
        _ws_constants.agent_tasks["ws-m"] = task
        _ws_constants.agent_conversations["ws-m"] = {"user_id": "u1"}

        clear_agent_mention_state()

        assert _ws_constants.agent_tasks == {}
        assert _ws_constants.agent_conversations == {}
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.cancelled()

    def test_empty_is_noop(self):
        clear_agent_mention_state()
        assert _ws_constants.agent_tasks == {}
        assert _ws_constants.agent_conversations == {}


# --- send_error ---


class TestSendError:
    def test_sends_error_json(self):
        sock = _mock_sock()
        send_error(sock, "bad thing")
        sock.send_json.assert_called_once_with(
            {"type": "error", "message": "bad thing"}
        )


# Proxy-trust, hosting-info, and client_is_loopback tests moved to
# test_util.py now that those helpers are Util(app_state) methods (#1503).
# --- handle_steer ---


class TestHandleTerminalInput:
    async def test_writes_data(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        t = _mock_terminal()
        conn = _base_conn(app_state=app_state)
        conn.terminal_session = t
        conn.container_id = "cid"
        registry.track_activity("cid", "ws")

        await conn.handle_terminal_input({"data": "ls\n"})

        t.write.assert_awaited_once_with("ls\n")
        registry.states.pop("ws", None)

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
        app_state = _make_app_state()
        registry = app_state.container_registry
        t = _mock_terminal()
        t.read_only = True
        conn = _base_conn(app_state=app_state)
        conn.terminal_session = t
        conn.container_id = "cid"
        registry.track_activity("cid", "ws")

        await conn.handle_terminal_input({"data": "ls\n"})
        t.write.assert_not_awaited()
        registry.states.pop("ws", None)

    async def test_read_only_allows_escape_sequences(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        t = _mock_terminal()
        t.read_only = True
        conn = _base_conn(app_state=app_state)
        conn.terminal_session = t
        conn.container_id = "cid"
        registry.track_activity("cid", "ws")

        # DA response: ESC [ ? 6 c
        await conn.handle_terminal_input({"data": "\x1b[?6c"})
        t.write.assert_awaited_once_with("\x1b[?6c")
        registry.states.pop("ws", None)

    async def test_oversized_input_dropped(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        t = _mock_terminal()
        conn = _base_conn(app_state=app_state)
        conn.terminal_session = t
        conn.container_id = "cid"
        registry.track_activity("cid", "ws")

        big_data = "x" * (_ws_constants.MAX_INPUT_SIZE + 1)
        await conn.handle_terminal_input({"data": big_data})
        t.write.assert_not_awaited()
        registry.states.pop("ws", None)


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
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        # Create a session with shared windows so the shared_terminals
        # broadcast path (lines 977-978) is exercised.
        session = sockets.get_or_create_session("ws", app_state)
        session.terminal_windows["other-uid"] = [
            {"name": "dev", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        sockets.connections[sock] = conn

        with (
            patch.object(_ws_controllers, "TerminalSession") as MockTS,
            patch.object(
                _mock_term,
                "list_windows",
                return_value=[
                    {"id": "@0", "index": 0, "name": "bash", "active": True}
                ],
            ),
            patch.object(_mock_term, "attach_browser", new_callable=AsyncMock),
            patch.object(
                _ws_controllers.TerminalController,
                "_sync_service_windows",
                new=AsyncMock(return_value=False),
            ),
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
            ssh_agent_socket=None,
            terminal=_mock_term,
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
        assert conn.browser_id == "test-browser-id"
        assert conn.terminal_session is mock_session
        assert conn.terminal_task is not None
        # Should have sent terminal_started ack (followed by terminal_windows)
        assert any(
            isinstance(m, dict) and m.get("type") == "terminal_started"
            for m in sent
        )

        # sync_terminal_windows should have populated terminal_windows
        ws_session = sockets.sessions.get("ws")
        assert ws_session is not None
        assert "uid" in ws_session.terminal_windows
        assert ws_session.terminal_windows["uid"][0]["name"] == "bash"

        # Clean up
        sockets.sessions.pop("ws", None)
        sockets.connections.pop(sock, None)
        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_terminal_start_fires_service_command(self):
        """terminal_start fires the service command in the agent's service
        session (the post-setup path, #1033/#1133) -- not in any user's
        session. The on_service_command_started callback is gone; the
        service command is fired via _fire_service_command."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"
        conn._service_command = "./serve"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")
        session = sockets.get_or_create_session("ws", app_state)
        await session.add_subscriber(sock, "cid")
        sockets.connections[sock] = conn

        with (
            patch.object(_ws_controllers, "TerminalSession") as MockTS,
            patch.object(
                _mock_term,
                "list_windows",
                return_value=[
                    {"id": "@0", "index": 0, "name": "bash", "active": True}
                ],
            ),
            patch.object(_mock_term, "attach_browser", new_callable=AsyncMock),
            patch.object(
                app_state.model.workspaces,
                "get_workspace",
                new=AsyncMock(return_value={"setup_state": "complete"}),
            ),
            patch.object(
                app_state.model.users,
                "agent_handle",
                new=AsyncMock(return_value="clanker"),
            ),
            patch.object(
                _mock_term,
                "ensure_service_session",
                new=AsyncMock(),
            ) as mock_ess,
        ):
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield

            mock_session.output = fake_output

            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        # Fired in the service session with the agent home, not threaded
        # into the user's TerminalSession (no service_command kwarg).
        mock_ess.assert_awaited_once_with(
            "cid",
            "/home/clanker",
            "./serve",
            setup_state="complete",
        )
        assert "service_command" not in MockTS.call_args.kwargs

        sockets.sessions.pop("ws", None)
        sockets.connections.pop(sock, None)
        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

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
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
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

        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_rename_failure_non_fatal(self):
        """If renaming the initial bash window fails, tabs still work."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        with (
            patch.object(_ws_controllers, "TerminalSession") as MockTS,
            patch.object(
                _mock_term,
                "list_windows",
                return_value=[
                    {"id": "@0", "index": 0, "name": "bash", "active": True}
                ],
            ),
            patch.object(
                _mock_term,
                "tmux_command",
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
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_window_list_failure_non_fatal(self):
        """If list_windows fails after terminal start, terminal still works."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        with (
            patch.object(_ws_controllers, "TerminalSession") as MockTS,
            patch.object(
                _mock_term,
                "list_windows",
                side_effect=TerminalError("tmux not ready"),
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
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_shared_list_failure_non_fatal(self):
        """If list_shared_terminals fails after start, terminal still works."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm
        registry.track_activity("cid", "ws")

        with (
            patch.object(_ws_controllers, "TerminalSession") as MockTS,
            patch.object(
                _mock_term,
                "list_windows",
                return_value=[
                    {"id": "@0", "index": 0, "name": "1", "active": True}
                ],
            ),
            patch.object(_mock_term, "tmux_command", return_value=""),
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
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_restart_revokes_old_browser_registration(self):
        """Starting a second terminal revokes the previous browser registration."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        with (
            patch.object(_ws_controllers, "TerminalSession") as MockTS,
            patch.object(_mock_term, "attach_browser", new_callable=AsyncMock),
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
            assert conn.browser_id == "bid-1"
            assert registry.resolve_browser("bid-1") is not None

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
            assert conn.browser_id == "bid-1"
            assert registry.resolve_browser("bid-1") is not None

            conn.terminal_task.cancel()
            try:
                await conn.terminal_task
            except asyncio.CancelledError:
                pass
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_browser_reattach_updates_registration(self):
        """browser_reattach re-registers the browser ID and calls attach_browser."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"

        registry.register_browser("bid-old", "ws", sock)
        conn.browser_id = "bid-old"

        with patch.object(
            _mock_term, "attach_browser", new_callable=AsyncMock
        ) as mock_attach:
            await conn.handle_browser_reattach({"browser_id": "bid-new"})

        assert conn.browser_id == "bid-new"
        assert registry.resolve_browser("bid-new") == ("ws", sock)
        assert registry.resolve_browser("bid-old") is None
        mock_attach.assert_awaited_once_with("cid", "bid-new")

        registry.revoke_workspace_browsers("ws")

    async def test_browser_reattach_no_browser_id_is_noop(self):
        """browser_reattach with no browser_id does nothing."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn.browser_id = "bid-existing"

        with patch.object(
            _mock_term, "attach_browser", new_callable=AsyncMock
        ) as mock_attach:
            await conn.handle_browser_reattach({})

        assert conn.browser_id == "bid-existing"
        mock_attach.assert_not_awaited()

    async def test_browser_reattach_no_container_is_noop(self):
        """browser_reattach without a container does nothing."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = None
        conn.workspace_id = "ws"

        with patch.object(
            _mock_term, "attach_browser", new_callable=AsyncMock
        ) as mock_attach:
            await conn.handle_browser_reattach({"browser_id": "bid-new"})

        assert conn.browser_id is None
        mock_attach.assert_not_awaited()

    async def test_passes_service_command(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"
        conn._service_command = "pi"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.is_alive = True
        MockTS = MagicMock(return_value=mock_session)
        with (
            patch(
                "klangk_backend.wshandler.controllers.TerminalSession", MockTS
            ),
            patch.object(_mock_term, "attach_browser", new_callable=AsyncMock),
            patch.object(
                app_state.model.workspaces,
                "get_workspace",
                new=AsyncMock(return_value={"setup_state": "complete"}),
            ),
            patch.object(
                app_state.model.users,
                "agent_handle",
                new=AsyncMock(return_value="clanker"),
            ),
            patch.object(
                _mock_term,
                "ensure_service_session",
                new=AsyncMock(),
            ) as mock_ess,
        ):
            await conn.handle_terminal_start(
                {
                    "cols": 80,
                    "rows": 24,
                    "browser_id": "bid-cmd",
                }
            )
            await asyncio.sleep(0)

        # The service command is NOT threaded into the user's session --
        # it fires in the standalone service session (#1133).
        MockTS.assert_called_once_with(
            "cid",
            session_name="uid",
            user_home="/home/testuser",
            user_id="uid",
            user_handle="testuser",
            ssh_agent_socket=None,
            terminal=_mock_term,
        )
        mock_ess.assert_awaited_once_with(
            "cid",
            "/home/clanker",
            "pi",
            setup_state="complete",
        )

        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_failure_sends_error(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock(
            side_effect=RuntimeError("podman broke")
        )
        MockTS = MagicMock(return_value=mock_session)
        with patch(
            "klangk_backend.wshandler.controllers.TerminalSession", MockTS
        ):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        # Should have sent an error, not terminal_started
        sent = sock.send_json.call_args_list
        assert any(call.args[0].get("type") == "error" for call in sent)
        # Session is stored immediately but stop() is called on failure
        mock_session.stop.assert_awaited_once()
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_slow_client_cleans_up(self):
        """SlowClientError during start cleans up without sending error."""
        app_state = _make_app_state()
        registry = app_state.container_registry

        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock(side_effect=SlowClientError())
        MockTS = MagicMock(return_value=mock_session)
        with patch(
            "klangk_backend.wshandler.controllers.TerminalSession", MockTS
        ):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        mock_session.stop.assert_awaited_once()
        # No error message sent (client is gone)
        sent = sock.send_json.call_args_list
        assert not any(call.args[0].get("type") == "error" for call in sent)
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_failure_send_error_ws_dead(self):
        """If send_error itself fails with a WS error, it's swallowed."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock(side_effect=ValueError("bad config"))
        # send_json raises RuntimeError (a WS_ERRORS member) when
        # trying to send the error message
        sock.send_json = MagicMock(side_effect=RuntimeError("ws gone"))
        MockTS = MagicMock(return_value=mock_session)
        with patch(
            "klangk_backend.wshandler.controllers.TerminalSession", MockTS
        ):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        mock_session.stop.assert_awaited_once()
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_cancellation_during_start_cleans_up(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock(side_effect=asyncio.CancelledError)
        MockTS = MagicMock(return_value=mock_session)
        with patch(
            "klangk_backend.wshandler.controllers.TerminalSession", MockTS
        ):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            task = conn.terminal_task
            with pytest.raises(asyncio.CancelledError):
                await task

        # session.stop() must be called to clean up the PTY subprocess
        mock_session.stop.assert_awaited_once()
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_session_replaced_during_start_aborts(self):
        """If stop_terminal replaces the session while start() is running,
        the startup task stops the orphaned session and does not send
        terminal_started."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn._has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator

        async def start_and_replace(*a, **kw):
            # Simulate stop_terminal replacing the session mid-start
            conn.terminal_session = AsyncMock()

        mock_session.start = AsyncMock(side_effect=start_and_replace)
        MockTS = MagicMock(return_value=mock_session)
        with patch(
            "klangk_backend.wshandler.controllers.TerminalSession", MockTS
        ):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        # The orphaned session must be stopped
        mock_session.stop.assert_awaited_once()
        # terminal_started must NOT be sent
        for call in sock.send_json.call_args_list:
            assert call.args[0].get("type") != "terminal_started"
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_no_container(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_terminal_start({})
        assert conn.terminal_session is None


# --- handle management ---


class TestHandleSetHandle:
    async def test_set_handle_success(self, user, temp_data_dir, app_state):

        ws = await app_state.workspaces.create_workspace(
            user["id"], "handle-test"
        )
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
        mock_skel.assert_awaited_once_with(
            "cid", user["id"], conn.app_state.podman
        )
        sent = sock.send_json.call_args_list
        assert any(
            call.args[0].get("type") == "handle_set"
            and call.args[0].get("handle") == "alice"
            for call in sent
        )
        assert conn._user_home == "/home/alice"

    async def test_set_handle_conflict(self, user, temp_data_dir, app_state):
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

    async def test_handle_auto_created_on_connect(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "auto-handle"
        )

        async def fake_start(*a, **kw):
            registry.track_activity("cid-ah", workspace["id"])
            return ("cid-ah", "created")

        with (
            patch.object(
                registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch("glob.glob", return_value=[]),
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        # Handle is derived from email at user creation time
        assert conn._user_home is not None
        assert conn._user_home.startswith("/home/")

        sockets.sessions.pop(workspace["id"], None)
        registry.states.pop(workspace["id"], None)

    async def test_handle_resolved_on_start(
        self, user, temp_data_dir, app_state
    ):

        ws = await app_state.workspaces.create_workspace(
            user["id"], "handle-test4"
        )
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
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        t = _mock_terminal()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "ctr-fwd"
        conn.terminal_session = t
        registry.track_activity("ctr-fwd", "ws-fwd")

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
        assert "ws-fwd" in registry.states
        registry.states.pop("ws-fwd", None)

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


class TestCleanupConnection:
    async def test_cleanup_last_subscriber_removes_session(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock()
        t = _mock_terminal()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "ctr-full"
        conn.workspace_id = "ws-cleanup-1"
        conn._idle_cb = lambda ws: None
        conn.terminal_session = t
        conn.terminal_task = asyncio.create_task(asyncio.sleep(10))

        registry.track_activity("ctr-full", "ws-cleanup-1")
        session = WorkspaceSession("ws-cleanup-1", app_state)
        session.subscribers.add(sock)
        sockets.sessions["ws-cleanup-1"] = session
        registry.states["ws-cleanup-1"].idle_callbacks.append(conn._idle_cb)

        await conn.cleanup()

        t.stop.assert_awaited_once()
        assert conn._idle_cb is None
        assert conn.terminal_session is None
        # Session removed when last subscriber disconnects
        assert "ws-cleanup-1" not in sockets.sessions

        registry.states.pop("ws-cleanup-1", None)

    async def test_cleanup_other_subscribers_remain(self):
        """When other subscribers remain, session stays alive."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock()
        other_sock = _mock_sock()
        t = _mock_terminal()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "ctr-shared"
        conn.workspace_id = "ws-cleanup-2"
        conn._idle_cb = lambda ws: None
        conn.terminal_session = t
        conn.terminal_task = asyncio.create_task(asyncio.sleep(10))

        registry.track_activity("ctr-shared", "ws-cleanup-2")
        session = WorkspaceSession("ws-cleanup-2", app_state)
        session.subscribers.add(sock)
        session.subscribers.add(other_sock)
        sockets.sessions["ws-cleanup-2"] = session
        registry.states["ws-cleanup-2"].idle_callbacks.append(conn._idle_cb)

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
        assert "ws-cleanup-2" in sockets.sessions
        assert other_sock in session.subscribers
        assert sock not in session.subscribers

        # Cleanup
        registry.states.pop("ws-cleanup-2", None)
        sockets.sessions.pop("ws-cleanup-2", None)

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

    async def test_workspace_not_found(self, user, app_state):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        await conn.handle_workspace_connect({"workspaceId": "fake"})
        assert "Permission denied" in sock.send_json.call_args[0][0]["message"]

    async def test_connect_success(self, user, agent_user, app_state):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "test-ws"
        )
        conn = _base_conn(user=user, ws=sock, app_state=app_state)

        async def fake_start(wid, workspace):
            conn.container_id = "cid"

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                registry,
                "get_workspace_ports",
                return_value=[9000, 9001],
            ),
        ):
            await conn.handle_workspace_connect(
                {"workspaceId": workspace["id"]}
            )

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        ready = [c for c in calls if c.get("type") == "container_ready"]
        assert len(ready) == 1
        assert ready[0]["workspaceId"] == workspace["id"]
        assert ready[0]["serviceCommand"] is None
        assert "userHome" in ready[0]
        # Integer timeout (default 30m) should show as "30m" not "30.0m"
        assert "30m" in conn.pending_status_msg

    async def test_connect_sends_service_command(
        self, user, agent_user, app_state
    ):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "cmd-ws", service_command="pi"
        )
        conn = _base_conn(user=user, ws=sock, app_state=app_state)

        async def fake_start(wid, workspace):
            conn.container_id = "cid"

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                registry,
                "get_workspace_ports",
                return_value=[9000],
            ),
        ):
            await conn.handle_workspace_connect(
                {"workspaceId": workspace["id"]}
            )

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        ready = [c for c in calls if c.get("type") == "container_ready"]
        assert ready[0]["serviceCommand"] == "pi"

    async def test_connect_denied_no_acl(self, user, app_state):
        """User without ACL entry gets 'Permission denied'."""
        sock = _mock_sock()
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "no-acl-ws"
        )
        conn = _base_conn(user={"id": "other-user", "email": "x"}, ws=sock)
        await conn.handle_workspace_connect({"workspaceId": workspace["id"]})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission denied" in str(c) for c in calls)

    async def test_connect_race_deleted(self, user, app_state):
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

    async def test_connect_container_start_valueerror(
        self, user, agent_user, app_state
    ):
        """ValueError from start_container is sent as an error, not a crash."""
        sock = _mock_sock()
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "bad-mount"
        )
        conn = _base_conn(user=user, ws=sock)

        with patch.object(
            Connection,
            "start_workspace_container",
            side_effect=ValueError("Bind mount source does not exist: /nope"),
        ):
            await conn.handle_workspace_connect(
                {"workspaceId": workspace["id"]}
            )

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        errors = [c for c in calls if c.get("type") == "error"]
        assert len(errors) == 1
        assert "does not exist" in errors[0]["message"]


class TestHandleWorkspaceDisconnect:
    async def test_disconnect(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws-1"

        with patch.object(
            registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            await conn.handle_workspace_disconnect()

        assert conn.workspace_id is None
        assert conn.container_id is None


# --- handle_restart_container ---


class TestStartWorkspaceContainer:
    async def test_new_session(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "start-ws"
        )

        async def fake_start(*a, **kw):
            registry.track_activity("cid-1", workspace["id"])
            return ("cid-1", "created")

        with (
            patch.object(
                registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch("glob.glob", return_value=[]),
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        assert conn.container_id == "cid-1"
        assert conn.workspace == workspace
        assert workspace["id"] in sockets.sessions
        assert conn._idle_cb is not None
        # Handle auto-created from email on connect
        assert conn._user_home is not None
        assert conn._user_home.startswith("/home/")

        sockets.sessions.pop(workspace["id"], None)
        registry.states.pop(workspace["id"], None)

    async def test_resolves_existing_handle(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "handle-ws"
        )
        # Set a custom handle in the DB
        await model.set_user_handle(user["id"], "chris")

        async def fake_start(*a, **kw):
            registry.track_activity("cid-h", workspace["id"])
            return ("cid-h", "created")

        with (
            patch.object(
                registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch("glob.glob", return_value=[]),
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        assert conn._user_home == "/home/chris"

        sockets.sessions.pop(workspace["id"], None)
        registry.states.pop(workspace["id"], None)

    async def test_idle_callback_ws_error(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "idle-ws"
        )

        async def fake_start(*a, **kw):
            registry.track_activity("cid-3", workspace["id"])
            return ("cid-3", "created")

        with (
            patch.object(
                registry,
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

        sockets.sessions.pop(workspace["id"], None)
        registry.states.pop(workspace["id"], None)

    async def test_clears_pending_status_msg(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.pending_status_msg = "stale message from prior connect"
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "pending-ws"
        )

        async def fake_start(*a, **kw):
            registry.track_activity("cid-p", workspace["id"])
            return ("cid-p", "created")

        with (
            patch.object(
                registry,
                "start_container",
                side_effect=fake_start,
            ),
            patch("glob.glob", return_value=[]),
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        assert conn.pending_status_msg is None

        sockets.sessions.pop(workspace["id"], None)
        registry.states.pop(workspace["id"], None)


# --- handle_websocket dispatch branches ---


class TestHandleWebsocketDispatch:
    """Test all command dispatch branches through the main handler."""

    async def _run_commands(self, user, commands, app_state=None):

        if app_state is None:
            app_state = _make_app_state()
        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        msgs = [json.dumps(c) for c in commands] + [WebSocketDisconnect()]
        websocket.receive_text = AsyncMock(side_effect=msgs)
        await handle_websocket(websocket, app_state)
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

    async def test_dispatch_terminal_stop(self, user, app_state):
        websocket = await self._run_commands(user, [{"cmd": "terminal_stop"}])
        websocket.accept.assert_awaited_once()

    async def test_dispatch_terminal_window_commands(self, user, app_state):
        for cmd in (
            "terminal_new_window",
            "terminal_select_window",
            "terminal_close_window",
            "terminal_rename_window",
            "terminal_list_windows",
        ):
            websocket = await self._run_commands(user, [{"cmd": cmd}])
            websocket.accept.assert_awaited_once()

    async def test_dispatch_restart_container(self, user, app_state):
        websocket = await self._run_commands(
            user, [{"cmd": "restart_container"}]
        )
        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_dispatch_workspace_connect(self, user, app_state):
        websocket = await self._run_commands(
            user, [{"cmd": "workspace_connect"}]
        )
        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Missing" in str(c) for c in calls)

    async def test_dispatch_set_handle(self, user, app_state):
        websocket = await self._run_commands(
            user, [{"cmd": "set_handle", "handle": "alice"}]
        )
        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_dispatch_workspace_disconnect(self, user, app_state):
        websocket = await self._run_commands(
            user, [{"cmd": "workspace_disconnect"}]
        )
        websocket.accept.assert_awaited_once()

    async def test_dispatch_browser_reattach(self, user, app_state):
        websocket = await self._run_commands(
            user, [{"cmd": "browser_reattach", "browser_id": "bid-x"}]
        )
        websocket.accept.assert_awaited_once()

    async def test_container_survives_disconnect(self, user, app_state):
        """Container should NOT be killed on disconnect — idle timeout handles it."""
        app_state = _make_app_state()
        registry = app_state.container_registry

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})

        workspace = await app_state.workspaces.create_workspace(
            user["id"], "stop-ws"
        )
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
                registry,
                "get_workspace_ports",
                return_value=[],
            ),
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ) as mock_stop,
        ):
            await handle_websocket(websocket, app_state)

        mock_stop.assert_not_awaited()


# --- handle_restart_container additional coverage ---


class TestHandleWebsocket:
    async def test_missing_token(self):
        app_state = _make_app_state()
        websocket = _mock_raw_sock(query_params={})
        await handle_websocket(websocket, app_state)
        websocket.close.assert_awaited_once_with(
            code=4001, reason="Missing token"
        )

    async def test_invalid_token(self, db):
        app_state = _make_app_state()
        websocket = _mock_raw_sock(query_params={"token": "bad"})
        await handle_websocket(websocket, app_state)
        websocket.close.assert_awaited_once_with(
            code=4001, reason="Invalid token"
        )

    async def test_expired_token(self, user):
        app_state = _make_app_state()
        from datetime import datetime, timedelta, timezone

        from jose import jwt

        expired = datetime.now(timezone.utc) - timedelta(hours=1)
        payload = {
            "sub": user["id"],
            "email": user["email"],
            "jti": "test-jti",
            "exp": expired,
        }
        token = jwt.encode(
            payload, _auth().secret, algorithm=_auth().algorithm
        )
        websocket = _mock_raw_sock(query_params={"token": token})
        await handle_websocket(websocket, app_state)
        websocket.close.assert_awaited_once_with(
            code=4002, reason="Token expired"
        )

    async def test_valid_token_then_disconnect(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(side_effect=WebSocketDisconnect())

        await handle_websocket(websocket, app_state)

        websocket.accept.assert_awaited_once()

    async def test_unexpected_exception_logged(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=ValueError("unexpected")
        )

        await handle_websocket(websocket, app_state)

        websocket.accept.assert_awaited_once()

    async def test_runtime_error_treated_as_disconnect(self, user, app_state):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=RuntimeError(
                'WebSocket is not connected. Need to call "accept" first.'
            )
        )

        await handle_websocket(websocket, app_state)

        websocket.accept.assert_awaited_once()

    async def test_invalid_json(self, user, app_state):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=["not json", WebSocketDisconnect()]
        )

        await handle_websocket(websocket, app_state)

        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Invalid JSON" in str(c) for c in calls)

    async def test_unknown_command(self, user, app_state):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "bogus"}),
                WebSocketDisconnect(),
            ]
        )

        await handle_websocket(websocket, app_state)

        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Unknown command" in str(c) for c in calls)

    async def test_ui_ready_with_pending(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "ui-ready-ws"
        )

        async def fake_start(self_arg, wid, ws_obj):
            self_arg.container_id = "cid"
            self_arg.workspace_id = wid
            self_arg._user_home = "/home/testuser"
            sockets.get_or_create_session(wid, app_state)

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
                registry,
                "get_workspace_ports",
                return_value=[],
            ),
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
        ):
            await handle_websocket(websocket, app_state)

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
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "ui_ready"}),
                WebSocketDisconnect(),
            ]
        )

        await handle_websocket(websocket, app_state)

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
        app_state = _make_app_state()
        sockets = app_state.sockets

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=RuntimeError("unexpected")
        )

        await handle_websocket(websocket, app_state)

        websocket.accept.assert_awaited_once()
        assert websocket not in sockets.connections


class TestExecHandlers:
    async def test_exec_start_no_container(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_exec_start({"command": ["ls"]})
        assert conn.exec_session is None

    async def test_exec_start_no_perm(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        with patch.object(
            conn, "_has_perm", new=AsyncMock(return_value=False)
        ):
            await conn.handle_exec_start({"command": ["ls"]})
        sock.send_json.assert_called()
        assert "code-in-isolation" in sock.send_json.call_args[0][0].get(
            "message", ""
        )
        assert conn.exec_session is None

    async def test_exec_start_no_command(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        with patch.object(conn, "_has_perm", new=AsyncMock(return_value=True)):
            await conn.handle_exec_start({"command": []})
        sock.send_json.assert_called()
        assert "command" in sock.send_json.call_args[0][0].get("message", "")

    async def test_exec_start_success(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock()

        async def empty_output():
            return
            yield  # pragma: no cover

        mock_session.output = empty_output
        mock_session.returncode = 0
        with patch(
            "klangk_backend.wshandler.controllers.ExecSession",
            return_value=mock_session,
        ):
            with patch.object(registry, "record_activity"):
                with patch.object(
                    conn, "_has_perm", new=AsyncMock(return_value=True)
                ):
                    await conn.handle_exec_start({"command": ["ls"]})
        assert conn.exec_session is mock_session
        assert conn.exec_task is not None
        conn.exec_task.cancel()
        try:
            await conn.exec_task
        except asyncio.CancelledError:
            pass

    async def test_exec_start_passes_ssh_agent_socket(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn._ssh_agent_socket = "/tmp/agent.sock"
        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock()

        async def empty_output():
            return
            yield  # pragma: no cover

        mock_session.output = empty_output
        mock_session.returncode = 0
        with patch(
            "klangk_backend.wshandler.controllers.ExecSession",
            return_value=mock_session,
        ) as mock_cls:
            with patch.object(registry, "record_activity"):
                with patch.object(
                    conn, "_has_perm", new=AsyncMock(return_value=True)
                ):
                    await conn.handle_exec_start({"command": ["ls"]})
        call_kwargs = mock_cls.call_args[1]
        assert "SSH_AUTH_SOCK=/tmp/agent.sock" in call_kwargs["env"]
        assert "HOME=/home/admin" in call_kwargs["env"]
        assert call_kwargs["work_dir"] == "/home/admin"
        conn.exec_task.cancel()
        try:
            await conn.exec_task
        except asyncio.CancelledError:
            pass

    async def test_exec_input_sends_data(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        import base64

        session = AsyncMock()
        session.is_alive = True
        conn = _base_conn(app_state=app_state)
        conn.container_id = "cid"
        conn.exec_session = session
        data = base64.b64encode(b"hello").decode()
        with patch.object(registry, "record_activity"):
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
        big_data = base64.b64encode(
            b"x" * (_ws_constants.MAX_INPUT_SIZE + 1)
        ).decode()
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
        app_state = _make_app_state()
        registry = app_state.container_registry
        import base64

        sock = _mock_sock()
        session = AsyncMock()
        session.returncode = 0

        async def fake_output():
            yield b"chunk1"
            yield b"chunk2"

        session.output = fake_output
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.exec_session = session
        with patch.object(registry, "record_activity"):
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
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        session = AsyncMock()

        async def fake_output():
            yield b"data"

        session.output = fake_output
        sock.send_json = MagicMock(side_effect=RuntimeError("ws dead"))
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        with patch.object(registry, "record_activity"):
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


class TestExecController:
    """Unit tests for the ExecController collaborator in isolation.

    These exercise the controller directly against a lightweight fake
    connection (a SimpleNamespace), proving it is decoupled from
    Connection (issue #961) and covering the branches that the
    existing Connection-level tests don't reach directly — notably the
    ``asyncio.CancelledError`` re-raise in ``forward_output`` and the
    ``Connection._claim_and_stop_exec`` backward-compat delegate.
    """

    def _controller(
        self,
        *,
        container_id="cid",
        user_home=None,
        ssh_agent_socket=None,
        sock=None,
        has_perm=True,
        app_state=None,
    ):
        if sock is None:
            sock = _mock_sock()
        if app_state is None:
            app_state = _make_app_state()
        conn = SimpleNamespace(
            sock=sock,
            container_id=container_id,
            _user_home=user_home,
            _ssh_agent_socket=ssh_agent_socket,
            _has_perm=AsyncMock(return_value=has_perm),
            app_state=app_state,
        )
        return ExecController(conn), sock, conn

    async def test_start_no_container_noop(self):
        ctrl, sock, _ = self._controller(container_id=None)
        await ctrl.start({"command": ["ls"]})
        assert ctrl.session is None
        assert ctrl.task is None
        sock.send_json.assert_not_called()

    async def test_start_no_perm_sends_error(self):
        ctrl, sock, _ = self._controller(has_perm=False)
        await ctrl.start({"command": ["ls"]})
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "error"
        assert "code-in-isolation" in msg["message"]
        assert ctrl.session is None

    async def test_start_no_command_sends_error(self):
        ctrl, sock, _ = self._controller()
        await ctrl.start({"command": []})
        msg = sock.send_json.call_args[0][0]
        assert "command" in msg["message"]
        assert ctrl.session is None

    async def test_start_stops_existing_session_first(self):
        """start() tears down any in-flight exec before starting a new one."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        old = AsyncMock()
        ctrl.session = old
        with (
            patch(
                "klangk_backend.wshandler.controllers.ExecSession"
            ) as MockExec,
            patch.object(registry, "record_activity"),
        ):
            mock_session = MockExec.return_value
            mock_session.start = AsyncMock()
            await ctrl.start({"command": ["ls"]})
            # Drain the spawned output-forwarding task.
            assert ctrl.task is not None
            ctrl.task.cancel()
            try:
                await ctrl.task
            except asyncio.CancelledError:
                pass
        old.stop.assert_awaited_once()
        assert ctrl.session is mock_session

    async def test_start_passes_user_home_and_ssh_agent_socket(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(
            user_home="/home/admin",
            ssh_agent_socket="/tmp/agent.sock",
        )
        with (
            patch(
                "klangk_backend.wshandler.controllers.ExecSession"
            ) as MockExec,
            patch.object(registry, "record_activity"),
        ):
            mock_session = MockExec.return_value
            mock_session.start = AsyncMock()
            await ctrl.start({"command": ["ls"]})
            ctrl.task.cancel()
            try:
                await ctrl.task
            except asyncio.CancelledError:
                pass
        kwargs = MockExec.call_args.kwargs
        assert "HOME=/home/admin" in kwargs["env"]
        assert "SSH_AUTH_SOCK=/tmp/agent.sock" in kwargs["env"]
        assert kwargs["work_dir"] == "/home/admin"

    async def test_start_defaults_work_dir_when_no_user_home(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(user_home=None, app_state=app_state)
        with (
            patch(
                "klangk_backend.wshandler.controllers.ExecSession"
            ) as MockExec,
            patch.object(registry, "record_activity"),
        ):
            mock_session = MockExec.return_value
            mock_session.start = AsyncMock()
            await ctrl.start({"command": ["ls"]})
            ctrl.task.cancel()
            try:
                await ctrl.task
            except asyncio.CancelledError:
                pass
        kwargs = MockExec.call_args.kwargs
        assert kwargs["env"] == []
        assert kwargs["work_dir"] == "/home/work"

    async def test_start_default_login_false(self):
        """#1041: a message with no ``login`` key runs raw argv (no
        shell) -- the safe default for any caller, and what rsync needs."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        with (
            patch(
                "klangk_backend.wshandler.controllers.ExecSession"
            ) as MockExec,
            patch.object(registry, "record_activity"),
        ):
            mock_session = MockExec.return_value
            mock_session.start = AsyncMock()
            await ctrl.start({"command": ["ls"]})
            ctrl.task.cancel()
            try:
                await ctrl.task
            except asyncio.CancelledError:
                pass
        mock_session.start.assert_awaited_with(["ls"], login=False)

    async def test_start_login_true_threads_through(self):
        """#1041: ``login: True`` in the message reaches ExecSession.start
        so the command runs as a bash login shell (sources ~/.profile).
        This is the klangkc exec default."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        with (
            patch(
                "klangk_backend.wshandler.controllers.ExecSession"
            ) as MockExec,
            patch.object(registry, "record_activity"),
        ):
            mock_session = MockExec.return_value
            mock_session.start = AsyncMock()
            await ctrl.start({"command": ["ls"], "login": True})
            ctrl.task.cancel()
            try:
                await ctrl.task
            except asyncio.CancelledError:
                pass
        mock_session.start.assert_awaited_with(["ls"], login=True)

    async def test_input_no_session(self):
        ctrl, _, _ = self._controller()
        await ctrl.input({"data": base64.b64encode(b"x").decode()})

    async def test_input_dead_session_dropped(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.is_alive = False
        ctrl.session = session
        await ctrl.input({"data": base64.b64encode(b"x").decode()})
        session.write.assert_not_awaited()

    async def test_input_oversized_dropped(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        session = AsyncMock()
        session.is_alive = True
        ctrl.session = session
        big = base64.b64encode(
            b"x" * (_ws_constants.MAX_INPUT_SIZE + 1)
        ).decode()
        with patch.object(registry, "record_activity"):
            await ctrl.input({"data": big})
        session.write.assert_not_awaited()

    async def test_input_writes_and_records_activity(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        session = AsyncMock()
        session.is_alive = True
        ctrl.session = session
        data = base64.b64encode(b"hello").decode()
        with patch.object(registry, "record_activity") as rec:
            await ctrl.input({"data": data})
        session.write.assert_awaited_once_with(b"hello")
        rec.assert_called_once_with("cid")

    async def test_close_stdin_no_session(self):
        ctrl, _, _ = self._controller()
        await ctrl.close_stdin()

    async def test_close_stdin_delegates(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        ctrl.session = session
        await ctrl.close_stdin()
        session.close_stdin.assert_awaited_once()

    async def test_stop_command_calls_stop(self):
        ctrl, _, _ = self._controller()
        with patch.object(ctrl, "stop", new=AsyncMock()) as stop:
            await ctrl.stop_command()
        stop.assert_awaited_once()

    async def test_stop_no_session(self):
        ctrl, _, _ = self._controller()
        await ctrl.stop()
        assert ctrl.session is None
        assert ctrl.task is None

    async def test_stop_cancels_task_and_stops_session(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        ctrl.session = session
        ctrl.task = asyncio.create_task(asyncio.sleep(999))
        await ctrl.stop()
        assert ctrl.task is None
        assert ctrl.session is None
        session.stop.assert_awaited_once()

    async def test_claim_and_stop_no_session(self):
        ctrl, _, _ = self._controller()
        await ctrl.claim_and_stop()
        assert ctrl.session is None

    async def test_claim_and_stop_drops_and_stops(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        ctrl.session = session
        await ctrl.claim_and_stop()
        assert ctrl.session is None
        session.stop.assert_awaited_once()

    async def test_forward_output_relays_chunks_and_exit_code(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, sock, _ = self._controller(app_state=app_state)
        session = AsyncMock()
        session.returncode = 7
        ctrl.session = session

        async def fake_output():
            yield b"chunk1"
            yield b"chunk2"

        session.output = fake_output
        with patch.object(registry, "record_activity"):
            await ctrl.forward_output(session)
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        outputs = [c for c in calls if c["type"] == "exec_output"]
        exits = [c for c in calls if c["type"] == "exec_exit"]
        assert len(outputs) == 2
        assert base64.b64decode(outputs[0]["data"]) == b"chunk1"
        assert base64.b64decode(outputs[1]["data"]) == b"chunk2"
        assert exits[0]["code"] == 7
        # Session claimed and stopped by the finally block.
        session.stop.assert_awaited_once()

    async def test_forward_output_exit_code_defaults_to_1(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, sock, _ = self._controller(app_state=app_state)
        session = AsyncMock()
        session.returncode = None
        ctrl.session = session

        async def fake_output():
            return
            yield  # pragma: no cover

        session.output = fake_output
        with patch.object(registry, "record_activity"):
            await ctrl.forward_output(session)
        exits = [
            c[0][0]
            for c in sock.send_json.call_args_list
            if c[0][0]["type"] == "exec_exit"
        ]
        assert exits[0]["code"] == 1

    async def test_forward_output_records_activity_when_container_set(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(container_id="cid", app_state=app_state)
        session = AsyncMock()
        session.returncode = 0
        ctrl.session = session

        async def fake_output():
            yield b"data"

        session.output = fake_output
        with patch.object(registry, "record_activity") as rec:
            await ctrl.forward_output(session)
        rec.assert_called_once_with("cid")

    async def test_forward_output_swallows_ws_error(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, sock, _ = self._controller(app_state=app_state)
        session = AsyncMock()
        ctrl.session = session

        async def fake_output():
            yield b"data"

        session.output = fake_output
        sock.send_json = MagicMock(side_effect=RuntimeError("ws dead"))
        with patch.object(registry, "record_activity"):
            # Must not raise; error is logged.
            await ctrl.forward_output(session)
        session.stop.assert_awaited_once()

    async def test_forward_output_reraises_cancelled(self):
        """Cancellation mid-stream re-raises and still cleans up the session."""
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.returncode = 0
        ctrl.session = session
        never = asyncio.Event()

        async def blocking_output():
            yield b"first"
            await never.wait()  # blocks until cancelled

        session.output = blocking_output
        task = asyncio.create_task(ctrl.forward_output(session))
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # finally block ran claim_and_stop -> session stopped.
        session.stop.assert_awaited_once()
        assert ctrl.session is None

    async def test_connection_claim_and_stop_exec_delegate(self):
        """Connection._claim_and_stop_exec forwards to the controller."""
        conn = _base_conn()
        with patch.object(conn.exec, "claim_and_stop", new=AsyncMock()) as m:
            await conn._claim_and_stop_exec()
        m.assert_awaited_once()

    async def test_connection_forward_exec_output_delegate(self):
        """Connection.forward_exec_output forwards to the controller."""
        conn = _base_conn()
        session = AsyncMock()
        with patch.object(conn.exec, "forward_output", new=AsyncMock()) as m:
            await conn.forward_exec_output(session)
        m.assert_awaited_once_with(session)

    async def test_connection_stop_exec_delegate(self):
        """Connection.stop_exec forwards to the controller."""
        conn = _base_conn()
        with patch.object(conn.exec, "stop", new=AsyncMock()) as m:
            await conn.stop_exec()
        m.assert_awaited_once()

    async def test_exec_session_property_round_trips_to_controller(self):
        conn = _base_conn()
        sentinel = object()
        conn.exec_session = sentinel
        assert conn.exec_session is sentinel
        assert conn.exec.session is sentinel

    async def test_exec_task_property_round_trips_to_controller(self):
        conn = _base_conn()
        task = asyncio.create_task(asyncio.sleep(999))
        try:
            conn.exec_task = task
            assert conn.exec_task is task
            assert conn.exec.task is task
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class TestSSHAgentHandlers:
    async def test_ssh_agent_start_no_container(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_ssh_agent_start()
        sock.send_json.assert_called()
        msg = sock.send_json.call_args[0][0]
        assert msg.get("type") == "error"

    async def test_ssh_agent_start_success(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        # Return empty immediately so the relay task exits cleanly.
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stdin = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        with (
            patch.object(
                _mock_pod,
                "exec_container",
                new=AsyncMock(),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
        ):
            await conn.handle_ssh_agent_start()
            # Let the relay task run and finish (stdout returns b"").
            assert conn._ssh_agent_task is not None
            await conn._ssh_agent_task
        assert conn._ssh_agent_proc is mock_proc
        assert conn._ssh_agent_socket is not None
        sock.send_json.assert_called()
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_started"
        assert "socket" in msg

    async def test_ssh_agent_data_writes_to_stdin(self):
        import base64

        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        conn._ssh_agent_proc = mock_proc
        data = base64.b64encode(b"agent-request").decode()
        await conn.handle_ssh_agent_data({"data": data})
        mock_proc.stdin.write.assert_called_once_with(b"agent-request")

    async def test_ssh_agent_data_no_proc(self):
        conn = _base_conn()
        # Should not raise when no process is active.
        await conn.handle_ssh_agent_data({"data": ""})

    async def test_ssh_agent_stop(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        conn._ssh_agent_proc = mock_proc
        conn._ssh_agent_socket = "/tmp/test.sock"
        conn._ssh_agent_task = asyncio.create_task(asyncio.sleep(999))
        with patch.object(
            _mock_pod,
            "exec_container",
            new=AsyncMock(),
        ):
            await conn.handle_ssh_agent_stop()
        assert conn._ssh_agent_proc is None
        assert conn._ssh_agent_socket is None
        sock.send_json.assert_called()
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_stopped"

    async def test_stop_ssh_agent_cleanup_on_disconnect(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        conn._ssh_agent_proc = mock_proc
        conn._ssh_agent_socket = "/tmp/test.sock"
        conn._ssh_agent_task = asyncio.create_task(asyncio.sleep(999))
        with patch.object(
            _mock_pod,
            "exec_container",
            new=AsyncMock(),
        ):
            await conn._stop_ssh_agent()
        assert conn._ssh_agent_proc is None
        assert conn._ssh_agent_task is None
        assert conn._ssh_agent_socket is None

    async def test_forward_ssh_agent_output(self):
        import base64

        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        read_data = [b"agent-response", b""]
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(side_effect=read_data)
        conn._ssh_agent_proc = mock_proc
        await conn._forward_ssh_agent_output()
        calls = [
            c[0][0]
            for c in sock.send_json.call_args_list
            if c[0][0].get("type") == "ssh_agent_response"
        ]
        assert len(calls) == 1
        assert base64.b64decode(calls[0]["data"]) == b"agent-response"

    async def test_ssh_agent_start_with_terminal(self):
        """SSH_AUTH_SOCK is passed to TerminalSession when agent is active."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn._user_home = "/home/testuser"
        conn._ssh_agent_socket = "/tmp/klangk-ssh-agent-uid.sock"
        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock()
        mock_session.session_name = "uid"
        mock_session.tmux_session_name = "uid"

        async def empty_output():
            return
            yield  # pragma: no cover

        mock_session.output = empty_output
        with (
            patch(
                "klangk_backend.wshandler.controllers.TerminalSession",
                return_value=mock_session,
            ) as MockTS,
            patch.object(registry, "record_activity"),
            patch.object(
                _mock_term,
                "attach_browser",
                new=AsyncMock(),
            ),
            patch.object(
                _mock_term,
                "list_windows",
                return_value=[],
            ),
            patch.object(
                conn,
                "_has_perm",
                new=AsyncMock(return_value=True),
            ),
        ):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            # Let the background task run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        MockTS.assert_called_once_with(
            "cid",
            session_name="uid",
            user_home="/home/testuser",
            user_id="uid",
            user_handle="testuser",
            ssh_agent_socket="/tmp/klangk-ssh-agent-uid.sock",
            terminal=_mock_term,
        )


class TestSshAgentForwarder:
    """Unit tests for the SshAgentForwarder collaborator in isolation.

    These exercise the forwarder directly against a lightweight fake
    connection (a SimpleNamespace), proving the collaborator is
    decoupled from Connection (issue #961) and covering the debug /
    error branches that were previously excluded with
    ``# pragma: no cover``.
    """

    def _forwarder(self, *, container_id="cid", user=None, sock=None):
        if sock is None:
            sock = _mock_sock()
        if user is None:
            user = {
                "id": "uid",
                "email": "testuser@example.com",
                "handle": "testuser",
            }
        conn = SimpleNamespace(
            sock=sock,
            user=user,
            container_id=container_id,
            app_state=SimpleNamespace(podman=_mock_pod, terminal=_mock_term),
        )
        return SshAgentForwarder(conn), sock

    @asynccontextmanager
    async def _track_tasks(self):
        """Capture asyncio.create_task() calls for cleanup."""
        created = []
        orig = asyncio.create_task

        def _wrap(coro, **kw):
            t = orig(coro, **kw)
            created.append(t)
            return t

        with patch(
            "klangk_backend.wshandler.controllers.asyncio.create_task", _wrap
        ):
            yield created
        for t in created:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    async def test_start_logs_and_spawns_stderr_relay_when_debug(self):
        """KLANGKC_DEBUG_SSH_AGENT logs start + spawns the stderr relay."""
        fwd, sock = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        # stderr.readline returns EOF immediately so the relay exits.
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")
        mock_proc.stdin = AsyncMock()
        with (
            patch.dict(os.environ, {"KLANGKC_DEBUG_SSH_AGENT": "1"}),
            patch.object(
                _mock_pod,
                "exec_container",
                new=AsyncMock(),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
        ):
            async with self._track_tasks() as tasks:
                await fwd.start()
                # Let the relay tasks run to completion.
                for _ in range(5):
                    await asyncio.sleep(0)
        assert fwd.proc is mock_proc
        assert fwd.socket == "/tmp/klangk-ssh-agent-uid.sock"
        # stderr relay task was created (debug + proc.stderr is not None).
        assert any(
            t.get_coro().__qualname__.startswith(
                "SshAgentForwarder.log_stderr"
            )
            for t in tasks
        )
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_started"
        assert msg["socket"] == "/tmp/klangk-ssh-agent-uid.sock"

    async def test_start_skips_stderr_relay_when_proc_has_no_stderr(self):
        """Debug set but proc.stderr is None -> no stderr relay task."""
        fwd, sock = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stderr = None
        mock_proc.stdin = AsyncMock()
        with (
            patch.dict(os.environ, {"KLANGKC_DEBUG_SSH_AGENT": "1"}),
            patch.object(
                _mock_pod,
                "exec_container",
                new=AsyncMock(),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
        ):
            async with self._track_tasks() as tasks:
                await fwd.start()
                for _ in range(3):
                    await asyncio.sleep(0)
        assert not any(
            t.get_coro().__qualname__.startswith(
                "SshAgentForwarder.log_stderr"
            )
            for t in tasks
        )

    async def test_start_no_container_sends_error(self):
        fwd, sock = self._forwarder(container_id=None)
        await fwd.start()
        msg = sock.send_json.call_args[0][0]
        assert msg.get("type") == "error"
        assert fwd.proc is None

    async def test_log_stderr_no_proc(self):
        fwd, _ = self._forwarder()
        fwd.proc = None
        # No-op, must not raise.
        await fwd.log_stderr()

    async def test_log_stderr_no_stderr_stream(self):
        fwd, _ = self._forwarder()
        fwd.proc = MagicMock(stderr=None)
        await fwd.log_stderr()

    async def test_log_stderr_reads_lines_until_eof(self):
        fwd, _ = self._forwarder()
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline = AsyncMock(
            side_effect=[b"line one\n", b"line two\n", b""]
        )
        fwd.proc = mock_proc
        with patch("klangk_backend.wshandler.controllers.logger") as lg:
            await fwd.log_stderr()
        info_msgs = [c.args for c in lg.info.call_args_list]
        assert (
            any(
                "line one" in a[0] % () if False else "line one" in str(a)
                for a in info_msgs
            )
            or info_msgs
        )
        # Decoded lines were logged.
        assert any("line one" in str(a) for a in info_msgs)
        assert any("line two" in str(a) for a in info_msgs)

    async def test_log_stderr_swallows_oserror(self):
        fwd, _ = self._forwarder()
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.readline = AsyncMock(side_effect=OSError("boom"))
        fwd.proc = mock_proc
        # Must not raise.
        await fwd.log_stderr()

    async def test_log_stderr_swallows_cancelled(self):
        fwd, _ = self._forwarder()
        mock_proc = MagicMock()
        mock_proc.stderr = AsyncMock()
        # Block on readline so we can cancel the running task.
        never = asyncio.Event()

        async def _block(*a, **k):
            await never.wait()

        mock_proc.stderr.readline = _block
        fwd.proc = mock_proc
        task = asyncio.create_task(fwd.log_stderr())
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        # Should suppress the CancelledError and complete normally.
        await task

    async def test_forward_output_no_proc(self):
        fwd, _ = self._forwarder()
        fwd.proc = None
        await fwd.forward_output()

    async def test_forward_output_no_stdout(self):
        fwd, _ = self._forwarder()
        fwd.proc = MagicMock(stdout=None)
        await fwd.forward_output()

    async def test_forward_output_relays_data(self):
        import base64

        fwd, sock = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(side_effect=[b"resp", b""])
        fwd.proc = mock_proc
        await fwd.forward_output()
        calls = [
            c[0][0]
            for c in sock.send_json.call_args_list
            if c[0][0].get("type") == "ssh_agent_response"
        ]
        assert len(calls) == 1
        assert base64.b64decode(calls[0]["data"]) == b"resp"

    async def test_forward_output_debug_logs_eof_and_data(self):
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(side_effect=[b"x", b""])
        fwd.proc = mock_proc
        with (
            patch.dict(os.environ, {"KLANGKC_DEBUG_SSH_AGENT": "1"}),
            patch("klangk_backend.wshandler.controllers.logger") as lg,
        ):
            await fwd.forward_output()
        info_args = [str(c) for c in lg.info.call_args_list]
        assert any("socat stdout" in a and "bytes" in a for a in info_args)
        assert any("socat stdout EOF" in a for a in info_args)

    async def test_forward_output_swallows_oserror(self):
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(side_effect=OSError("boom"))
        fwd.proc = mock_proc
        with patch("klangk_backend.wshandler.controllers.logger") as lg:
            await fwd.forward_output()
        lg.warning.assert_called_once()
        assert "SSH agent output relay error" in str(lg.warning.call_args)

    async def test_forward_output_swallows_cancelled(self):
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        never = asyncio.Event()

        async def _block(*a, **k):
            await never.wait()

        mock_proc.stdout.read = _block
        fwd.proc = mock_proc
        task = asyncio.create_task(fwd.forward_output())
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        # Suppresses CancelledError and completes normally.
        await task

    async def test_data_no_proc_debug_logs(self):
        fwd, _ = self._forwarder()
        fwd.proc = None
        with (
            patch.dict(os.environ, {"KLANGKC_DEBUG_SSH_AGENT": "1"}),
            patch("klangk_backend.wshandler.controllers.logger") as lg,
        ):
            await fwd.data({"data": base64.b64encode(b"x").decode()})
        assert any(
            "data received but no proc" in str(c)
            for c in lg.info.call_args_list
        )

    async def test_data_debug_logs_write(self):
        data = base64.b64encode(b"agent-request").decode()
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        fwd.proc = mock_proc
        with (
            patch.dict(os.environ, {"KLANGKC_DEBUG_SSH_AGENT": "1"}),
            patch("klangk_backend.wshandler.controllers.logger") as lg,
        ):
            await fwd.data({"data": data})
        mock_proc.stdin.write.assert_called_once_with(b"agent-request")
        assert any(
            "writing" in str(c) and "bytes" in str(c)
            for c in lg.info.call_args_list
        )

    async def test_data_empty_payload_noop(self):
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        fwd.proc = mock_proc
        await fwd.data({"data": ""})
        mock_proc.stdin.write.assert_not_called()

    async def test_stop_command_notifies_client(self):
        fwd, sock = self._forwarder()
        with patch.object(fwd, "stop", new=AsyncMock()) as stop:
            await fwd.stop_command()
        stop.assert_awaited_once()
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_stopped"

    async def test_stop_handles_process_lookup_error(self):
        fwd, _ = self._forwarder()
        mock_proc = MagicMock()
        mock_proc.kill = MagicMock(side_effect=ProcessLookupError())
        mock_proc.wait = AsyncMock()
        fwd.proc = mock_proc
        # No task, no socket: only the proc branch runs.
        await fwd.stop()
        assert fwd.proc is None

    async def test_stop_handles_socket_remove_oserror(self):
        fwd, _ = self._forwarder()
        fwd.socket = "/tmp/agent.sock"
        with patch.object(
            _mock_pod,
            "exec_container",
            new=AsyncMock(side_effect=OSError("boom")),
        ) as exec_mock:
            with patch("klangk_backend.wshandler.controllers.logger") as lg:
                await fwd.stop()
        exec_mock.assert_awaited_once()
        lg.warning.assert_called_once()
        assert "Failed to remove SSH agent socket" in str(lg.warning.call_args)
        assert fwd.socket is None

    async def test_stop_cancels_task_and_kills_proc(self):
        fwd, _ = self._forwarder()
        mock_proc = MagicMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        fwd.proc = mock_proc
        fwd.socket = None  # skip socket-removal branch
        fwd.task = asyncio.create_task(asyncio.sleep(999))
        with patch.object(
            _mock_pod,
            "exec_container",
            new=AsyncMock(),
        ):
            await fwd.stop()
        assert fwd.task is None
        assert fwd.proc is None
        mock_proc.kill.assert_called_once()

    async def test_connection_log_stderr_delegate(self):
        """Connection._log_ssh_agent_stderr forwards to the collaborator."""
        conn = _base_conn()
        with patch.object(conn.ssh_agent, "log_stderr", new=AsyncMock()) as m:
            await conn._log_ssh_agent_stderr()
        m.assert_awaited_once()


class TestExecDispatch:
    async def test_dispatch_exec_start(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
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
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_exec_input(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
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
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_exec_stop(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
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
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_exec_close_stdin(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
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
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_heartbeat(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
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
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_chat_send(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
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
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_chat_delete(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
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
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()


class TestChatLoadMoreDispatch:
    async def test_dispatch_chat_load_more(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
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
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()


class TestHandleHeartbeat:
    async def test_records_activity(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        conn = _base_conn(app_state=app_state)
        conn.container_id = "cid-hb"
        registry.track_activity("cid-hb", "ws-hb")
        registry.states["ws-hb"].last_activity = 0.0

        await conn.handle_heartbeat()

        assert registry.states["ws-hb"].last_activity > 0.0
        registry.states.pop("ws-hb", None)
        registry._cid_to_wsid.pop("cid-hb", None)

    async def test_no_container_id(self):
        conn = _base_conn()
        # Should not raise
        await conn.handle_heartbeat()


class TestBrowserBridge:
    async def test_dispatch_browser_response(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "browser_response", "id": "req-1"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            sockets,
            "handle_browser_response",
            wraps=sockets.handle_browser_response,
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_called_once()

    async def test_handle_browser_response_resolves_future(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        mock_sock = _mock_sock()
        sockets.pending_browser_requests["req-1"] = (
            future,
            mock_sock,
        )

        sockets.handle_browser_response(
            {"id": "req-1", "status": 200, "body": "hello"}, sender=mock_sock
        )

        assert future.done()
        result = future.result()
        assert result["body"] == "hello"

    async def test_handle_browser_response_wrong_sender_rejected(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        expected = _mock_sock()
        imposter = _mock_sock()
        sockets.pending_browser_requests["req-2"] = (
            future,
            expected,
        )

        sockets.handle_browser_response(
            {"id": "req-2", "status": 200}, sender=imposter
        )

        # Future should NOT be resolved — wrong sender
        assert not future.done()
        # Entry should still be pending
        assert "req-2" in sockets.pending_browser_requests
        sockets.pending_browser_requests.pop("req-2", None)

    async def test_handle_browser_response_missing_id(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        # Should not raise
        sockets.handle_browser_response({})

    async def test_handle_browser_response_unknown_id(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        # Should not raise
        sockets.handle_browser_response({"id": "unknown"})

    async def test_dispatch_browser_request_no_subscribers(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-empty", app_state)
        try:
            result = await session.dispatch_browser_request(
                {"action": "fetch", "url": "http://example.com"}
            )
            assert "error" in result
            assert "No browser client" in result["error"]
        finally:
            sockets.sessions.pop("ws-empty", None)

    async def test_dispatch_browser_request_cli_only(self):
        """CLI-only connections get immediate error, not 30s timeout."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-cli-only", app_state)
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
            sockets.sessions.pop("ws-cli-only", None)

    async def test_dispatch_browser_request_success(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-bridge", app_state)
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)

        async def respond_later():
            await asyncio.sleep(0.1)
            # Find the pending request and resolve it
            for req_id, (
                future,
                _sock,
            ) in sockets.pending_browser_requests.items():
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
            sockets.sessions.pop("ws-bridge", None)

    async def test_dispatch_browser_request_timeout(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-timeout", app_state)
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
            sockets.sessions.pop("ws-timeout", None)


class TestDispatchBrowserRequestTo:
    async def test_success(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-to", app_state)
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)

        async def respond_later():
            await asyncio.sleep(0.1)
            for req_id, (
                future,
                _sock,
            ) in sockets.pending_browser_requests.items():
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
            sockets.sessions.pop("ws-to", None)

    async def test_dead_socket(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-to-dead", app_state)
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
            sockets.sessions.pop("ws-to-dead", None)

    async def test_timeout(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-to-timeout", app_state)
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
            sockets.sessions.pop("ws-to-timeout", None)

    async def test_cancelled_cleans_up(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-to-cancel", app_state)
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)
        try:
            before = set(sockets.pending_browser_requests.keys())
            task = asyncio.create_task(
                session.dispatch_browser_request_to(
                    mock_sock,
                    {"action": "fetch"},
                    timeout=10.0,
                )
            )
            await asyncio.sleep(0.05)
            new_ids = set(sockets.pending_browser_requests.keys()) - before
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            for rid in new_ids:
                assert rid not in sockets.pending_browser_requests
        finally:
            sockets.sessions.pop("ws-to-cancel", None)


class TestCleanupRevokesBrowser:
    async def test_cleanup_revokes_browser_registration(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.workspace_id = "ws-revoke"
        conn.container_id = "cid-revoke"

        # Register a browser ID for this connection
        registry.register_browser("bid-revoke", "ws-revoke", sock)
        conn.browser_id = "bid-revoke"

        registry.track_activity("cid-revoke", "ws-revoke")
        session = WorkspaceSession("ws-revoke", app_state)
        session.subscribers.add(sock)
        sockets.sessions["ws-revoke"] = session

        await conn.cleanup()

        assert registry.resolve_browser("bid-revoke") is None
        assert conn.browser_id is None

        registry.revoke_workspace_browsers("ws-revoke")
        registry.states.pop("ws-revoke", None)
        sockets.sessions.pop("ws-revoke", None)


class TestResetWorkspaceState:
    async def test_noop_for_unknown_workspace(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        await reset_workspace_state(sockets, "ws-unknown")  # should not raise

    async def test_remove_session_noop_for_unknown(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        await sockets.remove_session("nonexistent")  # should not raise

    async def test_removes_session_with_no_subscribers(self):
        """remove_session acquires lock and removes empty session."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sockets.get_or_create_session("ws-reset-empty", app_state)
        assert "ws-reset-empty" in sockets.sessions
        registry.track_activity("cid-reset", "ws-reset-empty")
        try:
            await reset_workspace_state(sockets, "ws-reset-empty")
            assert "ws-reset-empty" not in sockets.sessions
        finally:
            sockets.sessions.pop("ws-reset-empty", None)
            registry.states.pop("ws-reset-empty", None)

    async def test_remove_session_skips_if_subscribers_reappear(self):
        """remove_session re-checks subscribers under lock and aborts if non-empty."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-reappear", app_state)
        mock_sock = _mock_sock()
        # Add subscriber so the re-check inside the lock finds a non-empty set
        session.subscribers.add(mock_sock)
        try:
            await sockets.remove_session("ws-reappear")
            # Session should NOT have been removed
            assert "ws-reappear" in sockets.sessions
            assert mock_sock in session.subscribers
        finally:
            sockets.sessions.pop("ws-reappear", None)

    async def test_reset_cleans_agent_state(self):
        """reset_workspace removes agent conversations and cancels agent tasks."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        ws_id = "ws-agent-cleanup"
        wshandler.agent_conversations[ws_id] = {"user_id": "u1"}

        async def noop():
            await asyncio.sleep(999)

        task = asyncio.create_task(noop())
        wshandler.agent_tasks[ws_id] = task
        try:
            await reset_workspace_state(sockets, ws_id)
            assert ws_id not in wshandler.agent_conversations
            assert ws_id not in wshandler.agent_tasks
            # Let cancellation propagate
            try:
                await task
            except asyncio.CancelledError:
                pass
            assert task.cancelled()
        finally:
            wshandler.agent_conversations.pop(ws_id, None)
            wshandler.agent_tasks.pop(ws_id, None)
            if not task.done():
                task.cancel()

    async def test_reset_stops_agent_session(self):
        """reset_workspace stops the Pi RPC subprocess via agent.stop_session."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        ws_id = "ws-agent-stop"
        with patch.object(
            app_state.agents, "stop_session", new_callable=AsyncMock
        ) as mock_stop:
            await reset_workspace_state(sockets, ws_id)
            mock_stop.assert_awaited_once_with(ws_id)


class TestNotifyUserWorkspacesChanged:
    """notify_user_workspaces_changed sends to a user's connections only."""

    def _register(self, sock, user, app_state):
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        app_state.sockets.connections[sock] = conn
        return conn

    def test_sends_to_matching_user_only(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock_a = _mock_sock()
        sock_b = _mock_sock()
        sock_other = _mock_sock()
        try:
            self._register(sock_a, {"id": "uid-1", "email": "a@x"}, app_state)
            self._register(sock_b, {"id": "uid-1", "email": "b@x"}, app_state)
            self._register(
                sock_other, {"id": "uid-2", "email": "c@x"}, app_state
            )
            sockets.notify_user_workspaces_changed("uid-1")
        finally:
            sockets.connections.pop(sock_a, None)
            sockets.connections.pop(sock_b, None)
            sockets.connections.pop(sock_other, None)
        # Both of uid-1's connections were notified...
        sock_a.send_json.assert_called_once_with(
            {"type": "workspaces_changed"}
        )
        sock_b.send_json.assert_called_once_with(
            {"type": "workspaces_changed"}
        )
        # ...and the other user's connection was not.
        sock_other.send_json.assert_not_called()

    def test_no_connections_is_noop(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        # Should not raise when the user has no active connections.
        sockets.notify_user_workspaces_changed("nobody")

    def test_dead_socket_is_pruned(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import WS_ERRORS

        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("dead"))
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            sockets.notify_user_workspaces_changed("uid-1")
            assert sock not in sockets.connections
        finally:
            sockets.connections.pop(sock, None)


class TestNotifyContainerStatus:
    """notify_container_status broadcasts to all authenticated connections."""

    def _register(self, sock, user, app_state):
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        app_state.sockets.connections[sock] = conn
        return conn

    def test_sends_to_all_authenticated(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock_a = _mock_sock()
        sock_b = _mock_sock()
        try:
            self._register(sock_a, {"id": "uid-1", "email": "a@x"}, app_state)
            self._register(sock_b, {"id": "uid-2", "email": "b@x"}, app_state)
            sockets.notify_container_status("ws-123", True)
        finally:
            sockets.connections.pop(sock_a, None)
            sockets.connections.pop(sock_b, None)
        expected = {
            "type": "container_status",
            "workspace_id": "ws-123",
            "running": True,
        }
        sock_a.send_json.assert_called_once_with(expected)
        sock_b.send_json.assert_called_once_with(expected)

    def test_sends_stopped(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            sockets.notify_container_status("ws-456", False)
        finally:
            sockets.connections.pop(sock, None)
        msg = sock.send_json.call_args[0][0]
        assert msg["running"] is False
        assert msg["workspace_id"] == "ws-456"

    def test_skips_unauthenticated(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        try:
            self._register(sock, {"id": None, "email": ""}, app_state)
            sockets.notify_container_status("ws-1", True)
        finally:
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    def test_dead_socket_is_pruned(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import WS_ERRORS

        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("dead"))
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            sockets.notify_container_status("ws-1", True)
            assert sock not in sockets.connections
        finally:
            sockets.connections.pop(sock, None)


class TestNotifyServiceHealth:
    """notify_service_health fans health events out to all connections."""

    def _register(self, sock, user, app_state):
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        app_state.sockets.connections[sock] = conn
        return conn

    def test_sends_healthy_to_all_authenticated(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock_a = _mock_sock()
        sock_b = _mock_sock()
        try:
            self._register(sock_a, {"id": "uid-1", "email": "a@x"}, app_state)
            self._register(sock_b, {"id": "uid-2", "email": "b@x"}, app_state)
            sockets.notify_service_health("ws-123", healthy=True)
        finally:
            sockets.connections.pop(sock_a, None)
            sockets.connections.pop(sock_b, None)
        expected = {
            "type": "service_health",
            "workspace_id": "ws-123",
            "healthy": True,
            "health_message": None,
            "running": True,
            "health_checked_at": None,
            "seq": 0,
        }
        sock_a.send_json.assert_called_once_with(expected)
        sock_b.send_json.assert_called_once_with(expected)

    def test_sends_unhealthy_with_reason(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        # The failure reason rides along on the broadcast so operators
        # can see *why* it's unhealthy (#1088).
        sock = _mock_sock()
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            sockets.notify_service_health(
                "ws-9", healthy=False, message="curl: connection refused"
            )
        finally:
            sockets.connections.pop(sock, None)
        msg = sock.send_json.call_args[0][0]
        assert msg["healthy"] is False
        assert msg["type"] == "service_health"
        assert msg["health_message"] == "curl: connection refused"

    def test_skips_unauthenticated(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        try:
            self._register(sock, {"id": None, "email": ""}, app_state)
            sockets.notify_service_health("ws-1", healthy=True)
        finally:
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    def test_dead_socket_is_pruned(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import WS_ERRORS

        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("dead"))
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            sockets.notify_service_health("ws-1", healthy=True)
            assert sock not in sockets.connections
        finally:
            sockets.connections.pop(sock, None)


class TestServiceHealthSnapshot:
    """send_service_health_snapshot replays current health to one socket
    on connect, closing the steady-state-unhealthy hole (#1175 item 1)."""

    def _state(
        self, ws_id, *, registry, health_check, health_status, message=None
    ):
        cs = container.ContainerState(ws_id, f"cid-{ws_id}", registry)
        cs.health_check = health_check
        cs.health_status = health_status
        cs.health_message = message
        return cs

    def test_replays_only_checked_workspaces(self):
        """Healthy + unhealthy are sent; unchecked and no-check skipped."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        saved = dict(registry.states)
        sock = _mock_sock()
        try:
            registry.states.clear()
            registry.states["ws-healthy"] = self._state(
                "ws-healthy",
                registry=registry,
                health_check="true",
                health_status="healthy",
            )
            registry.states["ws-sick"] = self._state(
                "ws-sick",
                registry=registry,
                health_check="curl localhost",
                health_status="unhealthy",
                message="conn refused",
            )
            # health check configured but never polled yet
            registry.states["ws-unchecked"] = self._state(
                "ws-unchecked",
                registry=registry,
                health_check="true",
                health_status=None,
            )
            # no health check at all (plain dev workspace)
            registry.states["ws-nocheck"] = self._state(
                "ws-nocheck",
                registry=registry,
                health_check=None,
                health_status=None,
            )
            sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)

        frames = [c[0][0] for c in sock.send_json.call_args_list]
        assert len(frames) == 2
        by_ws = {f["workspace_id"]: f for f in frames}
        assert by_ws["ws-healthy"]["healthy"] is True
        assert by_ws["ws-healthy"]["health_message"] is None
        assert by_ws["ws-sick"]["healthy"] is False
        assert by_ws["ws-sick"]["health_message"] == "conn refused"
        assert "ws-unchecked" not in by_ws
        assert "ws-nocheck" not in by_ws
        for f in frames:
            assert f["type"] == "service_health"

    def test_targets_only_the_given_socket(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        saved = dict(registry.states)
        sock = _mock_sock()
        other = _mock_sock()
        try:
            registry.states.clear()
            registry.states["ws-1"] = self._state(
                "ws-1",
                registry=registry,
                health_check="true",
                health_status="healthy",
            )
            sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
        sock.send_json.assert_called_once()
        other.send_json.assert_not_called()

    def test_dead_socket_breaks_cleanly(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        from klangk_backend.wshandler import WS_ERRORS

        saved = dict(registry.states)
        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("dead"))
        try:
            registry.states.clear()
            registry.states["ws-1"] = self._state(
                "ws-1",
                registry=registry,
                health_check="true",
                health_status="healthy",
            )
            registry.states["ws-2"] = self._state(
                "ws-2",
                registry=registry,
                health_check="true",
                health_status="unhealthy",
            )
            # Must not raise; the dead socket ends the snapshot early.
            sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)

    def test_empty_registry_sends_nothing(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        saved = dict(registry.states)
        sock = _mock_sock()
        try:
            registry.states.clear()
            sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
        sock.send_json.assert_not_called()


class TestServiceHealthFrame:
    """_service_health_frame: the additive contract fields (#1175)."""

    def test_defaults_preserve_legacy_shape(self):
        # Only the required healthy/message need to be supplied; the new
        # fields default so an old-style caller produces a superset of
        # the legacy frame (additive, non-breaking).
        from klangk_backend.wshandler.session import _service_health_frame

        out = _service_health_frame("ws-1", healthy=True, message=None)
        assert out["type"] == "service_health"
        assert out["workspace_id"] == "ws-1"
        assert out["healthy"] is True
        assert out["health_message"] is None
        assert out["running"] is True
        assert out["health_checked_at"] is None
        assert out["seq"] == 0

    def test_health_checked_at_serialized_as_iso(self):
        from klangk_backend.wshandler.session import (
            _service_health_frame,
            _iso_utc,
        )

        # A known epoch renders as a fixed ISO-8601 UTC string.
        ts = 1_700_000_000.0
        assert _iso_utc(ts) == "2023-11-14T22:13:20+00:00"
        assert _iso_utc(None) is None
        out = _service_health_frame(
            "ws-1", healthy=False, message="x", health_checked_at=ts
        )
        assert out["health_checked_at"] == "2023-11-14T22:13:20+00:00"

    def test_running_false_and_seq_forwarded(self):
        from klangk_backend.wshandler.session import _service_health_frame

        out = _service_health_frame(
            "ws-1",
            healthy=False,
            message=None,
            running=False,
            seq=7,
        )
        assert out["running"] is False
        assert out["seq"] == 7


class TestNotifyServiceHealthForwarding:
    """notify_service_health forwards running/checked_at/seq (#1175)."""

    def _register(self, sock, user, app_state):
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        app_state.sockets.connections[sock] = conn
        return conn

    def test_forwards_death_frame_fields(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        # A container-death call passes running=False + a seq; the frame
        # a subscriber receives carries them (#1175 items 2, 4).
        sock = _mock_sock()
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            sockets.notify_service_health(
                "ws-9",
                healthy=False,
                running=False,
                health_checked_at=1_700_000_000.0,
                seq=3,
            )
        finally:
            sockets.connections.pop(sock, None)
        msg = sock.send_json.call_args[0][0]
        assert msg["running"] is False
        assert msg["healthy"] is False
        assert msg["seq"] == 3
        assert msg["health_checked_at"] == "2023-11-14T22:13:20+00:00"


class TestServiceHealthSnapshotFields:
    """send_service_health_snapshot carries running/seq/checked_at."""

    def _state(self, ws_id, *, registry, checked_at=None, seq=0):
        cs = container.ContainerState(ws_id, f"cid-{ws_id}", registry)
        cs.health_check = "true"
        cs.health_status = "unhealthy"
        cs.health_message = "down"
        cs.health_checked_at = checked_at
        cs.health_seq = seq
        return cs

    def test_snapshot_frame_carries_live_fields(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        saved = dict(registry.states)
        sock = _mock_sock()
        try:
            registry.states.clear()
            registry.states["ws-1"] = self._state(
                "ws-1", registry=registry, checked_at=1_700_000_000.0, seq=5
            )
            sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
        frame = sock.send_json.call_args[0][0]
        # A snapshot is a live-container replay: running=True.
        assert frame["running"] is True
        assert frame["seq"] == 5
        assert frame["health_checked_at"] == "2023-11-14T22:13:20+00:00"


class TestHealthHeartbeat:
    """send_health_heartbeats: opt-in liveness frames (#1175 item 3b)."""

    def _register(self, sock, user, app_state, *, wants=False):
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.wants_health_heartbeat = wants
        app_state.sockets.connections[sock] = conn
        return conn

    def _frame(self, sock):
        return sock.send_json.call_args[0][0]

    def test_only_opted_in_connections_receive_it(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        opted = _mock_sock()
        quiet = _mock_sock()
        try:
            self._register(
                opted, {"id": "u1", "email": "a@x"}, app_state, wants=True
            )
            self._register(
                quiet, {"id": "u2", "email": "b@x"}, app_state, wants=False
            )
            sockets.send_health_heartbeats()
        finally:
            sockets.connections.pop(opted, None)
            sockets.connections.pop(quiet, None)
        opted.send_json.assert_called_once()
        frame = self._frame(opted)
        assert frame["type"] == "service_health_heartbeat"
        assert "timestamp" in frame
        # Default-off connections are left alone.
        quiet.send_json.assert_not_called()

    def test_skips_unauthenticated(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        try:
            self._register(
                sock, {"id": None, "email": ""}, app_state, wants=True
            )
            sockets.send_health_heartbeats()
        finally:
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    def test_dead_opted_in_socket_is_pruned(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import WS_ERRORS

        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("dead"))
        try:
            self._register(
                sock, {"id": "u1", "email": "a@x"}, app_state, wants=True
            )
            sockets.send_health_heartbeats()
            assert sock not in sockets.connections
        finally:
            sockets.connections.pop(sock, None)

    def test_subscribe_command_toggles_flag(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        # The subscribe_health_heartbeat command flips the per-connection
        # flag; enabled defaults to True when omitted.
        sock = _mock_sock()
        try:
            conn = self._register(
                sock, {"id": "u1", "email": "a@x"}, app_state
            )
            assert conn.wants_health_heartbeat is False
            sockets.handle_subscribe_health_heartbeat({}, sock)
            assert conn.wants_health_heartbeat is True
            sockets.handle_subscribe_health_heartbeat({"enabled": False}, sock)
            assert conn.wants_health_heartbeat is False
        finally:
            sockets.connections.pop(sock, None)

    def test_subscribe_unknown_socket_is_noop(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        # Not registered -- must not raise.
        sockets.handle_subscribe_health_heartbeat({}, sock)


class TestRemoveSessionLocked:
    async def test_removes_session(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-locked-rm", app_state)
        try:
            async with session.lock:
                await sockets.remove_session_locked(session)
            assert "ws-locked-rm" not in sockets.sessions
        finally:
            sockets.sessions.pop("ws-locked-rm", None)


class TestGetOrCreateSessionAtomicity:
    async def test_returns_same_session_for_same_workspace(self):
        """Concurrent calls return the same WorkspaceSession, not duplicates."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        sockets.sessions.pop("ws-atomic", None)
        try:
            s1 = sockets.get_or_create_session("ws-atomic", app_state)
            s2 = sockets.get_or_create_session("ws-atomic", app_state)
            assert s1 is s2
        finally:
            sockets.sessions.pop("ws-atomic", None)

    async def test_concurrent_get_or_create_via_gather(self):
        """Two coroutines that both call get_or_create_session end up
        with the identical session object (no orphaned duplicates)."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        sockets.sessions.pop("ws-gather", None)
        sessions = []

        async def grab():
            s = sockets.get_or_create_session("ws-gather", app_state)
            await asyncio.sleep(0)  # yield to let the other coroutine run
            sessions.append(s)

        try:
            await asyncio.gather(grab(), grab())
            assert len(sessions) == 2
            assert sessions[0] is sessions[1]
        finally:
            sockets.sessions.pop("ws-gather", None)


class TestCleanupSubscriberRace:
    async def test_new_subscriber_not_lost_during_cleanup(self):
        """A subscriber added under the lock while cleanup runs is not lost."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        session = WorkspaceSession("ws-race", app_state)
        session.subscribers.add(sock1)
        sockets.sessions["ws-race"] = session

        conn = _base_conn(ws=sock1, app_state=app_state)
        conn.workspace_id = "ws-race"
        conn.container_id = "cid-race"

        # Simulate: sock1 disconnects (cleanup) while sock2 connects
        # (start_workspace_container adds sock2 under the lock).
        # We do this by adding sock2 after sock1's cleanup, verifying the session
        # and sock2 survive.

        await conn.cleanup()

        # Session should be removed since sock1 was the last subscriber
        assert "ws-race" not in sockets.sessions

        # Now create a fresh session for sock2 (simulating start_workspace_container)
        session2 = sockets.get_or_create_session("ws-race", app_state)
        async with session2.lock:
            session2.subscribers.add(sock2)

        assert sock2 in session2.subscribers
        assert "ws-race" in sockets.sessions

        sockets.sessions.pop("ws-race", None)

    async def test_concurrent_cleanup_and_add(self):
        """When cleanup holds the lock, a concurrent add waits and is not lost."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        session = WorkspaceSession("ws-conc", app_state)
        session.subscribers.add(sock1)
        session.subscribers.add(sock2)
        sockets.sessions["ws-conc"] = session

        conn1 = _base_conn(ws=sock1, app_state=app_state)
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
        assert "ws-conc" in sockets.sessions
        assert sock2 in session.subscribers
        assert sock1 not in session.subscribers

        sockets.sessions.pop("ws-conc", None)


class TestWsDebugLogging:
    async def test_recv_logged_when_debug(self, user, monkeypatch):
        app_state = _make_app_state()

        monkeypatch.setattr(wshandler, "WS_DEBUG", True)
        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "heartbeat"}),
                WebSocketDisconnect(),
            ]
        )
        await handle_websocket(websocket, app_state)
        websocket.accept.assert_awaited_once()

    def test_send_error_logged_when_debug(self, monkeypatch):
        monkeypatch.setattr(wshandler, "WS_DEBUG", True)
        sock = _mock_sock()
        send_error(sock, "test error")
        sock.send_json.assert_called_once()

    async def test_broadcast_sends_to_subscribers(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-bcast", app_state)
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        try:
            delivered = session.broadcast({"type": "test"})
            assert delivered == 1
        finally:
            sockets.sessions.pop("ws-bcast", None)

    async def test_broadcast_to_browsers_sends_to_browser_subscribers(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-browser-bcast", app_state)
        mock_sock = _mock_sock()
        session.browser_subscribers.add(mock_sock)
        try:
            delivered = session.broadcast_to_browsers({"type": "test"})
            assert delivered == 1
        finally:
            sockets.sessions.pop("ws-browser-bcast", None)


class TestLogWsMsg:
    def test_terminal_output_truncated(self):
        with patch.object(_ws_constants, "WS_DEBUG", True):
            log_ws_msg(
                "RECV",
                {"type": "terminal_output", "data": "x" * 200},
                {"email": "test@example.com"},
            )

    def test_terminal_input_truncated(self):
        with patch.object(_ws_constants, "WS_DEBUG", True):
            log_ws_msg(
                "SEND",
                {"type": "terminal_input", "data": "y" * 50},
            )

    def test_other_message(self):
        with patch.object(_ws_constants, "WS_DEBUG", True):
            log_ws_msg("RECV", {"type": "heartbeat"})

    def test_other_message_with_user(self):
        with patch.object(_ws_constants, "WS_DEBUG", True):
            log_ws_msg(
                "RECV",
                {"cmd": "workspace_connect", "workspaceId": "ws-1"},
                {"email": "test@example.com"},
            )

    def test_noop_when_debug_disabled(self):
        with patch.object(_ws_constants, "WS_DEBUG", False):
            log_ws_msg("RECV", {"type": "heartbeat"})


class TestBroadcastDeadSubscribers:
    async def test_dead_subscriber_removed(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-dead-sub", app_state)
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
            sockets.sessions.pop("ws-dead-sub", None)


class TestHandleRestartContainer:
    async def test_restart_not_connected(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_restart_container()
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_restart_no_admin_perm(self):
        """A spectator (no admin perm) must not restart the container,
        and must not trigger cleanup or container (re)start side effects."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.workspace_id = "ws-noadmin"
        with (
            patch.object(conn, "_has_perm", new=AsyncMock(return_value=False)),
            patch.object(
                Connection, "cleanup", new_callable=AsyncMock
            ) as mock_cleanup,
            patch.object(
                Connection,
                "start_workspace_container",
                new_callable=AsyncMock,
            ) as mock_start,
        ):
            await conn.handle_restart_container()
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and "Permission denied" in m.get("message", "")
            for m in sent
        )
        # No destructive side effects: nothing torn down or (re)started.
        mock_cleanup.assert_not_called()
        mock_start.assert_not_called()

    async def test_restart_deny_leaves_other_connections_untouched(
        self, user, app_state
    ):
        """A spectator's denied restart must not change other users'
        container_id or otherwise disrupt their session (issue #873)."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock1 = _mock_sock(headers={"host": "localhost:8997"})
        sock2 = _mock_sock()
        ws = await _create_workspace_with_acl(
            app_state, user["id"], "restart-deny"
        )
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)
        conn2 = _base_conn(user=user, ws=sock2, app_state=app_state)
        conn1.workspace_id = ws["id"]
        conn1.container_id = "cid"
        conn2.workspace_id = ws["id"]
        conn2.container_id = "cid"
        sockets.connections[sock1] = conn1
        sockets.connections[sock2] = conn2
        try:
            # conn1 is a spectator: admin denied.
            with (
                patch.object(
                    conn1, "_has_perm", new=AsyncMock(return_value=False)
                ),
                patch.object(
                    Connection,
                    "start_workspace_container",
                    new_callable=AsyncMock,
                ) as mock_start,
            ):
                await conn1.handle_restart_container()
            # Neither connection's container was touched; nothing started.
            assert conn1.container_id == "cid"
            assert conn2.container_id == "cid"
            mock_start.assert_not_called()
        finally:
            sockets.connections.pop(sock1, None)
            sockets.connections.pop(sock2, None)
            sockets.sessions.pop(ws["id"], None)

    async def test_restart_success(self, user, app_state):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "restart-ws"
        )
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
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
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
            patch.object(registry, "record_activity"),
            patch.object(
                registry,
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
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-gone"
        conn.container_id = "cid-gone"
        conn.workspace = None
        # "ws-gone" is not a real workspace; grant admin so the perm
        # gate passes and we reach the "not found" path under test.
        conn._has_perm = AsyncMock(return_value=True)

        with (
            patch.object(
                app_state.workspaces,
                "get_workspace",
                return_value=None,
            ),
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
        ):
            await conn.handle_restart_container()

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("not found" in str(c) for c in calls)

    async def test_restart_fractional_timeout(
        self, user, monkeypatch, app_state
    ):
        app_state = _make_app_state()
        registry = app_state.container_registry
        monkeypatch.setattr(
            app_state.container_registry, "idle_timeout_seconds", 90
        )
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "restart-frac"
        )
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
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
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
            patch.object(registry, "record_activity"),
            patch.object(
                registry,
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

    async def test_restart_cleanup_error(self, user, app_state):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "restart-err"
        )
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
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
            patch.object(registry, "record_activity"),
            patch.object(
                registry,
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

    async def test_restart_cleanup_ws_disconnect(self, user, app_state):
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "restart-disc"
        )
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
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
            patch.object(registry, "record_activity"),
            patch.object(
                registry,
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

    async def test_restart_updates_other_connections_container_id(
        self, user, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock1 = _mock_sock(headers={"host": "localhost:8997"})
        sock2 = _mock_sock()
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "restart-cid"
        )
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)
        conn2 = _base_conn(user=user, ws=sock2, app_state=app_state)
        conn1.workspace_id = workspace["id"]
        conn1.container_id = "old-cid"
        conn1.workspace = workspace
        conn2.workspace_id = workspace["id"]
        conn2.container_id = "old-cid"

        sockets.connections[sock1] = conn1
        sockets.connections[sock2] = conn2

        async def fake_start(self_arg, wid, ws_obj):
            self_arg.container_id = "new-cid"
            self_arg.workspace_id = wid
            sockets.get_or_create_session(wid, app_state)

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                autospec=True,
                side_effect=fake_start,
            ),
            patch.object(registry, "record_activity"),
            patch.object(
                registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            await conn1.handle_restart_container()

        assert conn2.container_id == "new-cid"

        sockets.connections.pop(sock1, None)
        sockets.connections.pop(sock2, None)
        sockets.sessions.pop(workspace["id"], None)


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

    async def test_shutdown_no_admin_perm(self):
        """A spectator (no admin perm) must not shut down the container,
        must not stop it, and must not broadcast container_stopped."""
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.workspace_id = "ws-noadmin"
        conn.container_id = "cid"
        with (
            patch.object(conn, "_has_perm", new=AsyncMock(return_value=False)),
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ) as mock_stop,
        ):
            await conn.handle_shutdown_container()
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and "Permission denied" in m.get("message", "")
            for m in sent
        )
        # No destructive side effects.
        mock_stop.assert_not_called()
        assert not any(
            isinstance(m, dict)
            and m.get("type") == "event"
            and m.get("event", {}).get("name") == "container_stopped"
            for m in sent
        )

    async def test_shutdown_deny_does_not_clear_other_connections(
        self, user, app_state
    ):
        """A spectator's denied shutdown must not clear other users'
        container_id (which would disrupt their session) — issue #873."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        ws = await _create_workspace_with_acl(
            app_state, user["id"], "shutdown-deny"
        )
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)
        conn2 = _base_conn(user=user, ws=sock2, app_state=app_state)
        conn1.workspace_id = ws["id"]
        conn1.container_id = "cid"
        conn2.workspace_id = ws["id"]
        conn2.container_id = "cid"
        sockets.connections[sock1] = conn1
        sockets.connections[sock2] = conn2
        try:
            with (
                patch.object(
                    conn1, "_has_perm", new=AsyncMock(return_value=False)
                ),
                patch.object(
                    registry,
                    "stop_and_remove_container",
                    new_callable=AsyncMock,
                ) as mock_stop,
            ):
                await conn1.handle_shutdown_container()
            # Other connection's container_id must still be set.
            assert conn2.container_id == "cid"
            mock_stop.assert_not_called()
        finally:
            sockets.connections.pop(sock1, None)
            sockets.connections.pop(sock2, None)
            sockets.sessions.pop(ws["id"], None)

    async def test_shutdown_no_container(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.workspace_id = "ws"
        conn.container_id = None
        conn._has_perm = AsyncMock(return_value=True)
        await conn.handle_shutdown_container()
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and "No container" in m.get("message", "")
            for m in sent
        )

    async def test_shutdown_broadcasts_stopped(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        conn = _base_conn(user=user, ws=sock1, app_state=app_state)
        ws = await _create_workspace_with_acl(
            app_state, user["id"], "shutdown-ws"
        )
        conn.workspace_id = ws["id"]
        conn.container_id = "cid"

        session = sockets.get_or_create_session(ws["id"], app_state)
        await session.add_subscriber(sock1, "cid")
        await session.add_subscriber(sock2, "cid")

        with patch.object(
            registry,
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

        sockets.sessions.pop(ws["id"], None)

    async def test_shutdown_stops_agent_session(self, user, app_state):
        """handle_shutdown_container stops the agent RPC subprocess."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        ws = await _create_workspace_with_acl(
            app_state, user["id"], "shutdown-agent"
        )
        conn.workspace_id = ws["id"]
        conn.container_id = "cid"

        session = sockets.get_or_create_session(ws["id"], app_state)
        await session.add_subscriber(sock, "cid")

        # The on_workspace_killed callback also routes to reset_workspace,
        # so disable it here to measure only the explicit teardown call.
        old_cb = registry.on_workspace_killed
        registry.on_workspace_killed = None
        try:
            with (
                patch.object(
                    registry,
                    "stop_and_remove_container",
                    new_callable=AsyncMock,
                ),
                patch.object(
                    app_state.agents,
                    "stop_session",
                    new_callable=AsyncMock,
                ) as mock_stop,
            ):
                await conn.handle_shutdown_container()
            mock_stop.assert_awaited_once_with(ws["id"])
        finally:
            registry.on_workspace_killed = old_cb
            sockets.sessions.pop(ws["id"], None)

    async def test_shutdown_clears_other_connections_container_id(
        self, user, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)
        conn2 = _base_conn(user=user, ws=sock2, app_state=app_state)
        ws = await _create_workspace_with_acl(
            app_state, user["id"], "shutdown-cid"
        )
        conn1.workspace_id = ws["id"]
        conn1.container_id = "old-cid"
        conn2.workspace_id = ws["id"]
        conn2.container_id = "old-cid"

        session = sockets.get_or_create_session(ws["id"], app_state)
        await session.add_subscriber(sock1, "old-cid")
        await session.add_subscriber(sock2, "old-cid")
        sockets.connections[sock1] = conn1
        sockets.connections[sock2] = conn2

        with patch.object(
            registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            await conn1.handle_shutdown_container()

        assert conn2.container_id is None

        sockets.connections.pop(sock1, None)
        sockets.connections.pop(sock2, None)
        sockets.sessions.pop(ws["id"], None)

    async def test_shutdown_handles_stop_error(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        ws = await _create_workspace_with_acl(
            app_state, user["id"], "shutdown-err"
        )
        conn.workspace_id = ws["id"]
        conn.container_id = "cid"

        session = sockets.get_or_create_session(ws["id"], app_state)
        await session.add_subscriber(sock, "cid")

        with patch.object(
            registry,
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
        sockets.sessions.pop(ws["id"], None)

    async def test_shutdown_dispatch(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "shutdown_container"}),
                WebSocketDisconnect(),
            ]
        )
        await handle_websocket(websocket, app_state)
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
        with patch.object(
            _mock_term,
            "new_window",
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
        with patch.object(
            _mock_term,
            "new_window",
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
        with patch.object(
            _mock_term,
            "new_window",
            side_effect=ValueError("already exists"),
        ):
            await conn.handle_terminal_new_window({"name": "dup"})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"

    async def test_select_window_by_index(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch.object(
            _mock_term,
            "select_window",
        ) as mock_sel:
            await conn.handle_terminal_select_window({"index": 2})
        mock_sel.assert_called_once_with("cid", "uid", 2)

    async def test_select_window_by_id(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch.object(
            _mock_term,
            "select_window",
        ) as mock_sel:
            await conn.handle_terminal_select_window({"window_id": "@3"})
        mock_sel.assert_called_once_with("cid", "uid", "@3")

    async def test_select_window_error(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch.object(
            _mock_term,
            "select_window",
            side_effect=TerminalError("no such window"),
        ):
            await conn.handle_terminal_select_window({"index": 99})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"

    async def test_close_window(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch.object(
            _mock_term,
            "close_window",
            return_value=[
                {"id": "@0", "index": 0, "name": "bash", "active": True}
            ],
        ):
            await conn.handle_terminal_close_window({"index": 1})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "terminal_windows"

    async def test_close_shared_window_broadcasts(self, user):
        """Closing a shared window broadcasts updated shared_terminals."""
        async with _conn_in_workspace(
            user, "ws-1", user_home="/home/admin"
        ) as (sock, conn, session, app_state):
            session.terminal_windows[user["id"]] = [
                {"name": "bash", "index": 0, "id": "@0", "shared": True},
                {"name": "1", "index": 1, "id": "@1", "shared": False},
            ]
            with patch.object(
                _mock_term,
                "close_window",
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

    async def test_close_window_error(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        with patch.object(
            _mock_term,
            "close_window",
            side_effect=TerminalError("no such window"),
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
            patch.object(
                _mock_term,
                "rename_window",
            ),
            patch.object(
                _mock_term,
                "list_windows",
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
        with patch.object(
            _mock_term,
            "rename_window",
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
        with patch.object(
            _mock_term,
            "list_windows",
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
        with patch.object(
            _mock_term,
            "list_windows",
            side_effect=TerminalError("tmux not running"),
        ):
            await conn.handle_terminal_list_windows()
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"


class TestTerminalController:
    """Unit tests for the TerminalController collaborator in isolation.

    These exercise the controller directly against a lightweight fake
    connection (a SimpleNamespace), proving it is decoupled from
    Connection (issue #961) and covering the branches the existing
    Connection-level tests reach only indirectly — notably the
    ``Connection._notify_user_terminal_windows`` backward-compat
    delegate, the no-session early returns, ``activate_session``
    supersession, and ``forward_output`` cleanup paths.
    """

    def _controller(
        self,
        *,
        container_id="cid",
        workspace_id="ws-1",
        user_home="/home/alice",
        sock=None,
        has_perm=True,
        user=None,
        app_state=None,
    ):
        if sock is None:
            sock = _mock_sock()
        if user is None:
            user = {
                "id": "uid",
                "email": "alice@example.com",
                "handle": "alice",
            }
        if app_state is None:
            app_state = _make_app_state()
        conn = SimpleNamespace(
            sock=sock,
            container_id=container_id,
            workspace_id=workspace_id,
            _user_home=user_home,
            _ssh_agent_socket=None,
            browser_id=None,
            viewing_shared=None,
            _service_command=None,
            user=user,
            workspace=None,
            _has_perm=AsyncMock(return_value=has_perm),
            broadcast_shared_terminals=MagicMock(),
            app_state=app_state,
        )
        return TerminalController(conn), sock, conn

    # --- start: guard clauses ---

    async def test_start_no_container_skips(self):
        ctrl, sock, _ = self._controller(container_id=None)
        await ctrl.start({"cols": 80, "rows": 24})
        assert ctrl.session is None
        sock.send_json.assert_not_called()

    async def test_start_no_user_home_sends_error(self):
        ctrl, sock, _ = self._controller(user_home=None)
        await ctrl.start({"cols": 80, "rows": 24})
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "error"
        assert "Handle" in msg["message"]

    async def test_start_no_perm_sends_terminal_started(self):
        """Spectators get terminal_started (no session) for shared tabs."""
        ctrl, sock, _ = self._controller(has_perm=False)
        await ctrl.start({"cols": 80, "rows": 24})
        msg = sock.send_json.call_args[0][0]
        assert msg == {"type": "terminal_started"}
        assert ctrl.session is None

    # --- _setup_state_for_workspace: defensive fallbacks (#1033) ---

    async def test_setup_state_db_error_defaults_to_complete(self):
        """If the setup_state lookup raises, default to 'complete'."""
        ctrl, _, conn = self._controller()
        with patch.object(
            conn.app_state.model.workspaces,
            "get_workspace",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            result = await ctrl._setup_state_for_workspace()
        assert result == "complete"

    async def test_setup_state_workspace_missing_defaults_to_complete(self):
        """If get_workspace returns None, default to 'complete'."""
        ctrl, _, conn = self._controller()
        with patch.object(
            conn.app_state.model.workspaces,
            "get_workspace",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await ctrl._setup_state_for_workspace()
        assert result == "complete"

    async def test_setup_state_returns_workspace_value(self):
        """Returns the workspace's actual setup_state when present (#1033)."""
        ctrl, _, conn = self._controller()
        with patch.object(
            conn.app_state.model.workspaces,
            "get_workspace",
            new_callable=AsyncMock,
            return_value={"setup_state": "pending"},
        ):
            result = await ctrl._setup_state_for_workspace()
        assert result == "pending"

    async def test_start_rapid_debounce_skips(self):
        ctrl, sock, conn = self._controller()
        conn._last_terminal_start = time.monotonic()
        await ctrl.start({"cols": 80, "rows": 24})
        assert ctrl.session is None
        sock.send_json.assert_not_called()

    async def test_start_stops_existing_terminal_first(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, conn = self._controller(app_state=app_state)

        def _swallow(coro, **kw):
            # Close the coroutine so it doesn't warn about being
            # never awaited.
            coro.close()
            return MagicMock()

        with (
            patch.object(ctrl, "stop", new=AsyncMock()) as stop,
            patch(
                "klangk_backend.wshandler.controllers.TerminalSession"
            ) as MockTS,
            patch(
                "klangk_backend.wshandler.controllers.asyncio.create_task",
                _swallow,
            ),
            patch.object(registry, "record_activity"),
        ):
            MockTS.return_value.start = AsyncMock()
            await ctrl.start({"cols": 90, "rows": 30})
        stop.assert_awaited_once()
        assert ctrl.cols == 90
        assert ctrl.rows == 30
        assert ctrl.session is MockTS.return_value

    # --- input ---

    async def test_input_no_session(self):
        ctrl, _, _ = self._controller()
        await ctrl.input({"data": "x"})

    async def test_input_dead_session_dropped(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.is_alive = False
        ctrl.session = session
        await ctrl.input({"data": "x"})
        session.write.assert_not_awaited()

    async def test_input_read_only_blocks_user_text(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.is_alive = True
        session.read_only = True
        ctrl.session = session
        await ctrl.input({"data": "ls"})
        session.write.assert_not_awaited()

    async def test_input_read_only_allows_escape_sequences(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        session = AsyncMock()
        session.is_alive = True
        session.read_only = True
        ctrl.session = session
        with patch.object(registry, "record_activity"):
            await ctrl.input({"data": "\x1b[6n"})
        session.write.assert_awaited_once_with("\x1b[6n")

    async def test_input_oversized_dropped(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.is_alive = True
        session.read_only = False
        ctrl.session = session
        await ctrl.input({"data": "x" * (_ws_constants.MAX_INPUT_SIZE + 1)})
        session.write.assert_not_awaited()

    async def test_input_writes_and_records_activity(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        session = AsyncMock()
        session.is_alive = True
        session.read_only = False
        ctrl.session = session
        with patch.object(registry, "record_activity") as rec:
            await ctrl.input({"data": "ls"})
        session.write.assert_awaited_once_with("ls")
        rec.assert_called_once_with("cid")

    # --- resize ---

    async def test_resize_updates_dims_and_session(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        ctrl.session = session
        await ctrl.resize({"cols": 120, "rows": 40})
        assert ctrl.cols == 120
        assert ctrl.rows == 40
        session.resize.assert_awaited_once_with(120, 40)

    async def test_resize_no_session_still_updates_dims(self):
        ctrl, _, _ = self._controller()
        await ctrl.resize({"cols": 100, "rows": 35})
        assert ctrl.cols == 100
        assert ctrl.rows == 35

    async def test_resize_defaults_when_missing(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        ctrl.session = session
        await ctrl.resize({})
        assert ctrl.cols == 80
        assert ctrl.rows == 24
        session.resize.assert_awaited_once_with(80, 24)

    # --- stop / claim_and_stop / activate_session ---

    async def test_stop_command_calls_stop(self):
        ctrl, _, _ = self._controller()
        with patch.object(ctrl, "stop", new=AsyncMock()) as stop:
            await ctrl.stop_command()
        stop.assert_awaited_once()

    async def test_claim_and_stop_no_session(self):
        ctrl, _, _ = self._controller()
        await ctrl.claim_and_stop()
        assert ctrl.session is None

    async def test_claim_and_stop_drops_and_stops(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        ctrl.session = session
        await ctrl.claim_and_stop()
        assert ctrl.session is None
        session.stop.assert_awaited_once()

    async def test_stop_cancels_task_and_clears_viewing(self):
        """stop() clears the connection's viewing_shared and resets debounce."""
        ctrl, _, conn = self._controller()
        session = AsyncMock()
        ctrl.session = session
        ctrl.task = asyncio.create_task(asyncio.sleep(999))
        conn.viewing_shared = {"user_id": "x", "window_id": "@0"}
        conn._last_terminal_start = 12345.0
        await ctrl.stop()
        assert ctrl.task is None
        assert ctrl.session is None
        assert conn.viewing_shared is None
        assert conn._last_terminal_start == 0

    async def test_stop_broadcasts_when_was_viewing(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, _, conn = self._controller(app_state=app_state)
        session = AsyncMock()
        ctrl.session = session
        conn.viewing_shared = {"user_id": "x", "window_id": "@0"}
        with patch.object(sockets, "get_session") as gs:
            ws_session = MagicMock()
            gs.return_value = ws_session
            await ctrl.stop()
        conn.broadcast_shared_terminals.assert_called_once_with(ws_session)

    async def test_activate_session_superseded_returns_false(self):
        """If terminal_session changed, activate_session stops the stale one."""
        ctrl, _, _ = self._controller()
        stale = AsyncMock()
        # Controller's current session is a different object.
        ctrl.session = AsyncMock()
        result = await ctrl.activate_session(stale, 80, 24)
        assert result is False
        stale.stop.assert_awaited_once()

    async def test_activate_session_wires_forward_task(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        session = _mock_terminal()
        ctrl.session = session
        with patch.object(registry, "record_activity") as rec:
            result = await ctrl.activate_session(session, 80, 24)
        assert result is True
        assert ctrl.output_task is not None
        session.resize.assert_awaited_once_with(80, 24)
        rec.assert_called_once_with("cid")
        ctrl.output_task.cancel()
        try:
            await ctrl.output_task
        except asyncio.CancelledError:
            pass

    # --- forward_output ---

    async def test_forward_output_relays_and_cleans_up(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, sock, _ = self._controller(app_state=app_state)
        session = AsyncMock()
        ctrl.session = session

        async def fake_output():
            yield "chunk1"
            yield "chunk2"

        session.output = fake_output
        with patch.object(registry, "record_activity"):
            await ctrl.forward_output(session)
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert calls == [
            {"type": "terminal_output", "data": "chunk1"},
            {"type": "terminal_output", "data": "chunk2"},
        ]
        session.stop.assert_awaited_once()
        assert ctrl.session is None

    async def test_forward_output_records_activity_when_container_set(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, _ = self._controller(container_id="cid", app_state=app_state)
        session = AsyncMock()
        ctrl.session = session

        async def fake_output():
            yield "data"

        session.output = fake_output
        with patch.object(registry, "record_activity") as rec:
            await ctrl.forward_output(session)
        rec.assert_called_once_with("cid")

    async def test_forward_output_swallows_ws_error(self):
        from klangk_backend.wshandler import WS_ERRORS

        ctrl, sock, _ = self._controller()
        session = AsyncMock()
        ctrl.session = session

        async def fake_output():
            yield "data"

        session.output = fake_output
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("ws dead"))
        with patch("klangk_backend.wshandler.controllers.send_event"):
            await ctrl.forward_output(session)
        session.stop.assert_awaited_once()

    async def test_forward_output_reraises_cancelled(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        ctrl.session = session
        never = asyncio.Event()

        async def blocking_output():
            yield "first"
            await never.wait()

        session.output = blocking_output
        task = asyncio.create_task(ctrl.forward_output(session))
        for _ in range(3):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        session.stop.assert_awaited_once()
        assert ctrl.session is None

    # --- window helpers ---

    async def test_new_window_no_container(self):
        ctrl, sock, _ = self._controller(container_id=None)
        await ctrl.new_window({})
        sock.send_json.assert_not_called()

    async def test_new_window_error_sends_error(self):
        ctrl, sock, _ = self._controller()
        with patch.object(
            _mock_term,
            "new_window",
            side_effect=ValueError("boom"),
        ):
            await ctrl.new_window({"name": "x"})
        assert sock.send_json.call_args[0][0]["type"] == "error"

    async def test_close_window_no_container(self):
        ctrl, sock, _ = self._controller(container_id=None)
        await ctrl.close_window({})
        sock.send_json.assert_not_called()

    async def test_rename_window_no_name_sends_error(self):
        ctrl, sock, _ = self._controller()
        await ctrl.rename_window({"index": 0, "name": ""})
        assert sock.send_json.call_args[0][0]["type"] == "error"

    async def test_list_windows_no_container(self):
        ctrl, sock, _ = self._controller(container_id=None)
        await ctrl.list_windows()
        sock.send_json.assert_not_called()

    async def test_list_windows_error_sends_error(self):
        ctrl, sock, _ = self._controller()
        with patch.object(
            _mock_term,
            "list_windows",
            side_effect=OSError("boom"),
        ):
            await ctrl.list_windows()
        assert sock.send_json.call_args[0][0]["type"] == "error"

    async def test_select_window_no_container(self):
        ctrl, sock, _ = self._controller(container_id=None)
        await ctrl.select_window({"index": 0})
        sock.send_json.assert_not_called()

    async def test_select_window_uses_grouped_session_name(self):
        ctrl, _, _ = self._controller()
        session = MagicMock()
        session.tmux_session_name = "grouped"
        ctrl.session = session
        with patch.object(_mock_term, "select_window") as sel:
            await ctrl.select_window({"window_id": "@2"})
        sel.assert_called_once_with("cid", "grouped", "@2")

    async def test_select_window_falls_back_to_tmux_session_name(self):
        ctrl, _, _ = self._controller()
        session = MagicMock()
        session.tmux_session_name = None
        ctrl.session = session
        with patch.object(_mock_term, "select_window") as sel:
            await ctrl.select_window({"index": 1})
        sel.assert_called_once_with("cid", "uid", 1)

    # --- sync / notify helpers ---

    async def test_sync_terminal_windows_no_ws_session_noop(self):
        ctrl, _, _ = self._controller(workspace_id="nope")
        # No WorkspaceSession for this workspace.
        ctrl.sync_terminal_windows([{"id": "@0", "index": 0, "name": "bash"}])

    async def test_notify_user_terminal_windows_no_ws_session_sends_directly(
        self,
    ):
        ctrl, sock, _ = self._controller(workspace_id="nope")
        ctrl.notify_user_terminal_windows([{"id": "@0", "name": "bash"}])
        sent = sock.send_json.call_args[0][0]
        assert sent == {
            "type": "terminal_windows",
            "windows": [{"id": "@0", "name": "bash"}],
        }

    async def test_notify_user_terminal_windows_broadcasts_to_user_conns(
        self, user
    ):
        """When a ws_session exists, only this user's sockets receive it."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, conn = self._controller(user=user, app_state=app_state)
        ws_session = sockets.get_or_create_session("ws-1", app_state)
        other_sock = _mock_sock()
        other_conn = _base_conn(
            user={"id": "other", "email": "o@x.com", "handle": "o"},
            ws=other_sock,
        )
        other_conn.workspace_id = "ws-1"
        await ws_session.add_subscriber(sock, "cid")
        await ws_session.add_subscriber(other_sock, "cid")
        sockets.connections[sock] = conn
        sockets.connections[other_sock] = other_conn
        try:
            ctrl.notify_user_terminal_windows([{"id": "@0", "name": "bash"}])
            # user's sock received it; other_sock did not.
            sent = sock.send_json.call_args[0][0]
            assert sent["type"] == "terminal_windows"
            other_sock.send_json.assert_not_called()
        finally:
            await ws_session.remove_subscriber(sock)
            await ws_session.remove_subscriber(other_sock)
            sockets.connections.pop(sock, None)
            sockets.connections.pop(other_sock, None)
            sockets.sessions.pop("ws-1", None)

    # --- #1114: service-cmd shared singleton ---

    async def test_sync_terminal_windows_marks_service_cmd_shared(self):
        """The service-cmd window is shared by definition, so syncing the
        owner's own windows marks it shared even with no prior entry (#1114)."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, _, _ = self._controller(app_state=app_state)
        ws_session = sockets.get_or_create_session("ws-1", app_state)
        try:
            ctrl.sync_terminal_windows(
                [
                    {"id": "@0", "index": 0, "name": "bash"},
                    {
                        "id": "@1",
                        "index": 1,
                        "name": "service-cmd",
                    },
                ]
            )
            wins = ws_session.terminal_windows["uid"]
            dc = next(w for w in wins if w["name"] == "service-cmd")
            assert dc["shared"] is True
            # A plain window is not implicitly shared.
            bash = next(w for w in wins if w["name"] == "bash")
            assert bash["shared"] is False
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_sync_service_windows_injects_service_cmd_shared(self):
        """Discovery: connecting syncs the agent's service:service-cmd window
        into the session map (keyed by AGENT_USER_ID, marked shared, handle
        cached) even though the agent has no WS connection (#1133)."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend import model

        ctrl, _, conn = self._controller(app_state=app_state)
        ws_session = sockets.get_or_create_session("ws-1", app_state)
        try:
            with (
                patch.object(
                    _mock_term,
                    "list_windows",
                    new=AsyncMock(
                        return_value=[
                            {
                                "id": "@5",
                                "index": 1,
                                "name": "service-cmd",
                                "active": True,
                            }
                        ]
                    ),
                ),
                patch.object(
                    app_state.model.users,
                    "agent_handle",
                    new=AsyncMock(return_value="clanker"),
                ),
            ):
                synced = await ctrl._sync_service_windows(ws_session)
            assert synced is True
            agent_wins = ws_session.terminal_windows[model.AGENT_USER_ID]
            assert agent_wins[0]["name"] == "service-cmd"
            assert agent_wins[0]["shared"] is True
            # Handle cached so the window stays attributable.
            assert ws_session.agent_handle == "clanker"
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_sync_service_windows_no_container_returns_false(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, _, _ = self._controller(container_id=None, app_state=app_state)
        ws_session = sockets.get_or_create_session("ws-1", app_state)
        try:
            assert await ctrl._sync_service_windows(ws_session) is False
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_sync_service_windows_list_error_returns_false(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, _, _ = self._controller(app_state=app_state)
        ws_session = sockets.get_or_create_session("ws-1", app_state)
        try:
            with patch.object(
                _mock_term,
                "list_windows",
                new=AsyncMock(side_effect=TerminalError),
            ):
                assert await ctrl._sync_service_windows(ws_session) is False
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_sync_service_windows_empty_returns_false(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, _, _ = self._controller(app_state=app_state)
        ws_session = sockets.get_or_create_session("ws-1", app_state)
        try:
            with patch.object(
                _mock_term,
                "list_windows",
                new=AsyncMock(return_value=[]),
            ):
                assert await ctrl._sync_service_windows(ws_session) is False
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_sync_service_windows_agent_handle_error_returns_false(self):
        """If the agent handle can't be resolved, discovery is skipped
        (best-effort) rather than breaking the caller (#1133)."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, _, _ = self._controller(app_state=app_state)
        ws_session = sockets.get_or_create_session("ws-1", app_state)
        try:
            with (
                patch.object(
                    _mock_term,
                    "list_windows",
                    new=AsyncMock(
                        return_value=[
                            {"id": "@1", "index": 1, "name": "service-cmd"}
                        ]
                    ),
                ),
                patch.object(
                    app_state.model.users,
                    "agent_handle",
                    new=AsyncMock(side_effect=RuntimeError("db down")),
                ),
            ):
                assert await ctrl._sync_service_windows(ws_session) is False
            assert ws_session.agent_handle is None
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_get_shared_terminals_visible_when_agent_offline(self):
        """The service window stays in the shared list (attributed to the
        agent) via the cached agent_handle, though the agent has no WS
        connection (#1133)."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend import model
        from klangk_backend.wshandler.helpers import get_shared_terminals

        ws_session = sockets.get_or_create_session("ws-offline", app_state)
        try:
            ws_session.terminal_windows[model.AGENT_USER_ID] = [
                {"id": "@1", "index": 1, "name": "service-cmd", "shared": True}
            ]
            ws_session.agent_handle = "clanker"
            # No active connection for the agent.
            terminals = get_shared_terminals(ws_session, sockets)
            assert len(terminals) == 1
            assert terminals[0]["handle"] == "clanker"
            assert terminals[0]["window_name"] == "service-cmd"
            # Agent-owned windows are flagged so the UI can present the
            # service tab distinctly (#1159).
            assert terminals[0]["is_service"] is True
        finally:
            sockets.sessions.pop("ws-offline", None)

    async def test_fire_service_command_invokes_ensure_service_session(self):
        """_fire_service_command reads fresh setup_state from the DB,
        resolves the agent home, and targets the service session (#1133)."""
        ctrl, _, conn = self._controller()
        conn._service_command = "./run.sh"
        with (
            patch.object(
                conn.app_state.model.workspaces,
                "get_workspace",
                new=AsyncMock(return_value={"setup_state": "complete"}),
            ),
            patch.object(
                conn.app_state.model.users,
                "agent_handle",
                new=AsyncMock(return_value="clanker"),
            ),
            patch.object(
                _mock_term,
                "ensure_service_session",
                new=AsyncMock(),
            ) as mock_ess,
        ):
            await ctrl._fire_service_command()
        mock_ess.assert_awaited_once_with(
            "cid",
            "/home/clanker",
            "./run.sh",
            setup_state="complete",
        )

    async def test_fire_service_command_no_service_command_noop(self):
        ctrl, _, conn = self._controller()
        conn._service_command = None
        with patch.object(
            _mock_term,
            "ensure_service_session",
            new=AsyncMock(),
        ) as mock_ess:
            await ctrl._fire_service_command()
        mock_ess.assert_not_awaited()

    async def test_fire_service_command_no_container_noop(self):
        ctrl, _, conn = self._controller(container_id=None)
        conn._service_command = "./run.sh"
        with patch.object(
            _mock_term,
            "ensure_service_session",
            new=AsyncMock(),
        ) as mock_ess:
            await ctrl._fire_service_command()
        mock_ess.assert_not_awaited()

    # --- browser_reattach ---

    async def test_browser_reattach_no_browser_id(self):
        ctrl, _, _ = self._controller()
        await ctrl.browser_reattach({})
        # No registration, no browser_id set.

    async def test_browser_reattach_no_container(self):
        ctrl, _, conn = self._controller(container_id=None)
        await ctrl.browser_reattach({"browser_id": "bid"})
        assert conn.browser_id is None

    async def test_browser_reattach_registers_and_attaches(self):
        app_state = _make_app_state()
        registry = app_state.container_registry
        ctrl, _, conn = self._controller(app_state=app_state)
        with (
            patch.object(registry, "revoke_browser") as rev,
            patch.object(registry, "register_browser") as reg,
            patch.object(
                _mock_term,
                "attach_browser",
                new=AsyncMock(),
            ) as attach,
        ):
            await ctrl.browser_reattach({"browser_id": "bid"})
        rev.assert_called_once_with(conn.sock)
        reg.assert_called_once_with("bid", "ws-1", conn.sock)
        attach.assert_awaited_once_with("cid", "bid")
        assert conn.browser_id == "bid"

    # --- tmux_session_name ---

    def test_tmux_session_name_returns_user_id(self):
        ctrl, _, _ = self._controller()
        assert ctrl.tmux_session_name() == "uid"

    # --- Connection backward-compat delegates + property shims ---

    async def test_connection_notify_user_terminal_windows_delegate(self):
        """Connection._notify_user_terminal_windows forwards to controller."""
        conn = _base_conn()
        windows = [{"id": "@0", "name": "bash"}]
        with patch.object(conn.terminal, "notify_user_terminal_windows") as m:
            conn._notify_user_terminal_windows(windows)
        m.assert_called_once_with(windows)

    async def test_connection_sync_terminal_windows_delegate(self):
        conn = _base_conn()
        windows = [{"id": "@0", "name": "bash"}]
        with patch.object(conn.terminal, "sync_terminal_windows") as m:
            conn.sync_terminal_windows(windows)
        m.assert_called_once_with(windows)

    async def test_connection_tmux_session_name_delegate(self):
        conn = _base_conn()
        with patch.object(
            conn.terminal, "tmux_session_name", return_value="uid"
        ) as m:
            assert conn.tmux_session_name() == "uid"
        m.assert_called_once_with()

    async def test_connection_activate_session_delegate(self):
        conn = _base_conn()
        session = AsyncMock()
        with patch.object(
            conn.terminal, "activate_session", new=AsyncMock(return_value=True)
        ) as m:
            result = await conn.activate_session(session, 80, 24)
        assert result is True
        m.assert_awaited_once_with(session, 80, 24)

    async def test_connection_stop_terminal_delegate(self):
        conn = _base_conn()
        with patch.object(conn.terminal, "stop", new=AsyncMock()) as m:
            await conn.stop_terminal()
        m.assert_awaited_once()

    async def test_connection_forward_terminal_output_delegate(self):
        conn = _base_conn()
        session = AsyncMock()
        with patch.object(
            conn.terminal, "forward_output", new=AsyncMock()
        ) as m:
            await conn.forward_terminal_output(session)
        m.assert_awaited_once_with(session)

    async def test_connection_claim_and_stop_terminal_delegate(self):
        conn = _base_conn()
        with patch.object(
            conn.terminal, "claim_and_stop", new=AsyncMock()
        ) as m:
            await conn._claim_and_stop_terminal()
        m.assert_awaited_once()

    async def test_terminal_session_property_round_trip(self):
        conn = _base_conn()
        sentinel = object()
        conn.terminal_session = sentinel
        assert conn.terminal_session is sentinel
        assert conn.terminal.session is sentinel

    async def test_terminal_task_property_round_trip(self):
        conn = _base_conn()
        task = asyncio.create_task(asyncio.sleep(999))
        try:
            conn.terminal_task = task
            assert conn.terminal_task is task
            assert conn.terminal.task is task
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def test_terminal_cols_rows_property_round_trip(self):
        conn = _base_conn()
        conn._terminal_cols = 120
        conn._terminal_rows = 40
        assert conn.terminal.cols == 120
        assert conn.terminal.rows == 40
        assert conn._terminal_cols == 120
        assert conn._terminal_rows == 40


class TestShareWindowHandlers:
    """Tests for the unified share/unshare/join terminal handlers."""

    async def test_share_window_broadcasts(self, user, temp_data_dir):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": False},
            {"name": "2", "index": 1, "id": "@1", "shared": False},
        ]
        await session.add_subscriber(sock, "cid")
        try:
            with patch.object(
                acl_mod.ACL,
                "check_permission",
                new=AsyncMock(return_value=True),
            ):
                await conn.handle_share_window({"window_id": "@1"})
            assert session.terminal_windows[user["id"]][1]["shared"] is True
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            shared_msgs = [
                c for c in calls if c.get("type") == "shared_terminals"
            ]
            assert len(shared_msgs) >= 1
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_share_window_permission_denied(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=False)
        ):
            await conn.handle_share_window({"window_id": "@0"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission" in c.get("message", "") for c in calls)

    async def test_unshare_window_kicks_joiners(self, user, temp_data_dir):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(_mock_term, "kill_joiner_sessions") as mock_kill,
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
            sockets.sessions.pop("ws-1", None)

    async def test_list_shared_terminals(self, user, temp_data_dir):
        async with _conn_in_workspace(
            user, "ws-1", user_home="/home/admin"
        ) as (sock, conn, session, app_state):
            session.terminal_windows[user["id"]] = [
                {"name": "1", "index": 0, "id": "@0", "shared": False},
                {"name": "build", "index": 1, "id": "@1", "shared": True},
            ]
            with patch.object(
                acl_mod.ACL,
                "check_permission",
                new=AsyncMock(return_value=True),
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

    async def test_shared_terminals_include_viewers(self, user, temp_data_dir):
        """shared_terminals response includes viewer list."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        owner_sock = _mock_sock()
        owner_conn = _base_conn(user=user, ws=owner_sock, app_state=app_state)
        owner_conn.workspace_id = "ws-v"
        owner_conn.container_id = "cid"
        owner_conn._user_home = "/home/admin"

        viewer_user = {
            "id": "viewer-1",
            "email": "viewer@test.com",
            "handle": "viewer",
        }
        viewer_sock = _mock_sock()
        viewer_conn = _base_conn(
            user=viewer_user, ws=viewer_sock, app_state=app_state
        )
        viewer_conn.workspace_id = "ws-v"
        viewer_conn.viewing_shared = {
            "user_id": user["id"],
            "window_id": "@0",
        }

        session = sockets.get_or_create_session("ws-v", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(owner_sock, "cid")
        await session.add_subscriber(viewer_sock, "cid")
        sockets.connections[owner_sock] = owner_conn
        sockets.connections[viewer_sock] = viewer_conn
        try:
            with patch.object(
                acl_mod.ACL,
                "check_permission",
                new=AsyncMock(return_value=True),
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
            sockets.sessions.pop("ws-v", None)
            sockets.connections.pop(owner_sock, None)
            sockets.connections.pop(viewer_sock, None)

    async def test_stop_terminal_broadcasts_viewer_change(self, user):
        """Stopping a terminal that was viewing shared broadcasts update."""
        async with _conn_in_workspace(
            user, "ws-sv", user_home="/home/admin"
        ) as (sock, conn, session, app_state):
            conn.viewing_shared = {"user_id": "owner-1", "window_id": "@0"}
            await conn.stop_terminal()
            assert conn.viewing_shared is None
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            shared = [c for c in calls if c.get("type") == "shared_terminals"]
            assert len(shared) == 1

    async def test_create_shared_terminal_legacy(self, user, temp_data_dir):
        """Legacy create_shared_terminal creates a window and marks it shared."""
        async with _conn_in_workspace(
            user, "ws-1", user_home="/home/admin"
        ) as (sock, conn, session, app_state):
            session.terminal_windows[user["id"]] = [
                {"name": "1", "index": 0, "id": "@0", "shared": False}
            ]
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(
                    _mock_term,
                    "new_window",
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
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=True)
        ):
            await conn.handle_share_window({})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("id" in c.get("message", "").lower() for c in calls)

    async def test_share_window_not_found(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": False}
        ]
        try:
            with patch.object(
                acl_mod.ACL,
                "check_permission",
                new=AsyncMock(return_value=True),
            ):
                await conn.handle_share_window({"window_id": "@99"})
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                "not found" in c.get("message", "").lower() for c in calls
            )
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_share_window_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "no-session-ws"
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=True)
        ):
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
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=False)
        ):
            await conn.handle_unshare_window({"window_id": "@0"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission" in c.get("message", "") for c in calls)

    async def test_unshare_window_missing_id(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=True)
        ):
            await conn.handle_unshare_window({})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("id" in c.get("message", "").lower() for c in calls)

    async def test_unshare_window_not_found(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": True}
        ]
        try:
            with patch.object(
                acl_mod.ACL,
                "check_permission",
                new=AsyncMock(return_value=True),
            ):
                await conn.handle_unshare_window({"window_id": "@99"})
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                "not found" in c.get("message", "").lower() for c in calls
            )
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_unshare_window_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "no-session-ws"
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=True)
        ):
            await conn.handle_unshare_window({"window_id": "@0"})

    async def test_unshare_kill_error_handled(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": True}
        ]
        await session.add_subscriber(sock, "cid")
        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(
                    _mock_term,
                    "kill_joiner_sessions",
                    side_effect=TerminalError("no sessions"),
                ),
            ):
                await conn.handle_unshare_window({"window_id": "@0"})
            assert session.terminal_windows[user["id"]][0]["shared"] is False
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_join_shared_terminal(self, user, temp_data_dir):
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        owner = await model.create_user(
            "owner@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        registry.track_activity("cid", "ws-1")
        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(_ws_controllers, "TerminalSession") as MockTS,
                patch.object(_mock_term, "select_window"),
                patch.object(_mock_term, "tmux_command", return_value=""),
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
            sockets.sessions.pop("ws-1", None)
            registry.states.pop("ws-1", None)

    async def test_join_service_terminal_routes_to_service_session(
        self, user, temp_data_dir
    ):
        """Joining the agent's service window targets the standalone
        ``service`` tmux session, not a session named after the agent's
        user_id (which doesn't exist) -- #1158/#1159."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[model.AGENT_USER_ID] = [
            {"name": "service-cmd", "index": 0, "id": "@0", "shared": True},
        ]
        session.agent_handle = "clanker"
        registry.track_activity("cid", "ws-1")
        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(_ws_controllers, "TerminalSession") as MockTS,
                patch.object(_mock_term, "select_window"),
                patch.object(_mock_term, "tmux_command", return_value=""),
            ):
                mock_sess = _mock_terminal()
                MockTS.return_value = mock_sess

                async def fake_output():
                    return
                    yield

                mock_sess.output = fake_output

                await conn.handle_join_shared_terminal(
                    {"user_id": model.AGENT_USER_ID, "window_id": "@0"}
                )
                await asyncio.sleep(0)

            MockTS.assert_called_once()
            call_kwargs = MockTS.call_args[1]
            # The join targets the constant ``service`` session, NOT the
            # agent's user_id (there is no tmux session named after it).
            assert call_kwargs["join_session"] == "service"
            started = [
                c[0][0]
                for c in sock.send_json.call_args_list
                if c[0][0].get("type") == "terminal_started"
            ]
            assert len(started) == 1
            assert started[0]["shared_user_id"] == model.AGENT_USER_ID
        finally:
            sockets.sessions.pop("ws-1", None)
            registry.states.pop("ws-1", None)

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
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=False)
        ):
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
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=True)
        ):
            await conn.handle_join_shared_terminal({})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("required" in c.get("message", "").lower() for c in calls)

    async def test_join_shared_terminal_superseded(self, user, temp_data_dir):
        """If session is superseded during start, activate_session returns False."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        owner = await model.create_user(
            "owner-sup@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        registry.track_activity("cid", "ws-1")

        async def fake_start(*a, **kw):
            # Supersede the session before activate_session runs
            conn.terminal_session = None

        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(_ws_controllers, "TerminalSession") as MockTS,
                patch.object(
                    _mock_term,
                    "tmux_command",
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
            sockets.sessions.pop("ws-1", None)
            registry.states.pop("ws-1", None)

    async def test_join_shared_terminal_select_fallback(
        self, user, temp_data_dir
    ):
        """Falls back to bare @N when joiner session select fails."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        owner = await model.create_user(
            "owner-fb@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        sockets.connections[sock] = conn
        registry.track_activity("cid", "ws-1")

        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(_ws_controllers, "TerminalSession") as MockTS,
                patch.object(
                    _mock_term,
                    "tmux_command",
                    new_callable=AsyncMock,
                    side_effect=TerminalError("can't find session"),
                ),
                patch.object(
                    _mock_term,
                    "select_window",
                    new_callable=AsyncMock,
                ) as mock_select,
            ):
                mock_sess = _mock_terminal()
                mock_sess.tmux_session_name = "joiner-abc"

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
            sockets.sessions.pop("ws-1", None)
            sockets.connections.pop(sock, None)
            registry.states.pop("ws-1", None)

    async def test_join_shared_terminal_no_joiner_session(
        self, user, temp_data_dir
    ):
        """Falls back to bare @N when joiner session name is None."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        owner = await model.create_user(
            "owner-nj@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        await session.add_subscriber(sock, "cid")
        sockets.connections[sock] = conn
        registry.track_activity("cid", "ws-1")

        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(_ws_controllers, "TerminalSession") as MockTS,
                patch.object(
                    _mock_term,
                    "select_window",
                    new_callable=AsyncMock,
                ) as mock_select,
            ):
                mock_sess = _mock_terminal()
                # No joiner session name
                mock_sess.tmux_session_name = None

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
            sockets.sessions.pop("ws-1", None)
            sockets.connections.pop(sock, None)
            registry.states.pop("ws-1", None)

    async def test_join_shared_terminal_start_error(self, user, temp_data_dir):
        """If session.start() fails, error is sent and session stopped."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        owner = await model.create_user(
            "owner-err@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(_ws_controllers, "TerminalSession") as MockTS,
            ):
                mock_sess = _mock_terminal()
                mock_sess.start = AsyncMock(
                    side_effect=TerminalError("start failed")
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
            sockets.sessions.pop("ws-1", None)

    async def test_join_shared_terminal_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/x"
        conn.workspace_id = "no-session"
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=True)
        ):
            await conn.handle_join_shared_terminal(
                {"user_id": "x", "window_id": "@99"}
            )
        # Early return, no error sent

    async def test_join_shared_terminal_not_found(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn._user_home = "/home/x"
        conn.workspace_id = "ws-1"
        sockets.get_or_create_session("ws-1", app_state)
        try:
            with patch.object(
                acl_mod.ACL,
                "check_permission",
                new=AsyncMock(return_value=True),
            ):
                await conn.handle_join_shared_terminal(
                    {"user_id": "nobody", "window_id": "@99"}
                )
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                "not found" in c.get("message", "").lower() for c in calls
            )
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_delete_shared_terminal(self, user, temp_data_dir):
        async with _conn_in_workspace(
            user, "ws-1", user_home="/home/admin"
        ) as (sock, conn, session, app_state):
            session.terminal_windows[user["id"]] = [
                {"name": "1", "index": 0, "id": "@0", "shared": False},
                {"name": "build", "index": 1, "id": "@1", "shared": True},
            ]
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(_mock_term, "kill_joiner_sessions"),
                patch.object(_mock_term, "close_window", return_value=[]),
            ):
                await conn.handle_delete_shared_terminal(
                    {"user_id": user["id"], "window_id": "@1"}
                )
            windows = session.terminal_windows[user["id"]]
            assert len(windows) == 1
            assert windows[0]["name"] == "1"

    async def test_delete_shared_terminal_no_container(self, user):
        conn = _base_conn(user=user)
        await conn.handle_delete_shared_terminal(
            {"user_id": "x", "window_id": "@99"}
        )

    async def test_delete_shared_terminal_permission_denied(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=False)
        ):
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
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=True)
        ):
            await conn.handle_delete_shared_terminal({})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("required" in c.get("message", "").lower() for c in calls)

    async def test_delete_shared_terminal_not_found(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws-1"
        sockets.get_or_create_session("ws-1", app_state)
        try:
            with patch.object(
                acl_mod.ACL,
                "check_permission",
                new=AsyncMock(return_value=True),
            ):
                await conn.handle_delete_shared_terminal(
                    {"user_id": user["id"], "window_id": "@99"}
                )
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                "not found" in c.get("message", "").lower() for c in calls
            )
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_delete_shared_terminal_other_user_denied(
        self, user, temp_data_dir
    ):
        """A collaborator may not delete another user's terminal
        (regression for #874)."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        other = await model.create_user(
            "other@example.com", "x", verified=True
        )
        # Workspace owned by `other`; caller is neither the terminal
        # owner nor the workspace owner.
        workspace = await model.create_workspace(other["id"], "ws-other")
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = workspace["id"]
        session = sockets.get_or_create_session(workspace["id"], app_state)
        session.terminal_windows[other["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        try:
            with patch.object(
                acl_mod.ACL,
                "check_permission",
                new=AsyncMock(return_value=True),
            ):
                await conn.handle_delete_shared_terminal(
                    {"user_id": other["id"], "window_id": "@0"}
                )
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                "permission" in c.get("message", "").lower() for c in calls
            )
            # Window is untouched.
            assert len(session.terminal_windows[other["id"]]) == 1
        finally:
            sockets.sessions.pop(workspace["id"], None)
            sockets.connections.pop(sock, None)

    async def test_delete_shared_terminal_workspace_owner_can_delete_others(
        self, user, temp_data_dir
    ):
        """The workspace owner may delete another member's terminal."""
        other = await model.create_user(
            "other@example.com", "x", verified=True
        )
        # Workspace owned by the caller (`user`).
        workspace = await model.create_workspace(user["id"], "ws-mine")
        async with _conn_in_workspace(user, workspace["id"]) as (
            sock,
            conn,
            session,
            app_state,
        ):
            session.terminal_windows[other["id"]] = [
                {"name": "build", "index": 0, "id": "@0", "shared": True},
            ]
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(_mock_term, "kill_joiner_sessions"),
                patch.object(_mock_term, "close_window", return_value=[]),
            ):
                await conn.handle_delete_shared_terminal(
                    {"user_id": other["id"], "window_id": "@0"}
                )
            assert session.terminal_windows[other["id"]] == []

    async def test_delete_shared_terminal_error(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(
                    _mock_term,
                    "kill_joiner_sessions",
                    side_effect=RuntimeError("boom"),
                ),
            ):
                await conn.handle_delete_shared_terminal(
                    {"user_id": user["id"], "window_id": "@0"}
                )
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any("Failed" in c.get("message", "") for c in calls)
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_create_shared_terminal_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "no-session"
        with (
            patch.object(
                acl_mod.ACL,
                "check_permission",
                new=AsyncMock(return_value=True),
            ),
            patch.object(_mock_term, "new_window", return_value=[]),
        ):
            await conn.handle_create_shared_terminal({"name": "dev"})
        # Early return after new_window — no crash

    async def test_delete_shared_terminal_no_session(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn.workspace_id = "no-session"
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=True)
        ):
            await conn.handle_delete_shared_terminal(
                {"user_id": user["id"], "window_id": "@99"}
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
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=False)
        ):
            await conn.handle_create_shared_terminal({"name": "x"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission" in c.get("message", "") for c in calls)

    async def test_create_shared_terminal_empty_name(self, user, app_state):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=True)
        ):
            await conn.handle_create_shared_terminal({"name": ""})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Name" in c.get("message", "") for c in calls)

    async def test_create_shared_terminal_error(self, user, app_state):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        conn.workspace_id = "ws-1"
        with (
            patch.object(
                acl_mod.ACL,
                "check_permission",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                _mock_term,
                "new_window",
                side_effect=RuntimeError("fail"),
            ),
        ):
            await conn.handle_create_shared_terminal({"name": "dev"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Failed" in c.get("message", "") for c in calls)

    async def test_list_shared_terminals_no_workspace(self):
        conn = _base_conn()
        await conn.handle_list_shared_terminals()

    async def test_list_shared_terminals_permission_denied(
        self, user, app_state
    ):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=False)
        ):
            await conn.handle_list_shared_terminals()
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Permission" in c.get("message", "") for c in calls)

    async def test_list_shared_terminals_no_session(self, user, app_state):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "no-session"
        with patch.object(
            acl_mod.ACL, "check_permission", new=AsyncMock(return_value=True)
        ):
            await conn.handle_list_shared_terminals()
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        shared = [c for c in calls if c.get("type") == "shared_terminals"]
        assert shared[0]["terminals"] == []

    async def test_has_perm_checks_acl(self, user, temp_data_dir, app_state):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        ws = await _create_workspace_with_acl(app_state, user["id"], "perm-ws")
        conn.workspace_id = ws["id"]
        assert await conn._has_perm("view")

    async def test_has_perm_no_workspace(self):
        conn = _base_conn()
        assert not await conn._has_perm("view")


class TestSharedTerminalController:
    """Unit tests for the SharedTerminalController collaborator in isolation.

    These exercise the controller directly against a lightweight fake
    connection (a SimpleNamespace), proving it is decoupled from
    Connection (issue #961) and covering the branches the existing
    Connection-level tests reach only indirectly — notably the
    ``Connection._handle_list_error`` backward-compat delegate, the
    ``join_shared_terminal`` ``asyncio.CancelledError`` re-raise, and
    the ``find_window``/``broadcast_shared_terminals`` helpers as
    controller methods.
    """

    def _controller(
        self,
        *,
        container_id="cid",
        workspace_id="ws-1",
        user_home="/home/alice",
        sock=None,
        has_perm=True,
        user=None,
        app_state=None,
    ):
        if sock is None:
            sock = _mock_sock()
        if user is None:
            user = {
                "id": "uid",
                "email": "alice@example.com",
                "handle": "alice",
            }
        if app_state is None:
            app_state = _make_app_state()

        class _FakeConn:
            """Minimal Connection stand-in for isolated controller tests."""

            def __init__(self):
                self.sock = sock
                self.container_id = container_id
                self.workspace_id = workspace_id
                self._user_home = user_home
                self.user = user
                self.app_state = app_state
                self._has_perm = AsyncMock(return_value=has_perm)
                self.stop_terminal = AsyncMock()
                self.activate_session = AsyncMock(return_value=True)
                self.tmux_session_name = MagicMock(return_value="uid")
                self.sync_terminal_windows = MagicMock()
                self._terminal_cols = 80
                self._terminal_rows = 24
                self.terminal = SimpleNamespace(
                    session=None,
                    task=None,
                    _sync_service_windows=AsyncMock(return_value=False),
                )

            @property
            def terminal_session(self):
                return self.terminal.session

            @terminal_session.setter
            def terminal_session(self, value):
                self.terminal.session = value

            @property
            def terminal_task(self):
                return self.terminal.task

            @terminal_task.setter
            def terminal_task(self, value):
                self.terminal.task = value

        conn = _FakeConn()
        ctrl = SharedTerminalController(conn)
        return ctrl, sock, conn

    def _ws_session(self, ws_id="ws-1", app_state=None):
        if app_state is None:
            app_state = _make_app_state()
        return app_state.sockets.get_or_create_session(ws_id, app_state)

    # --- find_window ---

    async def test_find_window_returns_match(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, _, conn = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": False}
        ]
        try:
            found = ctrl.find_window(ws, user["id"], "@0")
            assert found is not None
            assert found["name"] == "a"
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_find_window_not_found_sends_error(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, conn = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        try:
            assert ctrl.find_window(ws, user["id"], "@99") is None
            msg = sock.send_json.call_args[0][0]
            assert msg == {"type": "error", "message": "Window not found"}
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_find_window_shared_true_rejects_unshared(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, conn = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": False}
        ]
        try:
            assert (
                ctrl.find_window(
                    ws, user["id"], "@0", shared=True, error_msg="nope"
                )
                is None
            )
            assert sock.send_json.call_args[0][0]["message"] == "nope"
        finally:
            sockets.sessions.pop("ws-1", None)

    # --- share_window ---

    async def test_share_window_marks_and_broadcasts(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, _, conn = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": False}
        ]
        try:
            await ctrl.share_window({"window_id": "@0"})
            assert ws.terminal_windows[user["id"]][0]["shared"] is True
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_share_window_no_container(self, user):
        ctrl, _, _ = self._controller(user=user, container_id=None)
        await ctrl.share_window({"window_id": "@0"})

    async def test_share_window_no_perm(self, user):
        ctrl, sock, _ = self._controller(user=user, has_perm=False)
        await ctrl.share_window({"window_id": "@0"})
        assert "Permission" in sock.send_json.call_args[0][0]["message"]

    async def test_share_window_missing_id(self, user):
        ctrl, sock, _ = self._controller(user=user)
        await ctrl.share_window({})
        assert "Window ID" in sock.send_json.call_args[0][0]["message"]

    async def test_share_window_not_found(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[user["id"]] = []
        try:
            await ctrl.share_window({"window_id": "@99"})
            assert sock.send_json.call_args[0][0]["type"] == "error"
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_share_window_no_session(self, user):
        ctrl, _, _ = self._controller(user=user, workspace_id="none")
        await ctrl.share_window({"window_id": "@0"})

    # --- unshare_window ---

    async def test_unshare_window_no_container(self, user):
        ctrl, _, _ = self._controller(user=user, container_id=None)
        await ctrl.unshare_window({"window_id": "@0"})

    async def test_unshare_window_no_perm(self, user):
        ctrl, sock, _ = self._controller(user=user, has_perm=False)
        await ctrl.unshare_window({"window_id": "@0"})
        assert "Permission" in sock.send_json.call_args[0][0]["message"]

    async def test_unshare_window_marks_unshared_and_kicks(
        self, user, temp_data_dir
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, _, conn = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": True}
        ]
        try:
            with patch.object(_mock_term, "kill_joiner_sessions") as kill:
                await ctrl.unshare_window({"window_id": "@0"})
            assert ws.terminal_windows[user["id"]][0]["shared"] is False
            kill.assert_awaited_once_with("cid", "uid")
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_unshare_kill_error_handled(self, user, temp_data_dir):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        await ws.add_subscriber(sock, "cid")
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": True}
        ]
        try:
            with patch.object(
                _mock_term,
                "kill_joiner_sessions",
                side_effect=OSError("boom"),
            ):
                # Should not raise.
                await ctrl.unshare_window({"window_id": "@0"})
            # Still broadcast the deletion.
            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                s.get("type") == "shared_terminal_deleted" for s in sent
            )
        finally:
            await ws.remove_subscriber(sock)
            sockets.sessions.pop("ws-1", None)

    # --- list_shared_terminals ---

    async def test_list_shared_terminals_no_workspace(self, user):
        ctrl, _, _ = self._controller(user=user, workspace_id=None)
        await ctrl.list_shared_terminals()

    async def test_list_shared_terminals_no_perm(self, user):
        ctrl, sock, _ = self._controller(user=user, has_perm=False)
        await ctrl.list_shared_terminals()
        assert "Permission" in sock.send_json.call_args[0][0]["message"]

    async def test_list_shared_terminals_no_session_sends_empty(self, user):
        ctrl, sock, _ = self._controller(user=user, workspace_id="none")
        await ctrl.list_shared_terminals()
        msg = sock.send_json.call_args[0][0]
        assert msg == {"type": "shared_terminals", "terminals": []}

    async def test_list_shared_terminals_sends_list(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": True}
        ]
        try:
            await ctrl.list_shared_terminals()
            msg = sock.send_json.call_args[0][0]
            assert msg["type"] == "shared_terminals"
            assert isinstance(msg["terminals"], list)
        finally:
            sockets.sessions.pop("ws-1", None)

    # --- broadcast_shared_terminals ---

    async def test_broadcast_shared_terminals_broadcasts(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, conn = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        await ws.add_subscriber(sock, "cid")
        try:
            ctrl.broadcast_shared_terminals(ws)
            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(s.get("type") == "shared_terminals" for s in sent)
        finally:
            await ws.remove_subscriber(sock)
            sockets.sessions.pop("ws-1", None)

    # --- create_shared_terminal (legacy) ---

    async def test_create_shared_terminal_no_container(self, user):
        ctrl, _, _ = self._controller(user=user, container_id=None)
        await ctrl.create_shared_terminal({"name": "x"})

    async def test_create_shared_terminal_no_perm(self, user):
        ctrl, sock, _ = self._controller(user=user, has_perm=False)
        await ctrl.create_shared_terminal({"name": "x"})
        assert "Permission" in sock.send_json.call_args[0][0]["message"]

    async def test_create_shared_terminal_empty_name(self, user, app_state):
        ctrl, sock, _ = self._controller(user=user)
        await ctrl.create_shared_terminal({"name": "  "})
        assert "Name" in sock.send_json.call_args[0][0]["message"]

    async def test_create_shared_terminal_marks_new_window_shared(
        self, user, temp_data_dir
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        await ws.add_subscriber(sock, "cid")
        try:
            with patch.object(
                _mock_term,
                "new_window",
                return_value=[{"id": "@0", "index": 0, "name": "build"}],
            ):
                await ctrl.create_shared_terminal({"name": "build"})
            # sync_terminal_windows is a delegate that Connection would
            # route to TerminalController; on the fake conn it's a
            # MagicMock, so populate the windows manually as the real
            # sync_terminal_windows would.
            ws.terminal_windows[user["id"]] = [
                {"id": "@0", "index": 0, "name": "build", "shared": True}
            ]
            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(s.get("type") == "shared_terminals" for s in sent)
        finally:
            await ws.remove_subscriber(sock)
            sockets.sessions.pop("ws-1", None)

    async def test_create_shared_terminal_error_sends_error(
        self, user, temp_data_dir
    ):
        ctrl, sock, _ = self._controller(user=user)
        with patch.object(
            _mock_term,
            "new_window",
            side_effect=OSError("boom"),
        ):
            await ctrl.create_shared_terminal({"name": "x"})
        assert sock.send_json.call_args[0][0]["type"] == "error"

    async def test_create_shared_terminal_no_session(self, user):
        ctrl, _, _ = self._controller(user=user, workspace_id="none")
        with patch.object(
            _mock_term,
            "new_window",
            return_value=[{"id": "@0", "index": 0, "name": "x"}],
        ):
            await ctrl.create_shared_terminal({"name": "x"})

    # --- delete_shared_terminal (legacy) ---

    async def test_delete_shared_terminal_no_container(self, user):
        ctrl, _, _ = self._controller(user=user, container_id=None)
        await ctrl.delete_shared_terminal({"user_id": "u", "window_id": "@0"})

    async def test_delete_shared_terminal_no_perm(self, user):
        ctrl, sock, _ = self._controller(user=user, has_perm=False)
        await ctrl.delete_shared_terminal({"user_id": "u", "window_id": "@0"})
        assert "Permission" in sock.send_json.call_args[0][0]["message"]

    async def test_delete_shared_terminal_missing_fields(self, user):
        ctrl, sock, _ = self._controller(user=user)
        await ctrl.delete_shared_terminal({"user_id": "u"})
        assert "required" in sock.send_json.call_args[0][0]["message"]

    async def test_delete_shared_terminal_other_user_denied(
        self, user, temp_data_dir
    ):
        ctrl, sock, conn = self._controller(user=user)
        with patch.object(
            conn.app_state.model.workspaces,
            "get_workspace_by_id",
            new=AsyncMock(),
        ) as gw:
            gw.return_value = {"user_id": "someone-else"}
            await ctrl.delete_shared_terminal(
                {"user_id": "owner-1", "window_id": "@0"}
            )
        assert "Permission" in sock.send_json.call_args[0][0]["message"]

    async def test_delete_shared_terminal_not_found(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows["owner-1"] = []
        try:
            await ctrl.delete_shared_terminal(
                {"user_id": "owner-1", "window_id": "@99"}
            )
            assert sock.send_json.call_args[0][0]["type"] == "error"
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_delete_shared_terminal_no_session(self, user):
        ctrl, _, _ = self._controller(user=user, workspace_id="none")
        await ctrl.delete_shared_terminal({"user_id": "u", "window_id": "@0"})

    async def test_delete_shared_terminal_closes_and_broadcasts(
        self, user, temp_data_dir
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        await ws.add_subscriber(sock, "cid")
        ws.terminal_windows["owner-1"] = [
            {"id": "@0", "index": 0, "name": "build", "shared": True},
            {"id": "@1", "index": 1, "name": "other", "shared": False},
        ]
        try:
            # owner_user_id != user["id"], so the delete handler calls
            # model.get_workspace_by_id to authorize; return a workspace
            # owned by the current user so the delete is permitted.
            with (
                patch.object(
                    app_state.model.workspaces,
                    "get_workspace_by_id",
                    new=AsyncMock(return_value={"user_id": user["id"]}),
                ),
                patch.object(_mock_term, "kill_joiner_sessions") as kill,
                patch.object(_mock_term, "close_window") as close,
            ):
                await ctrl.delete_shared_terminal(
                    {"user_id": "owner-1", "window_id": "@0"}
                )
            kill.assert_awaited_once_with("cid", "owner-1")
            close.assert_awaited_once_with("cid", "owner-1", "@0")
            remaining = ws.terminal_windows["owner-1"]
            assert [w["id"] for w in remaining] == ["@1"]
            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                s.get("type") == "shared_terminal_deleted" for s in sent
            )
        finally:
            await ws.remove_subscriber(sock)
            sockets.sessions.pop("ws-1", None)

    async def test_delete_shared_terminal_error_sends_error(
        self, user, temp_data_dir
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows["owner-1"] = [
            {"id": "@0", "index": 0, "name": "build", "shared": True}
        ]
        try:
            with patch.object(
                _mock_term,
                "kill_joiner_sessions",
                side_effect=OSError("boom"),
            ):
                await ctrl.delete_shared_terminal(
                    {"user_id": "owner-1", "window_id": "@0"}
                )
            assert sock.send_json.call_args[0][0]["type"] == "error"
        finally:
            sockets.sessions.pop("ws-1", None)

    # --- join_shared_terminal ---

    async def test_join_shared_terminal_no_container(self, user):
        ctrl, _, _ = self._controller(user=user, container_id=None)
        await ctrl.join_shared_terminal({"user_id": "x", "window_id": "@0"})

    async def test_join_shared_terminal_no_perm(self, user):
        ctrl, sock, _ = self._controller(user=user, has_perm=False)
        await ctrl.join_shared_terminal({"user_id": "x", "window_id": "@0"})
        assert "Permission" in sock.send_json.call_args[0][0]["message"]

    async def test_join_shared_terminal_missing_fields(self, user):
        ctrl, sock, _ = self._controller(user=user)
        await ctrl.join_shared_terminal({"user_id": ""})
        assert "required" in sock.send_json.call_args[0][0]["message"]

    async def test_join_shared_terminal_not_found(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows["owner-1"] = []
        try:
            await ctrl.join_shared_terminal(
                {"user_id": "owner-1", "window_id": "@99"}
            )
            assert sock.send_json.call_args[0][0]["type"] == "error"
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_join_shared_terminal_no_session(self, user):
        ctrl, _, _ = self._controller(user=user, workspace_id="none")
        await ctrl.join_shared_terminal({"user_id": "x", "window_id": "@0"})

    async def test_join_shared_terminal_sets_viewing_and_starts(
        self, user, temp_data_dir
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, conn = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows["owner-1"] = [
            {"id": "@0", "index": 0, "name": "build", "shared": True}
        ]
        try:
            with (
                patch.object(_ws_controllers, "TerminalSession") as MockTS,
                patch.object(_mock_term, "tmux_command"),
                patch.object(_mock_term, "select_window"),
            ):
                mock_sess = _mock_terminal()
                MockTS.return_value = mock_sess
                await ctrl.join_shared_terminal(
                    {"user_id": "owner-1", "window_id": "@0"}
                )
                # Drain the spawned start task.
                await asyncio.sleep(0)
            # viewing_shared marker set.
            assert ctrl.viewing_shared == {
                "user_id": "owner-1",
                "window_id": "@0",
            }
            conn.stop_terminal.assert_awaited_once()
            MockTS.assert_called_once()
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_join_shared_terminal_start_error_sends_error(
        self, user, temp_data_dir
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets
        ctrl, sock, conn = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows["owner-1"] = [
            {"id": "@0", "index": 0, "name": "build", "shared": True}
        ]
        try:
            with (
                patch.object(_ws_controllers, "TerminalSession") as MockTS,
                patch.object(
                    _mock_term,
                    "tmux_command",
                    side_effect=TerminalError("nope"),
                ),
                patch.object(_mock_term, "select_window"),
            ):
                mock_sess = _mock_terminal()
                mock_sess.start = AsyncMock(side_effect=OSError("boom"))
                MockTS.return_value = mock_sess
                await ctrl.join_shared_terminal(
                    {"user_id": "owner-1", "window_id": "@0"}
                )
                await asyncio.sleep(0)
            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                "Failed to join shared terminal" in s.get("message", "")
                for s in sent
            )
            mock_sess.stop.assert_awaited_once()
        finally:
            sockets.sessions.pop("ws-1", None)

    # --- handle_list_error ---

    async def test_handle_list_error_sends_error(self, user):
        ctrl, sock, _ = self._controller(user=user)
        await ctrl.handle_list_error(ValueError("boom"))
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "error"
        assert "Failed to list shared terminals" in msg["message"]

    # --- Connection backward-compat delegates + property shim ---

    async def test_connection_handle_list_error_delegate(self, user):
        """Connection._handle_list_error forwards to the controller."""
        conn = _base_conn(user=user)
        with patch.object(
            conn.shared, "handle_list_error", new=AsyncMock()
        ) as m:
            await conn._handle_list_error(ValueError("x"))
        m.assert_awaited_once()

    async def test_connection_share_window_delegate(self, user):
        conn = _base_conn(user=user)
        with patch.object(conn.shared, "share_window", new=AsyncMock()) as m:
            await conn.handle_share_window({"window_id": "@0"})
        m.assert_awaited_once_with({"window_id": "@0"})

    async def test_connection_unshare_window_delegate(self, user):
        conn = _base_conn(user=user)
        with patch.object(conn.shared, "unshare_window", new=AsyncMock()) as m:
            await conn.handle_unshare_window({"window_id": "@0"})
        m.assert_awaited_once_with({"window_id": "@0"})

    async def test_connection_join_shared_terminal_delegate(self, user):
        conn = _base_conn(user=user)
        with patch.object(
            conn.shared, "join_shared_terminal", new=AsyncMock()
        ) as m:
            await conn.handle_join_shared_terminal(
                {"user_id": "x", "window_id": "@0"}
            )
        m.assert_awaited_once_with({"user_id": "x", "window_id": "@0"})

    async def test_connection_list_shared_terminals_delegate(self, user):
        conn = _base_conn(user=user)
        with patch.object(
            conn.shared, "list_shared_terminals", new=AsyncMock()
        ) as m:
            await conn.handle_list_shared_terminals()
        m.assert_awaited_once()

    async def test_connection_create_shared_terminal_delegate(self, user):
        conn = _base_conn(user=user)
        with patch.object(
            conn.shared, "create_shared_terminal", new=AsyncMock()
        ) as m:
            await conn.handle_create_shared_terminal({"name": "x"})
        m.assert_awaited_once_with({"name": "x"})

    async def test_connection_delete_shared_terminal_delegate(self, user):
        conn = _base_conn(user=user)
        with patch.object(
            conn.shared, "delete_shared_terminal", new=AsyncMock()
        ) as m:
            await conn.handle_delete_shared_terminal(
                {"user_id": "u", "window_id": "@0"}
            )
        m.assert_awaited_once_with({"user_id": "u", "window_id": "@0"})

    async def test_connection_broadcast_shared_terminals_delegate(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        conn = _base_conn(user=user, app_state=app_state)
        ws = sockets.get_or_create_session("ws-1", app_state)
        try:
            with patch.object(conn.shared, "broadcast_shared_terminals") as m:
                conn.broadcast_shared_terminals(ws)
            m.assert_called_once_with(ws)
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_connection_find_window_delegate(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        conn = _base_conn(user=user, app_state=app_state)
        ws = sockets.get_or_create_session("ws-1", app_state)
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": False}
        ]
        try:
            with patch.object(
                conn.shared, "find_window", return_value={"id": "@0"}
            ) as m:
                result = conn._find_window(ws, user["id"], "@0")
            assert result == {"id": "@0"}
            m.assert_called_once_with(
                ws,
                user["id"],
                "@0",
                shared=False,
                error_msg="Window not found",
            )
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_viewing_shared_property_round_trip(self, user):
        conn = _base_conn(user=user)
        marker = {"user_id": "x", "window_id": "@0"}
        conn.viewing_shared = marker
        assert conn.viewing_shared is marker
        assert conn.shared.viewing_shared is marker


class TestFindWindow:
    """Direct tests for the extracted _find_window helper (#899).

    Locks its contract independently of the handlers that call it.
    In particular the shared=True branch, where a window that exists
    but is not shared must be rejected — previously covered, if at
    all, only incidentally through the join handlers.
    """

    def _setup(self, user, windows, app_state=None):
        if app_state is None:
            app_state = _make_app_state()
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        session = app_state.sockets.get_or_create_session("ws-find", app_state)
        if windows is not None:
            session.terminal_windows[user["id"]] = windows
        return sock, conn, session

    def _messages(self, sock):
        return [c[0][0] for c in sock.send_json.call_args_list]

    async def test_found_returns_window(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock, conn, session = self._setup(
            user,
            [{"id": "@0", "name": "a", "shared": False}],
            app_state=app_state,
        )
        try:
            assert (
                conn._find_window(session, user["id"], "@0")
                == (session.terminal_windows[user["id"]][0])
            )
            assert self._messages(sock) == []
        finally:
            sockets.sessions.pop("ws-find", None)

    async def test_not_found_sends_error_and_returns_none(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock, conn, session = self._setup(user, [], app_state=app_state)
        try:
            assert conn._find_window(session, user["id"], "@99") is None
            assert self._messages(sock) == [
                {"type": "error", "message": "Window not found"}
            ]
        finally:
            sockets.sessions.pop("ws-find", None)

    async def test_shared_true_finds_shared_window(self, user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock, conn, session = self._setup(
            user,
            [{"id": "@0", "name": "a", "shared": True}],
            app_state=app_state,
        )
        try:
            found = conn._find_window(session, user["id"], "@0", shared=True)
            assert found is not None
            assert found["name"] == "a"
        finally:
            sockets.sessions.pop("ws-find", None)

    async def test_shared_true_rejects_unshared_window(self, user, app_state):
        """A present-but-unshared window is treated as not found."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock, conn, session = self._setup(
            user,
            [{"id": "@0", "name": "a", "shared": False}],
            app_state=app_state,
        )
        try:
            assert (
                conn._find_window(session, user["id"], "@0", shared=True)
                is None
            )
            assert self._messages(sock) == [
                {"type": "error", "message": "Window not found"}
            ]
        finally:
            sockets.sessions.pop("ws-find", None)

    async def test_custom_error_message(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock, conn, session = self._setup(user, [], app_state=app_state)
        try:
            assert (
                conn._find_window(
                    session,
                    user["id"],
                    "@99",
                    error_msg="Shared terminal not found",
                )
                is None
            )
            assert self._messages(sock) == [
                {"type": "error", "message": "Shared terminal not found"}
            ]
        finally:
            sockets.sessions.pop("ws-find", None)


class TestFractionalTimeout:
    async def test_fractional_timeout_display(
        self, user, monkeypatch, agent_user
    ):
        app_state = _make_app_state()
        registry = app_state.container_registry
        monkeypatch.setattr(
            app_state.container_registry, "idle_timeout_seconds", 90
        )
        sock = _mock_sock()
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "frac-ws"
        )
        conn = _base_conn(user=user, ws=sock, app_state=app_state)

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
                registry,
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
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-cancel", app_state)
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        session.browser_subscribers.add(mock_sock)
        try:
            # Snapshot request IDs before so we can check ours was cleaned up
            before = set(sockets.pending_browser_requests.keys())
            task = asyncio.create_task(
                session.dispatch_browser_request(
                    {"action": "fetch"},
                    timeout=10.0,
                )
            )
            await asyncio.sleep(0.05)
            # Find the new request_id added by our dispatch
            new_ids = set(sockets.pending_browser_requests.keys()) - before
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # Our request should have been cleaned up
            for rid in new_ids:
                assert rid not in sockets.pending_browser_requests
        finally:
            sockets.sessions.pop("ws-cancel", None)


class TestDispatchBrowserRequestDeadSubscribers:
    async def test_all_subscribers_dead(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-all-dead", app_state)
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
            sockets.sessions.pop("ws-all-dead", None)


class TestSendQueueBehavior:
    """Tests for the bounded outbound send queue (BRYAN5)."""

    async def test_slow_client_closes_connection(self, user):
        """When the send queue is full, handle_websocket drops the client."""
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})

        # Make the raw websocket.send_json block forever so the queue fills up
        send_blocked = asyncio.Event()

        async def blocking_send(data):
            send_blocked.set()
            await asyncio.sleep(3600)

        websocket.send_json = AsyncMock(side_effect=blocking_send)

        # Client sends many messages that trigger send_json responses
        msgs = [json.dumps({"cmd": "bogus"})] * (SEND_QUEUE_SIZE + 5) + [
            WebSocketDisconnect()
        ]
        websocket.receive_text = AsyncMock(side_effect=msgs)

        # Should complete without hanging — SlowClientError triggers exit
        await asyncio.wait_for(
            handle_websocket(websocket, app_state), timeout=5.0
        )

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
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-slow-bcast", app_state)
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
            sockets.sessions.pop("ws-slow-bcast", None)

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
        app_state = _make_app_state()
        registry = app_state.container_registry
        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=SlowClientError("full"))
        session = AsyncMock()

        async def fake_output():
            yield b"data"

        session.output = fake_output
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        with patch.object(registry, "record_activity"):
            await conn.forward_exec_output(session)
        # Should not raise


class TestMentionsAgent:
    async def test_detects_mention(self, agent_user):
        from klangk_backend.wshandler import mentions_agent

        assert await mentions_agent("@clanker hello")
        assert await mentions_agent("hey @clanker what's up")
        assert await mentions_agent("@CLANKER help")

    async def test_no_false_positives(self, agent_user):
        from klangk_backend.wshandler import mentions_agent

        assert not await mentions_agent("hello everyone")
        assert not await mentions_agent("@someone else")
        assert not await mentions_agent("clanker without at sign")
        assert not await mentions_agent("@clankery partial match")

    async def test_follows_agent_handle_rename(self, agent_user):
        """Detection must track a renamed agent handle, not a stale cache.

        Regression test for #875: the compiled mention regex was cached
        permanently and ignored handle changes, so @mentions kept using
        the old handle forever.
        """
        from klangk_backend.wshandler import mentions_agent

        # Sanity: original handle is detected before the rename.
        assert await mentions_agent("@clanker hello")

        # Rename the agent handle in the DB and drop the cached user.
        async with model.transaction() as db:
            await db.execute(
                "UPDATE users SET handle = ? WHERE id = ?",
                ("RenamedBot", model.AGENT_USER_ID),
            )
        model.clear_agent_cache()

        # New handle is now detected ...
        assert await mentions_agent("@RenamedBot hello")
        # ... and the stale old handle no longer matches.
        assert not await mentions_agent("@clanker hello")


class TestAddressesOtherUser:
    async def test_starts_with_other_mention(self, agent_user):
        from klangk_backend.wshandler import addresses_other_user

        assert await addresses_other_user("@bob hello")
        assert await addresses_other_user("@alice@test.com what do you think?")

    async def test_starts_with_agent_mention(self, agent_user):
        from klangk_backend.wshandler import addresses_other_user

        assert not await addresses_other_user("@clanker hello")
        assert not await addresses_other_user("@CLANKER help")

    async def test_no_mention(self, agent_user):
        from klangk_backend.wshandler import addresses_other_user

        assert not await addresses_other_user("hello everyone")

    async def test_mention_in_middle(self, agent_user):
        from klangk_backend.wshandler import addresses_other_user

        assert not await addresses_other_user("I think @bob is right")


class TestChatFollowUp:
    @pytest.fixture(autouse=True)
    def _allow_chat(self):
        """Default the chat permission gate to allow (see TestChatSend)."""
        with patch.object(
            Connection, "_has_perm", new=AsyncMock(return_value=True)
        ):
            yield

    async def test_same_user_no_interjection(
        self, workspace, user, agent_user
    ):
        """Same user's follow-up routes without timer."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import agent_conversations

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.send_prompt = AsyncMock(return_value="reply")

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock, "cid")
        try:
            agent_conversations[workspace["id"]] = {
                "user_id": user["id"],
                "time": time.monotonic(),
                "interjected": False,
            }
            with patch.object(
                app_state.agents,
                "get_session",
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
            agent_conversations.pop(workspace["id"], None)
            await session.remove_subscriber(sock)

    async def test_interjection_within_window(
        self, workspace, user, agent_user
    ):
        """After interjection, follow-up within 30s still routes."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import agent_conversations

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.send_prompt = AsyncMock(return_value="reply")

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock, "cid")
        try:
            agent_conversations[workspace["id"]] = {
                "user_id": user["id"],
                "time": time.monotonic(),
                "interjected": True,
            }
            with patch.object(
                app_state.agents,
                "get_session",
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
            agent_conversations.pop(workspace["id"], None)
            await session.remove_subscriber(sock)

    async def test_interjection_expired(self, workspace, user, agent_user):
        """After interjection + 30s, follow-up does NOT route."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import agent_conversations

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock, "cid")
        try:
            agent_conversations[workspace["id"]] = {
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
            agent_conversations.pop(workspace["id"], None)
            await session.remove_subscriber(sock)

    async def test_different_user_marks_interjection(
        self, workspace, user, agent_user
    ):
        """A different user's message marks interjection."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import agent_conversations

        sock = _mock_sock()
        other_user = {"id": "other-uid", "email": "other@test.com"}
        conn = _base_conn(user=other_user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock, "cid")
        try:
            agent_conversations[workspace["id"]] = {
                "user_id": user["id"],
                "time": time.monotonic(),
                "interjected": False,
            }
            await conn.handle_chat_send({"message": "hey everyone"})
            await asyncio.sleep(0.1)
            assert agent_conversations[workspace["id"]]["interjected"]
        finally:
            agent_conversations.pop(workspace["id"], None)
            await session.remove_subscriber(sock)

    async def test_addressed_to_other_breaks(
        self, workspace, user, agent_user
    ):
        """Message starting with @someone else breaks conversation."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import agent_conversations

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock, "cid")
        try:
            agent_conversations[workspace["id"]] = {
                "user_id": user["id"],
                "time": time.monotonic(),
                "interjected": False,
            }
            await conn.handle_chat_send({"message": "@bob hey"})
            await asyncio.sleep(0.1)
            assert workspace["id"] not in agent_conversations
        finally:
            agent_conversations.pop(workspace["id"], None)
            await session.remove_subscriber(sock)


class TestChatSend:
    @pytest.fixture(autouse=True)
    def _allow_chat(self):
        """Default the chat permission gate to allow.

        These tests model chat broadcast/routing for a user who has the
        chat permission (the owner). The ``workspace`` fixture inserts a
        row without ACL seeding, so the real ``_has_perm`` would
        default-deny and short-circuit before the logic under test. The
        no-permission (spectator) case is covered explicitly by
        test_chat_send_requires_chat_perm (#1136), which overrides this
        with an instance-level patch returning False.
        """
        with patch.object(
            Connection, "_has_perm", new=AsyncMock(return_value=True)
        ):
            yield

    async def test_chat_send_broadcasts(self, workspace, user, agent_user):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock1 = _mock_sock()
        sock2 = _mock_sock()
        conn = _base_conn(user=user, ws=sock1, app_state=app_state)
        conn.workspace_id = workspace["id"]

        session = sockets.get_or_create_session(workspace["id"], app_state)
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
            sockets.sessions.pop(workspace["id"], None)

    async def test_chat_send_with_mention(self, workspace, user, agent_user):
        """Broadcast includes mention user IDs when @email is used."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]

        session = sockets.get_or_create_session(workspace["id"], app_state)
        session.subscribers.add(sock)
        try:
            await conn.handle_chat_send({"message": f"hey @{user['email']}"})
            sent = sock.send_json.call_args[0][0]
            assert sent["mentions"] == [user["id"]]
        finally:
            sockets.sessions.pop(workspace["id"], None)

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

    async def test_chat_send_requires_chat_perm(self, workspace, user):
        """A user without the chat permission (e.g. a spectator) must not be
        able to send chat. Chat is a privileged channel: @mentions (and
        follow-ups) route to the agent, which can make workspace changes.
        Reject before the message is persisted, broadcast, or routed (#1136)."""
        from klangk_backend import model

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        with (
            patch.object(conn, "_has_perm", new=AsyncMock(return_value=False)),
            patch.object(
                model, "add_chat_message", new_callable=AsyncMock
            ) as mock_add,
        ):
            await conn.handle_chat_send(
                {"message": "@clanker delete everything"}
            )
        # Rejected with a permission error...
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict)
            and m.get("type") == "error"
            and "chat permission" in m.get("message", "")
            for m in sent
        )
        # ...and nothing was persisted or routed.
        mock_add.assert_not_called()

    async def test_chat_send_agent_mention(self, workspace, user, agent_user):
        """@clanker sends thinking event + agent response."""
        app_state = _make_app_state()
        sockets = app_state.sockets

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
        mock_session.output = _empty_async_generator
        mock_session.send_prompt = AsyncMock(return_value="The time is now.")

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock, "cid")
        try:
            with patch.object(
                app_state.agents,
                "get_session",
                return_value=mock_session,
            ):
                await conn.handle_chat_send(
                    {"message": "@clanker what time is it?"}
                )
                await _await_agent_run(workspace["id"])
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
        """@clanker with no prompt uses default greeting."""
        app_state = _make_app_state()
        sockets = app_state.sockets

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.send_prompt = AsyncMock(return_value="Hi there!")

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock, "cid")
        try:
            with patch.object(
                app_state.agents,
                "get_session",
                return_value=mock_session,
            ):
                await conn.handle_chat_send({"message": "@clanker"})
                await _await_agent_run(workspace["id"])
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
        app_state = _make_app_state()
        sockets = app_state.sockets

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock, "cid")
        try:
            with patch.object(
                app_state.agents,
                "get_session",
                side_effect=RuntimeError("boom"),
            ):
                await conn.handle_chat_send({"message": "@clanker help"})
                await _await_agent_run(workspace["id"])
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
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.agent import AgentProcessDied

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.send_prompt = AsyncMock(
            side_effect=AgentProcessDied("exited")
        )

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock, "cid")
        try:
            with patch.object(
                app_state.agents,
                "get_session",
                return_value=mock_session,
            ):
                await conn.handle_chat_send({"message": "@clanker hello"})
                await _await_agent_run(workspace["id"])
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
        """Messages without @clanker don't trigger agent response."""
        app_state = _make_app_state()
        sockets = app_state.sockets

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        session = sockets.get_or_create_session(workspace["id"], app_state)
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

    async def test_chat_send_mention_supersedes_prior_run(
        self, workspace, user, agent_user
    ):
        """A second rapid @mention cancels the prior in-flight run, and abort
        then reaches the latest run instead of an orphaned task."""
        app_state = _make_app_state()
        sockets = app_state.sockets

        sock1 = _mock_sock()
        sock2 = _mock_sock()
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)
        conn1.workspace_id = workspace["id"]
        conn1.container_id = "cid"
        user2 = {
            "id": "uid-second",
            "email": "second@test.com",
            "handle": "second",
        }
        conn2 = _base_conn(user=user2, ws=sock2, app_state=app_state)
        conn2.workspace_id = workspace["id"]
        conn2.container_id = "cid"

        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock1, "cid")
        await session.add_subscriber(sock2, "cid")

        async def slow_mention(
            sockets, workspace_id, container_id, text, **kwargs
        ):
            await asyncio.sleep(999)

        try:
            with patch(
                "klangk_backend.wshandler.connection.handle_agent_mention",
                new=slow_mention,
            ):
                await conn1.handle_chat_send({"message": "@clanker first"})
                task1 = wshandler.agent_tasks[workspace["id"]]
                # A second mention from a different user must supersede
                # task1 rather than orphaning it.
                await conn2.handle_chat_send({"message": "@clanker second"})
                task2 = wshandler.agent_tasks[workspace["id"]]
                assert task2 is not task1
                await asyncio.sleep(0)
                assert task1.cancelled()
                # abort reaches the now-current run.
                await conn2.handle_chat_agent_abort()
                assert workspace["id"] not in wshandler.agent_tasks
                await asyncio.sleep(0)
                assert task2.cancelled()
        finally:
            await session.remove_subscriber(sock1)
            await session.remove_subscriber(sock2)
            for t in list(wshandler.agent_tasks.values()):
                if not t.done():
                    t.cancel()
            wshandler.agent_tasks.clear()
            wshandler.agent_conversations.pop(workspace["id"], None)

    async def test_chat_history_on_connect(self, user, agent_user, app_state):
        app_state = _make_app_state()
        registry = app_state.container_registry
        from klangk_backend import model

        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "chat-ws"
        )
        await model.add_chat_message(
            workspace["id"], "uid-other", "someone@test.com", "old message"
        )

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)

        async def fake_start(wid, ws_obj):
            conn.container_id = "cid"

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(
                registry,
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

    async def test_chat_load_more(self, workspace, user, app_state):
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

    async def test_chat_load_more_no_before_id(
        self, workspace, user, app_state
    ):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = workspace["id"]
        await conn.handle_chat_load_more({})
        sock.send_json.assert_not_called()


class TestPresence:
    async def test_presence_list_on_connect(self, user, agent_user, app_state):
        """Joining user receives presence_list with current users."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "pres-ws"
        )

        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        sockets.connections[sock] = conn

        async def fake_start(wid, ws_obj):
            conn.container_id = "cid"
            session = sockets.get_or_create_session(wid, app_state)
            await session.add_subscriber(sock, "cid")

        try:
            with (
                patch.object(
                    Connection,
                    "start_workspace_container",
                    side_effect=fake_start,
                ),
                patch.object(
                    registry,
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
            sockets.sessions.pop(workspace["id"], None)
            sockets.connections.pop(sock, None)

    async def test_presence_join_broadcast(self, user, agent_user):
        """Existing subscribers receive presence_join when someone connects."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        from klangk_backend import model

        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "pres-join-ws"
        )

        # First user connects
        sock1 = _mock_sock()
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)

        session = sockets.get_or_create_session(workspace["id"], app_state)
        session.subscribers.add(sock1)
        sockets.connections[sock1] = conn1
        conn1.workspace_id = workspace["id"]

        # Second user connects
        other = await model.create_user(
            "other@test.com", "hash", verified=True
        )
        await model.add_acl_entry(
            f"/workspaces/{workspace['id']}",
            100,
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
            app_state=app_state,
        )
        sockets.connections[sock2] = conn2

        async def fake_start(wid, ws_obj):
            conn2.container_id = "cid"
            session = sockets.get_or_create_session(wid, app_state)
            await session.add_subscriber(sock2, "cid")

        try:
            with (
                patch.object(
                    Connection,
                    "start_workspace_container",
                    side_effect=fake_start,
                ),
                patch.object(
                    registry,
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
            sockets.sessions.pop(workspace["id"], None)
            sockets.connections.pop(sock1, None)
            sockets.connections.pop(sock2, None)

    async def test_presence_leave_broadcast(self, user, app_state):
        """Remaining subscribers receive presence_leave after debounce."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend import model

        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "pres-lv-ws"
        )

        sock1 = _mock_sock()
        sock2 = _mock_sock()
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)
        other = await model.create_user("lv@test.com", "hash", verified=True)
        conn2 = _base_conn(
            user={"id": other["id"], "email": "lv@test.com"},
            ws=sock2,
            app_state=app_state,
        )
        conn1.workspace_id = workspace["id"]
        conn2.workspace_id = workspace["id"]

        session = WorkspaceSession(workspace["id"], app_state)
        session.subscribers.add(sock1)
        session.subscribers.add(sock2)
        sockets.sessions[workspace["id"]] = session
        sockets.connections[sock1] = conn1
        sockets.connections[sock2] = conn2

        saved_delay = sockets.PRESENCE_LEAVE_DELAY
        try:
            # Use a tiny delay so the test completes quickly
            sockets.PRESENCE_LEAVE_DELAY = 0.05

            # conn2 disconnects — leave is debounced, not immediate
            await conn2.cleanup()
            calls1 = [c[0][0] for c in sock1.send_json.call_args_list]
            leaves = [c for c in calls1 if c.get("type") == "presence_leave"]
            assert len(leaves) == 0  # not yet

            # Wait for the debounce to fire (generous margin for slow CI)
            await asyncio.sleep(0.5)

            calls1 = [c[0][0] for c in sock1.send_json.call_args_list]
            leaves = [c for c in calls1 if c.get("type") == "presence_leave"]
            assert len(leaves) == 1
            assert leaves[0]["user_id"] == other["id"]
        finally:
            sockets.PRESENCE_LEAVE_DELAY = saved_delay
            sockets._pending_leaves.clear()
            sockets.sessions.pop(workspace["id"], None)
            sockets.connections.pop(sock1, None)
            sockets.connections.pop(sock2, None)

    async def test_presence_leave_suppressed_on_reconnect(self, user):
        """Reconnecting within debounce window suppresses leave and join."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        registry = app_state.container_registry
        from klangk_backend import model

        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "pres-debounce-ws"
        )

        sock1 = _mock_sock()  # observer
        other = await model.create_user("flap@test.com", "hash", verified=True)
        await model.add_acl_entry(
            f"/workspaces/{workspace['id']}",
            100,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=other["id"],
        )
        other_user = {
            "id": other["id"],
            "email": "flap@test.com",
            "handle": other.get("handle", ""),
        }
        sock2 = _mock_sock()  # flapping user
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)
        conn2 = _base_conn(user=other_user, ws=sock2, app_state=app_state)
        conn1.workspace_id = workspace["id"]
        conn2.workspace_id = workspace["id"]

        session = WorkspaceSession(workspace["id"], app_state)
        session.subscribers.add(sock1)
        session.subscribers.add(sock2)
        sockets.sessions[workspace["id"]] = session
        sockets.connections[sock1] = conn1
        sockets.connections[sock2] = conn2

        saved_delay = sockets.PRESENCE_LEAVE_DELAY
        try:
            sockets.PRESENCE_LEAVE_DELAY = 5.0  # long enough to cancel

            # conn2 disconnects — pending leave scheduled
            await conn2.cleanup()
            key = (workspace["id"], other["id"])
            assert key in sockets._pending_leaves

            # conn2 reconnects — cancel pending leave
            sock3 = _mock_sock()
            conn3 = _base_conn(user=other_user, ws=sock3, app_state=app_state)
            sockets.connections[sock3] = conn3

            async def fake_start(wid, ws_obj):
                conn3.container_id = "cid"
                s = sockets.get_or_create_session(wid, app_state)
                await s.add_subscriber(sock3, "cid")

            with (
                patch.object(
                    Connection,
                    "start_workspace_container",
                    side_effect=fake_start,
                ),
                patch.object(
                    registry,
                    "get_workspace_ports",
                    return_value=[],
                ),
            ):
                await conn3.handle_workspace_connect(
                    {"workspaceId": workspace["id"]}
                )

            # Pending leave should be cancelled
            assert key not in sockets._pending_leaves

            # Observer should NOT have received presence_leave or
            # presence_join (the user never visibly left)
            calls1 = [c[0][0] for c in sock1.send_json.call_args_list]
            leaves = [c for c in calls1 if c.get("type") == "presence_leave"]
            joins = [c for c in calls1 if c.get("type") == "presence_join"]
            assert len(leaves) == 0
            assert len(joins) == 0

            # No system chat messages about join/leave
            chats = [
                c
                for c in calls1
                if c.get("type") == "chat_message"
                and c.get("message_type") == model.MSG_SYSTEM
            ]
            assert len(chats) == 0
        finally:
            sockets.PRESENCE_LEAVE_DELAY = saved_delay
            sockets._pending_leaves.clear()
            sockets.sessions.pop(workspace["id"], None)
            sockets.connections.pop(sock1, None)
            sockets.connections.pop(sock2, None)
            sockets.connections.pop(sock3, None)

    async def test_presence_leave_multi_tab(self, user, app_state):
        """No presence_leave if user has another connection in workspace."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend import model

        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "pres-mt-ws"
        )

        sock1 = _mock_sock()
        sock2 = _mock_sock()
        sock3 = _mock_sock()
        # sock1 and sock2 are same user, sock3 is another user
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)
        conn2 = _base_conn(user=user, ws=sock2, app_state=app_state)
        other = await model.create_user("mt@test.com", "hash", verified=True)
        conn3 = _base_conn(
            user={"id": other["id"], "email": "mt@test.com"},
            ws=sock3,
            app_state=app_state,
        )
        conn1.workspace_id = workspace["id"]
        conn2.workspace_id = workspace["id"]
        conn3.workspace_id = workspace["id"]

        session = WorkspaceSession(workspace["id"], app_state)
        session.subscribers.add(sock1)
        session.subscribers.add(sock2)
        session.subscribers.add(sock3)
        sockets.sessions[workspace["id"]] = session
        sockets.connections[sock1] = conn1
        sockets.connections[sock2] = conn2
        sockets.connections[sock3] = conn3

        try:
            # sock1 disconnects, but sock2 (same user) remains
            await conn1.cleanup()

            calls3 = [c[0][0] for c in sock3.send_json.call_args_list]
            leaves = [c for c in calls3 if c.get("type") == "presence_leave"]
            assert len(leaves) == 0
        finally:
            sockets.sessions.pop(workspace["id"], None)
            sockets.connections.pop(sock1, None)
            sockets.connections.pop(sock2, None)
            sockets.connections.pop(sock3, None)


class TestRefreshUserHandle:
    async def test_refresh_updates_connections_and_broadcasts(
        self, user, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "handle-refresh-ws"
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]

        session = WorkspaceSession(workspace["id"], app_state)
        session.subscribers.add(sock)
        sockets.sessions[workspace["id"]] = session
        sockets.connections[sock] = conn

        try:
            await wshandler.refresh_user_handle(
                sockets, user["id"], "newhandle"
            )
            assert conn.user["handle"] == "newhandle"
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            plist = [c for c in calls if c.get("type") == "presence_list"]
            assert len(plist) == 1
            handles = [u["user_handle"] for u in plist[0]["users"]]
            assert "newhandle" in handles
            # System chat message broadcast
            chats = [c for c in calls if c.get("type") == "chat_message"]
            assert len(chats) == 1
            assert "is now known as newhandle" in chats[0]["message"]
        finally:
            sockets.sessions.pop(workspace["id"], None)
            sockets.connections.pop(sock, None)

    async def test_refresh_no_connections(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        await wshandler.refresh_user_handle(sockets, "nonexistent", "whatever")


class TestChatDelete:
    async def test_chat_delete_broadcasts_update(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend import model

        workspace = await app_state.workspaces.create_workspace(
            user["id"], "chat-del-ws"
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"

        msg = await model.add_chat_message(
            workspace["id"], user["id"], "u@test.com", "delete me"
        )

        session = WorkspaceSession(workspace["id"], app_state)
        session.subscribers.add(sock)
        sockets.sessions[workspace["id"]] = session

        await conn.handle_chat_delete({"message_id": msg["id"]})

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        updated = [c for c in calls if c.get("type") == "chat_updated"]
        assert len(updated) == 1
        assert updated[0]["message_id"] == msg["id"]
        assert updated[0]["message"] == "<message deleted by author>"

        sockets.sessions.pop(workspace["id"], None)

    async def test_chat_delete_wrong_user_ignored(self, user, app_state):
        from klangk_backend import model

        workspace = await app_state.workspaces.create_workspace(
            user["id"], "chat-del-ws2"
        )
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
    def test_default(self):
        assert _util().bridge_idle_timeout() == 30.0

    def test_env_override(self):
        assert (
            _util(
                {"KLANGK_BRIDGE_TIMEOUT_SECONDS": "45"}
            ).bridge_idle_timeout()
            == 45.0
        )

    def test_invalid_env_falls_back(self):
        assert (
            _util(
                {"KLANGK_BRIDGE_TIMEOUT_SECONDS": "nope"}
            ).bridge_idle_timeout()
            == 30.0
        )


class TestHandleBrowserChunk:
    def test_missing_id(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sockets.handle_browser_chunk({})  # no raise

    def test_unknown_id(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sockets.handle_browser_chunk({"id": "nope", "delta": "x"})

    def test_wrong_sender_ignored(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        q: asyncio.Queue = asyncio.Queue()
        expected = _mock_sock()
        imposter = _mock_sock()
        sockets.streaming_browser_requests["c-1"] = (q, expected)
        try:
            sockets.handle_browser_chunk(
                {"id": "c-1", "delta": "x"}, sender=imposter
            )
            assert q.empty()
        finally:
            sockets.streaming_browser_requests.pop("c-1", None)

    def test_success(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        q: asyncio.Queue = asyncio.Queue()
        sock = _mock_sock()
        sockets.streaming_browser_requests["c-2"] = (q, sock)
        try:
            sockets.handle_browser_chunk(
                {"id": "c-2", "delta": "hello"}, sender=sock
            )
            assert q.get_nowait() == {"type": "chunk", "delta": "hello"}
        finally:
            sockets.streaming_browser_requests.pop("c-2", None)


class TestHandleBrowserResponseStreaming:
    def test_done_enqueued(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        q: asyncio.Queue = asyncio.Queue()
        sock = _mock_sock()
        sockets.streaming_browser_requests["d-1"] = (q, sock)
        try:
            sockets.handle_browser_response(
                {"id": "d-1", "cmd": "browser_response", "text": "final"},
                sender=sock,
            )
            assert q.get_nowait() == {
                "type": "done",
                "result": {"text": "final"},
            }
        finally:
            sockets.streaming_browser_requests.pop("d-1", None)

    def test_wrong_sender_ignored(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        q: asyncio.Queue = asyncio.Queue()
        expected = _mock_sock()
        imposter = _mock_sock()
        sockets.streaming_browser_requests["d-2"] = (q, expected)
        try:
            sockets.handle_browser_response(
                {"id": "d-2", "text": "x"}, sender=imposter
            )
            assert q.empty()
        finally:
            sockets.streaming_browser_requests.pop("d-2", None)


class TestDispatchBrowserRequestStreamTo:
    async def test_streams_chunks_then_done(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-stream", app_state)
        sock = _mock_sock()
        session.subscribers.add(sock)
        session.browser_subscribers.add(sock)

        async def feed():
            await asyncio.sleep(0.05)
            for rid, (_q, _s) in list(
                sockets.streaming_browser_requests.items()
            ):
                sockets.handle_browser_chunk(
                    {"id": rid, "delta": "hel"}, sender=sock
                )
                sockets.handle_browser_chunk(
                    {"id": rid, "delta": "lo"}, sender=sock
                )
                sockets.handle_browser_response(
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
            assert not sockets.streaming_browser_requests
        finally:
            await task
            sockets.sessions.pop("ws-stream", None)

    async def test_send_failure_yields_error(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-stream-dead", app_state)
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
            assert not sockets.streaming_browser_requests
        finally:
            sockets.sessions.pop("ws-stream-dead", None)

    async def test_idle_timeout_yields_error(self):
        app_state = _make_app_state()
        sockets = app_state.sockets
        session = sockets.get_or_create_session("ws-stream-to", app_state)
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
            assert not sockets.streaming_browser_requests
        finally:
            sockets.sessions.pop("ws-stream-to", None)

    async def test_loop_dispatches_browser_chunk(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.sockets

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "browser_chunk", "id": "x", "delta": "d"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            sockets,
            "handle_browser_chunk",
            wraps=sockets.handle_browser_chunk,
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_called_once()


class TestUiReadySharedTerminals:
    async def test_ui_ready_sends_shared_terminals(
        self, user, temp_data_dir, app_state
    ):

        ws = await app_state.workspaces.create_workspace(
            user["id"], "ui-shared"
        )
        async with _conn_in_workspace(
            {"id": user["id"], "email": user["email"]},
            ws["id"],
            user_home="/home/testuser",
        ) as (sock, conn, session, app_state):
            conn.pending_status_msg = "ready"

            # Set up in-memory shared state
            session.terminal_windows[user["id"]] = [
                {"name": "dev", "index": 0, "id": "@0", "shared": True},
            ]
            await conn.handle_ui_ready()

            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(
                isinstance(m, dict) and m.get("type") == "shared_terminals"
                for m in sent
            )

    async def test_ui_ready_sends_container_ready(
        self, user, temp_data_dir, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets

        ws = await app_state.workspaces.create_workspace(
            user["id"], "ui-ready-cr"
        )
        sock = _mock_sock()
        conn = _base_conn(
            user={"id": user["id"], "email": user["email"]},
            ws=sock,
            app_state=app_state,
        )
        conn.workspace_id = ws["id"]
        conn.container_id = "cid"
        conn._user_home = "/home/testuser"
        conn.pending_status_msg = "ready"

        session = sockets.get_or_create_session(ws["id"], app_state)
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
            sockets.sessions.pop(ws["id"], None)


class TestTokenRenewal:
    async def test_renewal_creates_new_token(self, user, app_state):
        """Token renewal loop creates a new token and pushes it."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "renew-ws"
        )
        session = sockets.get_or_create_session(workspace["id"], app_state)
        session.container_id = "test-cid"

        try:
            # Drive the renewal loop on a fast clock (so the first renewal
            # fires well within the test's wait) while giving the minted
            # token a wide (1h) lifetime. Decoupling trigger timing from
            # token lifetime avoids the wall-clock race that made this
            # test flaky on slow CI runners (#1564): the renewed token
            # used to be minted with a ~0.36s lifetime (expire_hours=
            # 0.0001) and was already expired by the time the decode
            # assertion ran on a loaded runner.
            original_sleep = asyncio.sleep

            async def fast_sleep(delay, *a, **kw):
                await original_sleep(min(delay, 0.01))

            with (
                patch.object(
                    app_state.auth, "workspace_token_expire_hours", 1.0
                ),
                patch.object(
                    _mock_term,
                    "set_workspace_token",
                    new_callable=AsyncMock,
                ) as mock_set,
                patch("asyncio.sleep", side_effect=fast_sleep),
            ):
                expiry = datetime.now(timezone.utc) + timedelta(seconds=0.1)
                session.start_token_renewal(expiry)
                await original_sleep(0.5)
                session._token_renewal_task.cancel()
                try:
                    await session._token_renewal_task
                except asyncio.CancelledError:
                    pass

            assert mock_set.call_count >= 1
            cid, token = mock_set.call_args.args
            assert cid == "test-cid"
            decoded = _auth().decode_workspace_token(token)
            assert decoded == workspace["id"]
        finally:
            sockets.sessions.pop(workspace["id"], None)

    async def test_renewal_retries_on_failure(self, user, app_state):
        """Token renewal retries after failure."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "retry-ws"
        )
        session = sockets.get_or_create_session(workspace["id"], app_state)
        session.container_id = "test-cid"

        try:
            call_count = 0

            async def fail_then_succeed(cid, token):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("podman exec failed")

            # Patch asyncio.sleep in the wshandler module to skip delays
            original_sleep = asyncio.sleep

            async def fast_sleep(delay, *a, **kw):
                await original_sleep(min(delay, 0.05))

            with (
                patch.object(
                    app_state.auth, "workspace_token_expire_hours", 0.0001
                ),
                patch.object(
                    _mock_term,
                    "set_workspace_token",
                    side_effect=fail_then_succeed,
                ),
                patch("asyncio.sleep", side_effect=fast_sleep),
            ):
                expiry = datetime.now(timezone.utc) + timedelta(seconds=0.1)
                session.start_token_renewal(expiry)
                await original_sleep(0.5)
                session._token_renewal_task.cancel()
                try:
                    await session._token_renewal_task
                except asyncio.CancelledError:
                    pass

            # First call fails, retry should succeed
            assert call_count >= 2
        finally:
            sockets.sessions.pop(workspace["id"], None)

    async def test_reset_cancels_token_renewal_task(self, user, app_state):
        """reset() cancels the token renewal task (issue #871).

        Without cancellation the renewal loop keeps running after a
        container is killed and the session is reset, leaking a task
        that renews tokens for a dead container forever.
        """
        app_state = _make_app_state()
        sockets = app_state.sockets
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "leak-ws"
        )
        session = sockets.get_or_create_session(workspace["id"], app_state)
        session.container_id = "test-cid"

        try:
            with (
                patch.object(
                    app_state.auth, "workspace_token_expire_hours", 0.0001
                ),
                patch.object(
                    _mock_term,
                    "set_workspace_token",
                    new_callable=AsyncMock,
                ) as mock_set,
            ):
                expiry = datetime.now(timezone.utc) + timedelta(seconds=0.1)
                session.start_token_renewal(expiry)
                task = session._token_renewal_task
                assert task is not None and not task.done()

                await session.reset()

                assert task.done()
                assert session._token_renewal_task is None
                assert session.workspace_token_expiry is None

            # Renewal must never fire again after reset, even if we wait.
            calls_before = mock_set.call_count
            await asyncio.sleep(0.3)
            assert mock_set.call_count == calls_before
        finally:
            sockets.sessions.pop(workspace["id"], None)

    async def test_concurrent_add_subscriber_no_duplicate_renewal(
        self, user, app_state
    ):
        """Two concurrent add_subscriber calls must not create duplicate
        renewal tasks (#1299)."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "race-ws"
        )
        session = sockets.get_or_create_session(workspace["id"], app_state)

        try:
            with patch.object(
                _mock_term,
                "set_workspace_token",
                new_callable=AsyncMock,
            ):
                expiry = datetime.now(timezone.utc) + timedelta(hours=1)
                sock1 = _mock_sock()
                sock2 = _mock_sock()

                await asyncio.gather(
                    session.add_subscriber(sock1, "cid", token_expiry=expiry),
                    session.add_subscriber(sock2, "cid", token_expiry=expiry),
                )

                assert session.workspace_token_expiry is not None
                assert session._token_renewal_task is not None
                # Only one task should be created, not two.
                task = session._token_renewal_task
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            await session.reset()
            sockets.sessions.pop(workspace["id"], None)


class TestSSHAgentDispatch:
    async def test_dispatch_ssh_agent_start(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "ssh_agent_start"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_ssh_agent_start", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_ssh_agent_data(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "ssh_agent_data", "data": "AA=="}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_ssh_agent_data", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_ssh_agent_stop(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "ssh_agent_stop"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_ssh_agent_stop", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_share_window(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "share_window", "window_id": "w1"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_share_window", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_unshare_window(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "unshare_window", "window_id": "w1"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_unshare_window", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_create_shared_terminal(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "create_shared_terminal"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_create_shared_terminal", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_join_shared_terminal(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "cmd": "join_shared_terminal",
                        "user_id": "u1",
                        "window_id": "w1",
                    }
                ),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_join_shared_terminal", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_delete_shared_terminal(self, user):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "delete_shared_terminal"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection,
            "handle_delete_shared_terminal",
            new_callable=AsyncMock,
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_list_shared_terminals(self, user, app_state):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "list_shared_terminals"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection,
            "handle_list_shared_terminals",
            new_callable=AsyncMock,
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()

    async def test_dispatch_chat_agent_abort(self, user, app_state):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "chat_agent_abort"}),
                WebSocketDisconnect(),
            ]
        )
        with patch.object(
            Connection, "handle_chat_agent_abort", new_callable=AsyncMock
        ) as mock:
            await handle_websocket(websocket, app_state)
        mock.assert_awaited_once()


class TestStartAgentIfNeeded:
    async def test_starts_agent_and_broadcasts_presence(
        self, user, db, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "agent-ws"
        )
        conn.workspace_id = workspace["id"]

        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock, "cid")

        mock_agent_session = AsyncMock()
        mock_agent_session.ensure_started = AsyncMock()

        try:
            with patch.object(
                app_state.agents,
                "get_session",
                return_value=mock_agent_session,
            ):
                await conn._start_agent_if_needed()
            mock_agent_session.ensure_started.assert_awaited_once()
        finally:
            await session.remove_subscriber(sock)
            sockets.sessions.pop(workspace["id"], None)

    async def test_start_agent_logs_on_failure(self, user, db, app_state):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-fail"

        with patch.object(
            conn.app_state.agents,
            "get_session",
            side_effect=RuntimeError("nope"),
        ):
            # Should not raise
            await conn._start_agent_if_needed()


class TestHandleChatAgentAbort:
    async def test_cancels_running_task(self, user, db, app_state):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "abort-ws"
        )
        conn.workspace_id = workspace["id"]

        async def slow():
            await asyncio.sleep(999)

        task = asyncio.create_task(slow())
        wshandler.agent_tasks[workspace["id"]] = task
        try:
            await conn.handle_chat_agent_abort()
            # Let cancellation propagate
            await asyncio.sleep(0)
            assert workspace["id"] not in wshandler.agent_tasks
            assert task.cancelled() or task.done()
        finally:
            wshandler.agent_tasks.pop(workspace["id"], None)
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def test_abort_no_workspace(self, user, db, app_state):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = None
        await conn.handle_chat_agent_abort()

    async def test_abort_no_task(self, user, db, app_state):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "abort-none"
        )
        conn.workspace_id = workspace["id"]
        wshandler.agent_tasks.pop(workspace["id"], None)
        await conn.handle_chat_agent_abort()

    async def test_drop_if_current_removes_own_entry(self):
        """A finishing run drops the entry only when it is the current task."""
        ws_id = "ws-drop-self"
        wshandler.agent_tasks[ws_id] = asyncio.current_task()
        try:
            wshandler.drop_agent_task_if_current(ws_id)
            assert ws_id not in wshandler.agent_tasks
        finally:
            wshandler.agent_tasks.pop(ws_id, None)

    async def test_drop_if_current_keeps_other_entry(self):
        """A superseded (older) run must not pop a newer task's entry."""
        ws_id = "ws-drop-other"

        async def other():
            await asyncio.sleep(999)

        other_task = asyncio.create_task(other())
        wshandler.agent_tasks[ws_id] = other_task
        try:
            wshandler.drop_agent_task_if_current(ws_id)
            # Entry belongs to a different task; left intact.
            assert wshandler.agent_tasks[ws_id] is other_task
        finally:
            other_task.cancel()
            try:
                await other_task
            except asyncio.CancelledError:
                pass
            wshandler.agent_tasks.pop(ws_id, None)


class TestPresenceIncludesAgent:
    async def test_agent_in_presence_when_running(
        self, user, agent_user, app_state
    ):
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "pres-ws"
        )
        async with _conn_in_workspace(user, workspace["id"]) as (
            sock,
            conn,
            session,
            app_state,
        ):
            with patch.object(
                app_state.agents,
                "is_running",
                side_effect=lambda ws_id: ws_id == workspace["id"],
            ):
                users = await get_presence_list(
                    workspace["id"], app_state.sockets
                )
            ids = [u["user_id"] for u in users]
            assert model.AGENT_USER_ID in ids

    async def test_agent_not_in_presence_when_running_in_other_workspace(
        self, user, agent_user, app_state
    ):
        """Agent running in a different workspace must not appear in this
        workspace's presence list (regression for #870)."""
        workspace = await app_state.workspaces.create_workspace(
            user["id"], "pres-ws"
        )
        async with _conn_in_workspace(user, workspace["id"]) as (
            sock,
            conn,
            session,
            app_state,
        ):
            with patch.object(
                app_state.agents,
                "is_running",
                side_effect=lambda ws_id: ws_id == "other-workspace",
            ):
                users = await get_presence_list(
                    workspace["id"], app_state.sockets
                )
            ids = [u["user_id"] for u in users]
            assert model.AGENT_USER_ID not in ids


class TestAgentMentionOtherMsgsContext:
    async def test_other_user_messages_prepended_to_prompt(
        self, user, agent_user
    ):
        """When other users have spoken since the agent's last response,
        their messages are prepended to the prompt."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import handle_agent_mention

        workspace = await model.create_workspace(user["id"], "ctx-ws")
        ws_id = workspace["id"]

        # Create a user2 whose message should appear in context
        user2 = await model.create_user(
            "user2@example.com", "hash", verified=True
        )
        agent_email = (await model.get_agent_user())["email"]

        # Simulate conversation: agent response, then user2 message,
        # then user1 mentions agent
        await model.add_chat_message(
            ws_id,
            model.AGENT_USER_ID,
            agent_email,
            "I'm here",
            message_type=model.MSG_AGENT,
        )
        await model.add_chat_message(
            ws_id,
            user2["id"],
            user2["email"],
            "interesting point",
            message_type=model.MSG_USER,
        )

        captured_prompt = []
        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator

        async def capture_prompt(prompt):
            captured_prompt.append(prompt)
            return "response"

        mock_session.send_prompt = capture_prompt

        sock = _mock_sock()
        session = sockets.get_or_create_session(ws_id, app_state)
        await session.add_subscriber(sock, "cid")

        try:
            with patch.object(
                app_state.agents,
                "get_session",
                return_value=mock_session,
            ):
                await handle_agent_mention(
                    sockets, ws_id, "cid", "@clanker what?"
                )

            assert len(captured_prompt) == 1
            assert "user2@example.com" in captured_prompt[0]
            assert "interesting point" in captured_prompt[0]
        finally:
            await session.remove_subscriber(sock)
            sockets.sessions.pop(ws_id, None)
            wshandler.agent_tasks.pop(ws_id, None)


class TestAgentMentionAskerIdentity:
    """The asking user's identity is injected so the agent can resolve "my"."""

    def test_header_includes_id_handle_home(self):
        from klangk_backend.wshandler.agent_mention import (
            asker_context_header,
        )

        header = asker_context_header("uid-123", "alice", "/home/alice")

        assert "id uid-123" in header
        assert "handle alice" in header
        assert "home /home/alice" in header
        # Points the agent at the asker's own tmux session.
        assert 'tmux session "uid-123"' in header
        assert '"my"/"my history"' in header

    def test_header_none_without_user_id(self):
        from klangk_backend.wshandler.agent_mention import (
            asker_context_header,
        )

        assert asker_context_header(None, "alice", "/home/alice") is None

    async def test_identity_prepended_to_prompt(self, user, agent_user):
        """An @mention from a user injects that user's identity header."""
        app_state = _make_app_state()
        sockets = app_state.sockets
        from klangk_backend.wshandler import handle_agent_mention

        workspace = await model.create_workspace(user["id"], "id-ws")
        ws_id = workspace["id"]

        captured_prompt = []
        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator

        async def capture_prompt(prompt):
            captured_prompt.append(prompt)
            return "response"

        mock_session.send_prompt = capture_prompt

        sock = _mock_sock()
        session = sockets.get_or_create_session(ws_id, app_state)
        await session.add_subscriber(sock, "cid")

        try:
            with patch.object(
                app_state.agents,
                "get_session",
                return_value=mock_session,
            ):
                await handle_agent_mention(
                    sockets,
                    ws_id,
                    "cid",
                    "@clanker restart my service",
                    user_id=user["id"],
                    user_handle=user.get("handle") or "somebody",
                    user_home="/home/somebody",
                )

            assert len(captured_prompt) == 1
            prompt = captured_prompt[0]
            # The @mention is stripped and the asker header is present.
            assert "@clanker" not in prompt
            assert "restart my service" in prompt
            assert user["id"] in prompt
            assert "/home/somebody" in prompt
        finally:
            await session.remove_subscriber(sock)
            sockets.sessions.pop(ws_id, None)
            wshandler.agent_tasks.pop(ws_id, None)


class TestTokenRenewalFailureLogged:
    async def test_exception_during_renewal_is_logged(self, user):
        """The except Exception branch in _token_renewal_loop."""
        app_state = _make_app_state()
        ws_session = WorkspaceSession("ws-tok", app_state)
        ws_session.container_id = "test-cid"
        ws_session.workspace_token_expiry = datetime.now(
            timezone.utc
        ) + timedelta(seconds=0.05)
        with (
            patch.object(
                app_state.auth, "workspace_token_expire_hours", 0.0001
            ),
            patch.object(
                _mock_term,
                "set_workspace_token",
                side_effect=RuntimeError("boom"),
            ),
        ):
            task = asyncio.create_task(ws_session._token_renewal_loop())
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
