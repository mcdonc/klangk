"""Tests for wshandler: WebSocket command dispatch, event forwarding, terminal, cleanup."""

import asyncio
import base64
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import types
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

from fastapi import WebSocketDisconnect

from klangk import (
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
from klangk.exceptions import (
    ContainerGoneError,
    NodeDrainingError,
    TerminalError,
)
from klangk.podman import PodmanError
from _helpers import make_settings
from klangk.wshandler import (
    support as _ws_support,
    controllers as _ws_controllers,
)
from klangk.wshandler import (
    Connection,
    ExecController,
    SafeWebSocket,
    SharedTerminalController,
    SlowClientError,
    SshAgentForwarder,
    TerminalController,
    WebSocketState,
    WorkspaceSession,
    broadcast_event,
    disconnect_all_websockets,
    send_error,
    handle_websocket,
    reset_workspace_state,
    log_ws_msg,
    SEND_QUEUE_SIZE,
)


def _util(env=None):
    """Build a Util instance from explicit env."""
    settings = make_settings(env)
    return util_mod.Util(
        types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    )


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
_mock_term.has_tmux_session = AsyncMock(return_value=False)
_mock_term.service_cmd_window_exists = AsyncMock(return_value=False)


def _make_app_state(registry=None, sockets=None):
    """Build a minimal app_state for tests."""
    from klangk.container import ContainerRegistry

    settings = make_settings({})
    # Two-phase: shell first so owned instances (sockets, registry, etc.)
    # can take app_state at construction (#1426).
    app_state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=settings)
    )
    if sockets is None:
        sockets = WebSocketState(app_state)
    app_state.state.sockets = sockets
    if registry is None:
        registry = ContainerRegistry(app_state)
    app_state.state.container_registry = registry
    app_state.state.podman = _mock_pod
    # #1480: shared mock Terminal wired onto app_state. Tests patch
    # its methods via patch.object(_mock_term, ...).
    app_state.state.terminal = _mock_term
    app_state.state.workspaces = ws_mod.Workspaces(app_state)
    app_state.state.files = files_mod.Files(app_state)
    app_state.state.email = emailsvc_mod.EmailService(app_state)
    app_state.state.util = util_mod.Util(app_state)

    app_state.state.auth = auth_mod.Auth(app_state)
    # #1572: ContainerRegistry reaches app_state.state.model.ports; Auth reaches
    # app_state.state.model.{tokens,login_attempts}.
    from _helpers import wire_db_and_model

    wire_db_and_model(app_state)
    return app_state


def _auth():
    """A standalone Auth for token forging (same default secret as the
    app fixture, so tokens round-trip through app.state.auth.decode_*)."""
    from _helpers import wire_db_and_model

    state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=make_settings({}))
    )
    wire_db_and_model(state)
    return auth_mod.Auth(state)


async def _grant_monitor(app_state, user_id: str, workspace_id: str) -> None:
    """Seed a member ALLOW ``monitor`` ACE so a connection passes the
    #1714/#2783 scope.

    The status fan-outs ACL-check each recipient for ``monitor`` on
    ``/workspaces/{id}``; the per-test DB starts with no ACEs (default
    deny), so tests that assert delivery must grant membership first.
    """
    await app_state.state.model.init_db()
    # acl_entries.user_id has an FK to users(id): plant the principal row.
    async with app_state.state.db.transaction() as tx:
        await tx.execute(
            "INSERT OR IGNORE INTO users (id, email, verified)"
            " VALUES (?, ?, 1)",
            (user_id, f"{user_id}@test.example"),
        )
    resource = f"/workspaces/{workspace_id}"
    entries = await app_state.state.model.acl.get_acl_entries(resource)
    position = max((e["position"] for e in entries), default=-1) + 1
    await app_state.state.model.acl.add_acl_entry(
        resource,
        position,
        model.ACTION_ALLOW,
        "monitor-workspace",
        model.PRINCIPAL_USER,
        user_id=user_id,
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


def _base_conn(user=None, ws=None, app_state=None, perms=()):
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
    conn = Connection(ws, user, app_state)
    if perms:
        granted = frozenset(perms)

        async def _perm(perm):
            return perm in granted

        conn.has_perm = _perm  # type: ignore[method-assign]
    return conn


@asynccontextmanager
async def _conn_in_workspace(
    user,
    workspace_id: str = "ws-1",
    *,
    container_id: str = "cid",
    user_home: str | None = None,
    app_state=None,
    perms=(),
):
    """Yield ``(sock, conn, session, app_state)`` registered in workspace state.

    Creates a mock socket and Connection, registers it as a subscriber
    of a fresh WorkspaceSession, and tears the registration down on exit.
    The yielded ``session`` may be mutated (e.g. ``terminal_windows``)
    by the caller before use. *perms* overrides ``has_perm`` (the
    synthetic workspace has no ACL rows, so real checks deny).
    """
    if app_state is None:
        app_state = _make_app_state()
    sockets = app_state.state.sockets
    sock = _mock_sock()
    conn = _base_conn(user=user, ws=sock, app_state=app_state, perms=perms)
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
    groups atomically (see app_state.state.model.workspaces.create_workspace_with_acl, #128), so this
    is a thin alias kept for call-site readability.
    """
    return await app_state.state.workspaces.create_workspace(
        user_id, name, **kwargs
    )


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
        assert sw.stopped is False  # live socket, not mid-teardown (#2623)
        await sw.stop_sender()
        assert sw.stopped is True
        with pytest.raises(SlowClientError):
            sw.send_json({"type": "late"})


# --- disconnect_all (SIGHUP runtime restart) ---


class TestDisconnectAll:
    async def test_clears_connections_sessions_and_sockets(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        sockets.connections[sock] = conn
        sockets.get_or_create_session("ws-disc", app_state)
        # A pending browser-delegate request with an unresolved future.
        br_future = asyncio.get_running_loop().create_future()
        sockets.pending_browser_requests["req-1"] = (br_future, sock)
        # A streaming browser request.
        stream_q = asyncio.Queue()
        sockets.streaming_browser_requests["req-2"] = (stream_q, sock)

        await sockets.disconnect_all()

        assert sockets.connections == {}
        assert sockets.sessions == {}
        assert sockets.pending_browser_requests == {}
        assert sockets.streaming_browser_requests == {}
        assert br_future.cancelled()
        sock.close.assert_awaited_once_with(code=1012)

    async def test_swallows_close_errors(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        bad_sock = _mock_sock()
        bad_sock.close = AsyncMock(side_effect=RuntimeError("boom"))
        sockets.connections[bad_sock] = _base_conn(
            ws=bad_sock, app_state=app_state
        )

        # Must not raise even though close() blows up.
        await sockets.disconnect_all()

        assert sockets.connections == {}

    async def test_empty_is_noop(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        await sockets.disconnect_all()
        assert sockets.connections == {}
        assert sockets.sessions == {}

    async def test_disconnect_all_websockets_wrapper(self, app_state):
        """The package-level wrapper delegates to state.disconnect_all."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        sockets.connections[sock] = _base_conn(ws=sock, app_state=app_state)
        await disconnect_all_websockets(sockets)
        assert sockets.connections == {}
        sock.close.assert_awaited_once_with(code=1012)


# --- send_error ---


class TestSendError:
    def test_sends_error_json(self):
        sock = _mock_sock()
        send_error(sock, "bad thing")
        sock.send_json.assert_called_once_with(
            {"type": "error", "message": "bad thing"}
        )

    def test_code_field_added_when_given(self):
        """#2525: a machine-readable ``code`` rides the frame so clients
        can class the failure without parsing the message text."""
        sock = _mock_sock()
        send_error(sock, "host at capacity: ...", code="capacity")
        sock.send_json.assert_called_once_with(
            {
                "type": "error",
                "message": "host at capacity: ...",
                "code": "capacity",
            }
        )


# Proxy-trust, hosting-info, and client_is_loopback tests moved to
# test_util.py now that those helpers are Util(app_state) methods (#1503).
# --- handle_steer ---


class TestReadOnlyInputWhitelist:
    """Direct coverage for the read-only terminal-input whitelist (#1716).

    Only terminal-protocol RESPONSES tmux needs to initialize may pass;
    user typing and arbitrary escape sequences (notably OSC 52) are
    dropped.
    """

    @pytest.mark.parametrize(
        "data",
        [
            # DA1/DA2/DA3 device-attribute responses.
            "\x1b[?6c",
            "\x1b[?1;2c",
            "\x1b[?64;1;2;4;6;9;15;16;17;18;21;22c",
            "\x1b[>41;0;33c",
            "\x1b[=123c",
            # DSR cursor-position report.
            "\x1b[1;1R",
            "\x1b[24;80R",
            # OSC color reports (palette + default fg/bg/cursor),
            # terminated by either BEL or ESC '\'.
            "\x1b]11;rgb:0000/0000/0000\x07",
            "\x1b]10;rgb:aaaa/bbbb/cccc\x1b\\",
            "\x1b]12;rgb:ff/ff/ff\x07",
            "\x1b]4;0;rgb:00/00/00\x1b\\",
            # OSC color value forms beyond rgb: (#rrggbb / rgbi:).
            "\x1b]11;#ff00ff\x07",
            "\x1b]11;#aabbccddeeff\x1b\\",
            "\x1b]11;rgbi:255/0/255\x07",
            "\x1b]11;rgbi:0.5/0.0/1.0\x07",
            # XTVERSION response: DCS > | <name SP version> ST.
            "\x1bP>|xterm.js 5.5.0\x1b\\",
            "\x1bP>|tmux 3.4\x07",
            # XTGETTCAP response: DCS (0|1) + r <hex>=<hex> ST.
            "\x1bP1+r5443=787465726d\x1b\\",
            "\x1bP0+r\x1b\\",  # capability-not-found failure reply
            # Multiple responses batched in one message.
            "\x1b[?6c\x1b]11;rgb:0000/0000/0000\x07",
            "\x1b[?6c\x1bP>|xterm.js 5.5.0\x1b\\",
        ],
    )
    def test_allows_init_responses(self, data):
        assert _ws_controllers.is_allowed_read_only_input(data)

    @pytest.mark.parametrize(
        "data",
        [
            # User typing.
            "ls",
            "",
            # OSC 52 clipboard read/write — the headline threat.
            "\x1b]52;c;Zm9v\x07",  # write
            "\x1b]52;c;?\x07",  # read
            # Other OSC: title set, color reset.
            "\x1b]0;title\x07",
            "\x1b]104\x07",
            # CSI queries / commands (not responses).
            "\x1b[18t",  # report terminal size
            "\x1b[19t",
            "\x1b[6n",  # DSR cursor query
            "\x1b[2J",  # clear screen
            "\x1b[H",  # cursor home
            "\x1b[c",  # bare DA query
            # DCS passthrough (arbitrary tmux commands).
            "\x1bPtmux;evil\x1b\\",
            # Smuggling: text or OSC 52 around a valid response.
            "ls\x1b[?6c",
            "\x1b[?6cls",
            "\x1b[?6c\x1b]52;c;Zm9v\x07",
        ],
    )
    def test_blocks_everything_else(self, data):
        assert not _ws_controllers.is_allowed_read_only_input(data)


class TestHandleTerminalInput:
    async def test_writes_data(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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

    async def test_read_only_blocks_typing(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        t = _mock_terminal()
        t.read_only = True
        conn = _base_conn(app_state=app_state)
        conn.terminal_session = t
        conn.container_id = "cid"
        registry.track_activity("cid", "ws")

        await conn.handle_terminal_input({"data": "ls\n"})
        t.write.assert_not_awaited()
        registry.states.pop("ws", None)

    async def test_read_only_allows_escape_sequences(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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

    async def test_read_only_blocks_osc52_clipboard(self, app_state):
        """OSC 52 clipboard read/write is dropped for spectators (#1716)."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        t = _mock_terminal()
        t.read_only = True
        conn = _base_conn(app_state=app_state)
        conn.terminal_session = t
        conn.container_id = "cid"
        registry.track_activity("cid", "ws")

        for data in (
            "\x1b]52;c;Zm9v\x07",  # clipboard write
            "\x1b]52;c;?\x07",  # clipboard read
        ):
            await conn.handle_terminal_input({"data": data})
        t.write.assert_not_awaited()
        registry.states.pop("ws", None)

    async def test_oversized_input_dropped(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        t = _mock_terminal()
        conn = _base_conn(app_state=app_state)
        conn.terminal_session = t
        conn.container_id = "cid"
        registry.track_activity("cid", "ws")

        big_data = "x" * (_ws_support.MAX_INPUT_SIZE + 1)
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

    async def test_resize_malformed_values_fall_back(self):
        """#3071: a null/string/bool/oversized cols or rows must not raise
        (struct.pack only takes unsigned shorts), must not be persisted,
        and must not tear down the session — each bad field falls back to
        the current stored size."""
        t = _mock_terminal()
        conn = _base_conn()
        conn.terminal_session = t

        await conn.handle_terminal_resize({"cols": 120, "rows": 40})
        t.resize.reset_mock()

        # null / string / bool / oversized — all fall back to 120x40.
        await conn.handle_terminal_resize({"cols": None, "rows": None})
        await conn.handle_terminal_resize({"cols": "120", "rows": "40"})
        await conn.handle_terminal_resize({"cols": True, "rows": False})
        await conn.handle_terminal_resize({"cols": 70000, "rows": -1})
        await conn.handle_terminal_resize({"cols": 1.5, "rows": 2.5})

        assert t.resize.await_count == 5
        for call in t.resize.await_args_list:
            assert call.args == (120, 40)
        assert conn.terminal.cols == 120
        assert conn.terminal.rows == 40

    async def test_resize_valid_after_malformed_not_poisoned(self):
        """A malformed frame must not poison the stored size for a later
        valid resize or terminal_start (#3071)."""
        t = _mock_terminal()
        conn = _base_conn()
        conn.terminal_session = t

        await conn.handle_terminal_resize({"cols": None, "rows": "x"})
        assert conn.terminal.cols == 80
        assert conn.terminal.rows == 24

        await conn.handle_terminal_resize({"cols": 100, "rows": 30})
        t.resize.assert_awaited_with(100, 30)
        assert conn.terminal.cols == 100
        assert conn.terminal.rows == 30


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
    async def test_starts_session(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
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
                "sync_service_windows",
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
            ssh_agent_socket="/tmp/klangk-ssh-agent-uid.sock",
            terminal=_mock_term,
            workspace_name=None,
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

    async def test_start_malformed_dims_fall_back(self):
        """#3071: terminal_start with non-int cols/rows falls back to the
        stored size instead of crashing session.start inside the task
        ("Terminal start failed")."""
        app_state = _make_app_state()
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"
        conn.has_perm = AsyncMock(return_value=True)

        with patch.object(_ws_controllers, "TerminalSession") as MockTS:
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session
            await conn.handle_terminal_start({"cols": "x", "rows": None})
            # Let the background task run
            await asyncio.sleep(0)

        mock_session.start.assert_awaited_once_with(80, 24)
        assert conn.terminal.cols == 80
        assert conn.terminal.rows == 24
        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass

    async def test_terminal_start_fires_service_command(self, app_state):
        """terminal_start fires the service command in the agent's service
        session (the post-setup path, #1033/#1133) -- not in any user's
        session. The on_service_command_started callback is gone; the
        service command is fired via fire_service_command."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"
        conn._service_command = "./serve"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
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
                app_state.state.model.workspaces,
                "get_workspace",
                new=AsyncMock(return_value={"setup_state": "complete"}),
            ),
            patch.object(
                app_state.state.model.users,
                "agent_handle",
                new=AsyncMock(return_value="klangk"),
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

        # Fired in the service session (HOME pinned inside it, #2717),
        # not threaded into the user's TerminalSession (no
        # service_command kwarg).
        mock_ess.assert_awaited_once_with(
            "cid",
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

        conn.has_perm = deny_isolation
        await conn.handle_terminal_start({"cols": 80, "rows": 24})
        sent = sock.send_json.call_args_list
        # Should send terminal_started (no error) so the pane renders
        assert any(
            call.args[0].get("type") == "terminal_started" for call in sent
        )
        # But no actual session created
        assert conn.terminal_session is None

    async def test_rapid_terminal_start_debounced(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm
        import time

        conn._last_terminal_start = time.monotonic()
        await conn.handle_terminal_start({"cols": 80, "rows": 24})
        # Should be silently ignored (debounced)
        assert conn.terminal_session is None

        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_rename_failure_non_fatal(self, app_state):
        """If renaming the initial bash window fails, tabs still work."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
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

    async def test_start_window_list_failure_non_fatal(self, app_state):
        """If list_windows fails after terminal start, terminal still works."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
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

    async def test_start_container_gone_recovers_cleanly(self, app_state):
        """A recycled container during window sync is handled cleanly (#2178).

        When the container vanishes between terminal start and the window
        sync, the handler must NOT traceback an expected race: it logs a
        warning, stops the dead session, revokes the browser, and sends the
        client a user-visible error (no terminal_windows frame, no
        traceback in the log).
        """
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        with (
            patch.object(_ws_controllers, "TerminalSession") as MockTS,
            patch.object(
                _mock_term,
                "list_windows",
                side_effect=ContainerGoneError("container gone"),
            ),
            patch.object(_ws_controllers, "logger") as mock_logger,
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
        # terminal_started went out before the sync attempt
        assert any(
            isinstance(m, dict) and m.get("type") == "terminal_started"
            for m in sent
        )
        # a user-visible error was sent (clean recovery, not a silent drop)
        assert any(
            isinstance(m, dict)
            and m.get("type") == "error"
            and "recycled" in m.get("message", "")
            for m in sent
        )
        # no window/shared-terminal frames after the dead sync
        assert not any(
            isinstance(m, dict) and m.get("type") == "terminal_windows"
            for m in sent
        )
        # clean warning, never a traceback
        mock_logger.warning.assert_called()
        mock_logger.exception.assert_not_called()
        # dead session torn down and browser unregistered
        mock_session.stop.assert_awaited()
        assert conn.browser_id is None
        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_container_gone_send_error_fails(self, app_state):
        """A send_error failure during container-gone recovery is swallowed (#2178).

        The client socket may already be closed by the time we report the
        recycled-container error; that must not escape the recovery path.
        """
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        # terminal_started send must succeed; only the recovery error send
        # raises (the socket is gone by then).
        def _send_json(msg):
            if isinstance(msg, dict) and msg.get("type") == "error":
                raise WebSocketDisconnect

        sock.send_json.side_effect = _send_json

        with (
            patch.object(_ws_controllers, "TerminalSession") as MockTS,
            patch.object(
                _mock_term,
                "list_windows",
                side_effect=ContainerGoneError("container gone"),
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

        # recovery completed despite the closed socket
        mock_session.stop.assert_awaited()
        assert conn.browser_id is None
        conn.terminal_task.cancel()
        try:
            await conn.terminal_task
        except asyncio.CancelledError:
            pass
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_shared_list_failure_non_fatal(self, app_state):
        """If list_shared_terminals fails after start, terminal still works."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm
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

    async def test_restart_revokes_old_browser_registration(self, app_state):
        """Starting a second terminal revokes the previous browser registration."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
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

    async def test_terminal_start_disabled_never_arms_bridge(self, app_state):
        """#2710: with KLANGKD_BROWSER_DELEGATE_ENABLED=false, terminal_start
        registers no browser for bridge routing and attaches no ID into
        the container env (klangk-browser-id comes up empty)."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sockets = app_state.state.sockets
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")
        session = sockets.get_or_create_session("ws", app_state)
        await session.add_subscriber(sock, "cid")
        sockets.connections[sock] = conn

        app_state.state.settings.browser_delegate_enabled = False
        with (
            patch.object(_ws_controllers, "TerminalSession") as MockTS,
            patch.object(
                _mock_term, "attach_browser", new_callable=AsyncMock
            ) as mock_attach,
        ):
            mock_session = _mock_terminal()
            MockTS.return_value = mock_session

            async def fake_output():
                return
                yield

            mock_session.output = fake_output

            await conn.handle_terminal_start(
                {"cols": 80, "rows": 24, "browser_id": "bid-off"}
            )
            await asyncio.sleep(0)

            assert conn.browser_id is None
            assert registry.resolve_browser("bid-off") is None
            mock_attach.assert_not_awaited()

            conn.terminal_task.cancel()
            try:
                await conn.terminal_task
            except asyncio.CancelledError:
                pass

        app_state.state.settings.browser_delegate_enabled = True
        sockets.sessions.pop("ws", None)
        sockets.connections.pop(sock, None)
        registry.states.pop("ws", None)

    async def test_browser_reattach_updates_registration(self, app_state):
        """browser_reattach re-registers the browser ID and calls attach_browser."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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

    async def test_browser_reattach_disabled_drops_registration(
        self, app_state
    ):
        """#2710: with KLANGKD_BROWSER_DELEGATE_ENABLED=false, re-attach
        neither registers the new ID nor attaches it to the container —
        and drops any pre-disable registration instead."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"

        registry.register_browser("bid-old", "ws", sock)
        conn.browser_id = "bid-old"
        app_state.state.settings.browser_delegate_enabled = False

        with patch.object(
            _mock_term, "attach_browser", new_callable=AsyncMock
        ) as mock_attach:
            await conn.handle_browser_reattach({"browser_id": "bid-new"})

        assert conn.browser_id is None
        assert registry.resolve_browser("bid-new") is None
        assert registry.resolve_browser("bid-old") is None
        mock_attach.assert_not_awaited()

        app_state.state.settings.browser_delegate_enabled = True
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

    async def test_passes_service_command(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"
        conn._service_command = "pi"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.is_alive = True
        MockTS = MagicMock(return_value=mock_session)
        with (
            patch("klangk.wshandler.controllers.TerminalSession", MockTS),
            patch.object(_mock_term, "attach_browser", new_callable=AsyncMock),
            patch.object(
                app_state.state.model.workspaces,
                "get_workspace",
                new=AsyncMock(return_value={"setup_state": "complete"}),
            ),
            patch.object(
                app_state.state.model.users,
                "agent_handle",
                new=AsyncMock(return_value="klangk"),
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
            ssh_agent_socket="/tmp/klangk-ssh-agent-uid.sock",
            terminal=_mock_term,
            workspace_name=None,
        )
        mock_ess.assert_awaited_once_with(
            "cid",
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

    async def test_start_failure_sends_error(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock(
            side_effect=RuntimeError("podman broke")
        )
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk.wshandler.controllers.TerminalSession", MockTS):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        # Should have sent an error, not terminal_started
        sent = sock.send_json.call_args_list
        assert any(call.args[0].get("type") == "error" for call in sent)
        # Session is stored immediately but stop() is called on failure
        mock_session.stop.assert_awaited_once()
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_slow_client_cleans_up(self, app_state):
        """SlowClientError during start cleans up without sending error."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry

        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock(side_effect=SlowClientError())
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk.wshandler.controllers.TerminalSession", MockTS):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        mock_session.stop.assert_awaited_once()
        # No error message sent (client is gone)
        sent = sock.send_json.call_args_list
        assert not any(call.args[0].get("type") == "error" for call in sent)
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_start_failure_send_error_ws_dead(self, app_state):
        """If send_error itself fails with a WS error, it's swallowed."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock(side_effect=ValueError("bad config"))
        # send_json raises RuntimeError (a WS_ERRORS member) when
        # trying to send the error message
        sock.send_json = MagicMock(side_effect=RuntimeError("ws gone"))
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk.wshandler.controllers.TerminalSession", MockTS):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            await asyncio.sleep(0)

        mock_session.stop.assert_awaited_once()
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_cancellation_during_start_cleans_up(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock(side_effect=asyncio.CancelledError)
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk.wshandler.controllers.TerminalSession", MockTS):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            task = conn.terminal_task
            with pytest.raises(asyncio.CancelledError):
                await task

        # session.stop() must be called to clean up the PTY subprocess
        mock_session.stop.assert_awaited_once()
        registry.revoke_workspace_browsers("ws")
        registry.states.pop("ws", None)

    async def test_session_replaced_during_start_aborts(self, app_state):
        """If stop_terminal replaces the session while start() is running,
        the startup task stops the orphaned session and does not send
        terminal_started."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn.workspace_id = "ws"
        conn._user_home = "/home/testuser"

        async def _perm(*a):
            return True

        conn.has_perm = _perm  # type: ignore[method-assign]
        registry.track_activity("cid", "ws")

        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator

        async def start_and_replace(*a, **kw):
            # Simulate stop_terminal replacing the session mid-start
            conn.terminal_session = AsyncMock()

        mock_session.start = AsyncMock(side_effect=start_and_replace)
        MockTS = MagicMock(return_value=mock_session)
        with patch("klangk.wshandler.controllers.TerminalSession", MockTS):
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

        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "handle-test"
        )
        sock = _mock_sock()
        conn = _base_conn(
            user={"id": user["id"], "email": user["email"]}, ws=sock
        )
        # #3135: the per-handle machinery needs the ceiling on — the
        # deploy flag no longer defaults to per-handle homes.
        conn.app.state.settings.per_handle_home = True
        conn.workspace_id = ws["id"]
        conn.workspace = {
            "user_id": user["id"],
            "per_handle_home": True,
        }
        conn.container_id = "cid"

        with patch(
            "klangk.workspaces.populate_home_skel",
            new_callable=AsyncMock,
        ) as mock_skel:
            await conn.handle_set_handle({"handle": "alice"})
        mock_skel.assert_awaited_once_with(
            "cid", user["id"], conn.app.state.podman
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
        other = await app_state.state.model.users.create_user(
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
        # #3135: arm the per-handle ceiling — the handle-derivation
        # assertions below are per-handle-path only (under the default
        # ceiling-off the connect clamps to the shared home).
        app_state.state.settings.per_handle_home = True
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.state.workspaces.create_workspace(
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

        # Handle is derived from email at user creation time (#3135: the
        # ceiling armed above keeps this on the per-handle path)
        assert conn._user_home is not None
        assert conn._user_home.startswith("/home/")

        sockets.sessions.pop(workspace["id"], None)
        registry.states.pop(workspace["id"], None)

    async def test_handle_resolved_on_start(
        self, user, temp_data_dir, app_state
    ):

        ws = await app_state.state.workspaces.create_workspace(
            user["id"], "handle-test4"
        )
        sock = _mock_sock()
        conn = _base_conn(
            user={"id": user["id"], "email": user["email"]}, ws=sock
        )
        conn.workspace_id = ws["id"]

        # Handle is already in the DB from create_user
        handle = await app_state.state.model.users.get_user_handle(user["id"])
        assert handle is not None
        assert handle == user["handle"]


# --- forward_terminal_output ---


class TestForwardTerminalOutput:
    async def test_forwards_output(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
    async def test_cleanup_last_subscriber_removes_session(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
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

    async def test_cleanup_other_subscribers_remain(self, app_state):
        """When other subscribers remain, session stays alive."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
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

    async def test_cleanup_step_failure_skips_nothing(self, app_state, caplog):
        """#3069: one teardown step failing must neither skip the other
        stops nor the subscriber removal after them."""
        import logging

        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "ctr-teardown"
        conn.workspace_id = "ws-teardown"
        conn._idle_cb = None
        session = WorkspaceSession("ws-teardown", app_state)
        session.subscribers.add(sock)
        sockets.sessions["ws-teardown"] = session
        stopped = []

        async def failing_stop():
            stopped.append("terminal")
            raise RuntimeError("terminal teardown boom")

        async def ok_exec():
            stopped.append("exec")

        async def ok_ssh():
            stopped.append("ssh")

        conn.stop_terminal = failing_stop
        conn.stop_exec = ok_exec
        conn._stop_ssh_agent = ok_ssh
        with caplog.at_level(
            logging.ERROR, logger="klangk.wshandler.connection"
        ):
            await conn.cleanup()
        # All three stops ran despite the first raising …
        assert stopped == ["terminal", "exec", "ssh"]
        assert "Cleanup step failing_stop failed" in caplog.text
        # … and the session bookkeeping after them still ran.
        assert "ws-teardown" not in sockets.sessions


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
        frame = sock.send_json.call_args[0][0]
        assert "Permission denied" in frame["message"]
        assert frame["code"] == "forbidden"

    async def test_connect_success(self, user, agent_user, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
        # Integer timeout (default 60m) should show as "60m" not "60.0m"
        assert "60m" in conn.pending_status_msg

    async def test_connect_sends_service_command(
        self, user, agent_user, app_state
    ):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
        workspace = await app_state.state.workspaces.create_workspace(
            user["id"], "no-acl-ws"
        )
        conn = _base_conn(user={"id": "other-user", "email": "x"}, ws=sock)
        await conn.handle_workspace_connect({"workspaceId": workspace["id"]})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        denied = [
            c
            for c in calls
            if isinstance(c, dict) and c.get("type") == "error"
        ]
        assert any("Permission denied" in str(c) for c in denied)
        # #2891: machine-readable refusal code for the client.
        assert all(c.get("code") == "forbidden" for c in denied)

    async def test_connect_allowed_without_terminal(
        self, user, agent_user, app_state
    ):
        """#2975: ``join-workspace`` alone opens the gate — a files-only
        member (no ``terminal``) can connect and render the workspace;
        the Terminal tab simply won't mount for them."""
        from klangk import model

        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        member = await app_state.state.model.users.create_user(
            "files-only@example.com", "pw", verified=True
        )
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "files-only-ws"
        )
        resource = f"/workspaces/{workspace['id']}"
        for pos, perm in enumerate(("join-workspace", "files-view"), 100):
            await app_state.state.model.acl.add_acl_entry(
                resource,
                pos,
                model.ACTION_ALLOW,
                perm,
                model.PRINCIPAL_USER,
                user_id=member["id"],
            )
        conn = _base_conn(
            user=member,
            ws=sock,
            app_state=app_state,
        )

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
        assert len(ready) == 1

    async def test_connect_denied_terminal_without_join(self, user, app_state):
        """#2975: an old-style ``terminal``-only grant (pre-migration
        shape, or a hand-built ACL) no longer passes the connect gate —
        ``join-workspace`` is the gate, ``terminal`` is tab visibility."""
        from klangk import model

        sock = _mock_sock()
        member = await app_state.state.model.users.create_user(
            "terminal-only@example.com", "pw", verified=True
        )
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "terminal-only-ws"
        )
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{workspace['id']}",
            100,
            model.ACTION_ALLOW,
            "terminal",
            model.PRINCIPAL_USER,
            user_id=member["id"],
        )
        conn = _base_conn(
            user=member,
            ws=sock,
        )
        await conn.handle_workspace_connect({"workspaceId": workspace["id"]})
        frame = sock.send_json.call_args[0][0]
        assert "Permission denied" in frame["message"]
        assert frame["code"] == "forbidden"

    async def test_connect_race_deleted(self, user, app_state):
        """ACL passes but workspace deleted before lookup."""
        from klangk import model

        sock = _mock_sock()
        fake_id = "deleted-ws-id"
        await app_state.state.model.acl.add_acl_entry(
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
        # #2891: machine-readable refusal code for the client.
        assert any(c.get("code") == "not_found" for c in calls)

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

    async def test_connect_container_start_podman_error(
        self, user, agent_user, app_state
    ):
        """#3071/#2676: a PodmanError from start_container surfaces its
        actionable message, matching the restart path — not the per-frame
        guard's generic frame, nor a dropped session."""
        sock = _mock_sock()
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "podman-fail"
        )
        conn = _base_conn(user=user, ws=sock)

        with patch.object(
            Connection,
            "start_workspace_container",
            side_effect=PodmanError(500, "no space left on device"),
        ):
            await conn.handle_workspace_connect(
                {"workspaceId": workspace["id"]}
            )

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        errors = [c for c in calls if c.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["message"] == (
            "Container start failed: [500] no space left on device"
        )

    async def test_connect_capacity_refusal_error_frame(
        self, user, agent_user, app_state
    ):
        """#2525: an admission-control refusal (host memory / user quota)
        is sent as a clear error frame — the actionable message, not a
        dropped socket — so the client can surface 'stop a workspace
        first / free host memory' and retry."""
        from klangk.exceptions import WorkspaceCapacityError

        sock = _mock_sock()
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "cap-ws"
        )
        conn = _base_conn(user=user, ws=sock)

        with patch.object(
            Connection,
            "start_workspace_container",
            side_effect=WorkspaceCapacityError(
                "host at capacity: 1.2 GB available, workspace wants "
                "9.0 GB (memory limit 8.0 GB + 1.0 GB reserve). Stop an "
                "idle workspace, free host memory, or lower the workspace "
                "memory limit (KLANGKD_CONTAINER_MEMORY_LIMIT)."
            ),
        ):
            await conn.handle_workspace_connect(
                {"workspaceId": workspace["id"]}
            )

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        errors = [c for c in calls if c.get("type") == "error"]
        assert len(errors) == 1
        assert "host at capacity" in errors[0]["message"]
        # Machine-readable class so the UI can render it as a capacity
        # refusal without parsing the message (#2525).
        assert errors[0]["code"] == "capacity"
        # The socket stays open — a refusal is not a disconnect.
        sock.close.assert_not_awaited()


class TestHandleWorkspaceDisconnect:
    async def test_disconnect(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.state.workspaces.create_workspace(
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
        # #3135: default deploy (ceiling off) clamps the stored-true column
        # to the shared home — asserted explicitly so this stays a wiring
        # pin rather than a vacuous startswith("/home/").
        assert conn._user_home == container.SHARED_HOME

        sockets.sessions.pop(workspace["id"], None)
        registry.states.pop(workspace["id"], None)

    async def test_resolves_existing_handle(self, user, app_state):
        app_state = _make_app_state()
        # #3135: arm the per-handle ceiling — the stored true column is
        # inert while the deploy flag is off.
        app_state.state.settings.per_handle_home = True
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.state.workspaces.create_workspace(
            user["id"], "handle-ws"
        )
        # Set a custom handle in the DB
        await app_state.state.model.users.set_user_handle(user["id"], "chris")

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
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.state.workspaces.create_workspace(
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
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.pending_status_msg = "stale message from prior connect"
        workspace = await app_state.state.workspaces.create_workspace(
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


# --- home layout (per_handle_home gate, #2169 chunk 2 / #2720) ---


class TestSharedHomeLayout:
    """Both layouts through the WS connect / handle-set / exec seams.

    per_handle_home=False workspaces serve the single shared
    /home/klangk for every connection; per-handle workspaces keep the
    symlink machinery byte-identical (the default path above).
    """

    async def test_connect_shared_layout_skips_symlink(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.state.workspaces.create_workspace(
            user["id"], "shared-ws", per_handle_home=False
        )
        captured = {}

        async def fake_start(spec):
            captured["spec"] = spec
            registry.track_activity("cid-sh", workspace["id"])
            return ("cid-sh", "created")

        with (
            patch.object(registry, "start_container", side_effect=fake_start),
            patch("glob.glob", return_value=[]),
            patch.object(
                app_state.state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
            ) as symlink_mock,
            patch.object(
                app_state.state.model.users,
                "get_user_handle",
                new_callable=AsyncMock,
            ) as handle_mock,
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        # No per-user machinery on the shared path: no handle lookup,
        # no /home/{handle} -> .users/{uid} symlink, no per-user skel.
        handle_mock.assert_not_awaited()
        symlink_mock.assert_not_awaited()
        assert conn._user_home == container.SHARED_HOME
        assert conn._home_created is False
        # The layout rides the start spec (health-monitor branch, same
        # pattern as health_check/owner_id; the spec→ContainerState
        # threading is covered in TestStartContainer).
        assert captured["spec"].per_handle_home is False

        sockets.sessions.pop(workspace["id"], None)
        registry.states.pop(workspace["id"], None)

    async def test_connect_per_handle_layout_keeps_symlink(
        self, user, app_state
    ):
        # Explicit counterpart: with the ceiling armed (#3135 — the
        # deploy flag must be on for per-handle homes at all), the
        # stored-true layout is unchanged.
        app_state = _make_app_state()
        app_state.state.settings.per_handle_home = True
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.state.workspaces.create_workspace(
            user["id"], "per-handle-ws"
        )
        captured = {}

        async def fake_start(spec):
            captured["spec"] = spec
            registry.track_activity("cid-ph", workspace["id"])
            return ("cid-ph", "created")

        with (
            patch.object(registry, "start_container", side_effect=fake_start),
            patch("glob.glob", return_value=[]),
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        assert conn._user_home == f"/home/{user['handle']}"
        assert conn._home_created is True  # fresh user dir → skel populate
        assert captured["spec"].per_handle_home is True

        sockets.sessions.pop(workspace["id"], None)
        registry.states.pop(workspace["id"], None)

    async def test_connect_stored_true_clamped_by_ceiling(
        self, user, app_state
    ):
        """#3135: a workspace that stored per_handle_home=true (create-
        time opt-in, or m0009's backfill) gets the shared layout while
        the deploy ceiling is off — the stored column is clamped at
        connect, never rewritten."""
        app_state = _make_app_state()  # settings default: ceiling OFF
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        workspace = await app_state.state.workspaces.create_workspace(
            user["id"], "clamped-ws", per_handle_home=True
        )
        captured = {}

        async def fake_start(spec):
            captured["spec"] = spec
            registry.track_activity("cid-cl", workspace["id"])
            return ("cid-cl", "created")

        with (
            patch.object(registry, "start_container", side_effect=fake_start),
            patch("glob.glob", return_value=[]),
            patch.object(
                app_state.state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
            ) as symlink_mock,
        ):
            await conn.start_workspace_container(workspace["id"], workspace)

        symlink_mock.assert_not_awaited()  # no per-user machinery
        assert conn._user_home == container.SHARED_HOME
        assert conn._home_created is False
        assert captured["spec"].per_handle_home is False

        sockets.sessions.pop(workspace["id"], None)
        registry.states.pop(workspace["id"], None)

    async def test_set_handle_shared_layout_skips_symlink(
        self, user, app_state
    ):
        sock = _mock_sock()
        conn = _base_conn(
            user={"id": user["id"], "email": user["email"]}, ws=sock
        )
        conn.workspace_id = "ws-shared"
        conn.workspace = {"user_id": user["id"], "per_handle_home": False}
        conn.container_id = "cid"
        conn._user_home = container.SHARED_HOME

        with (
            patch.object(
                conn.app.state.workspaces,
                "ensure_home_symlink",
                new_callable=AsyncMock,
            ) as symlink_mock,
            patch(
                "klangk.workspaces.populate_home_skel",
                new_callable=AsyncMock,
            ) as skel_mock,
        ):
            await conn.handle_set_handle({"handle": "alice"})
        symlink_mock.assert_not_awaited()
        skel_mock.assert_not_awaited()
        # The handle still updates in the DB and the reply reports the
        # (constant) shared home — nothing per-handle to refresh.
        sent = sock.send_json.call_args_list
        assert any(
            call.args[0].get("type") == "handle_set"
            and call.args[0].get("handle") == "alice"
            and call.args[0].get("home") == container.SHARED_HOME
            for call in sent
        )
        assert conn._user_home == container.SHARED_HOME

    async def test_exec_shared_home_sets_env_and_work_dir(self, app_state):
        # The exec path reads the connection's home; under the shared
        # layout that is /home/klangk, so HOME and the cwd both point
        # there (#2169 decision: exec cwd is /home/klangk, not
        # /home/klangk).
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn._user_home = container.SHARED_HOME
        mock_session = AsyncMock()

        async def empty_output():
            return
            yield  # pragma: no cover

        mock_session.output = empty_output
        mock_session.returncode = 0
        with (
            patch(
                "klangk.wshandler.controllers.ExecSession",
                return_value=mock_session,
            ) as session_cls,
            patch.object(registry, "record_activity"),
            patch.object(conn, "has_perm", new=AsyncMock(return_value=True)),
        ):
            await conn.handle_exec_start({"command": ["ls"]})
        kwargs = session_cls.call_args.kwargs
        assert f"HOME={container.SHARED_HOME}" in kwargs["env"]
        assert kwargs["work_dir"] == container.SHARED_HOME
        conn.exec_task.cancel()
        try:
            await conn.exec_task
        except asyncio.CancelledError:
            pass


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

    async def test_cleanup_failure_still_pops_connection(self, user, caplog):
        """#3069: a cleanup exception must not skip the registry pop —
        otherwise the SafeWebSocket->Connection entry leaks."""
        import logging

        app_state = _make_app_state()
        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(side_effect=[WebSocketDisconnect()])
        with (
            patch.object(
                Connection,
                "cleanup",
                new=AsyncMock(side_effect=RuntimeError("cleanup boom")),
            ),
            caplog.at_level(logging.ERROR, logger="klangk.wshandler.dispatch"),
        ):
            await handle_websocket(websocket, app_state)  # must not raise
        assert app_state.state.sockets.connections == {}
        assert "Connection cleanup failed" in caplog.text

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
        registry = app_state.state.container_registry

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})

        workspace = await app_state.state.workspaces.create_workspace(
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
    async def test_missing_token(self, app_state):
        app_state = _make_app_state()
        websocket = _mock_raw_sock(query_params={})
        await handle_websocket(websocket, app_state)
        websocket.close.assert_awaited_once_with(
            code=4001, reason="Missing token"
        )

    async def test_invalid_token(self, db, app_state):
        app_state = _make_app_state()
        websocket = _mock_raw_sock(query_params={"token": "bad"})
        await handle_websocket(websocket, app_state)
        websocket.close.assert_awaited_once_with(
            code=4001, reason="Invalid token"
        )

    async def test_expired_token(self, user, app_state):
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

    async def test_valid_token_then_disconnect(self, user, app_state):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(side_effect=WebSocketDisconnect())

        await handle_websocket(websocket, app_state)

        websocket.accept.assert_awaited_once()

    async def test_unexpected_exception_logged(self, user, app_state):
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=ValueError("unexpected")
        )

        await handle_websocket(websocket, app_state)

        websocket.accept.assert_awaited_once()

    async def test_snapshot_failure_still_runs_cleanup(self, user, app_state):
        """#1714 review: the connect-time snapshot awaits DB queries inside
        the handler's try — a raise there must run the ``finally`` cleanup
        (sender stopped, connection unregistered), not leak them."""
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(side_effect=WebSocketDisconnect())

        with patch.object(
            app_state.state.sockets,
            "send_service_health_snapshot",
            AsyncMock(side_effect=RuntimeError("db unavailable")),
        ):
            await handle_websocket(websocket, app_state)

        websocket.accept.assert_awaited_once()
        # The finally block ran: the connection was unregistered.
        assert not app_state.state.sockets.connections

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

    @pytest.mark.parametrize("frame", ["[]", '"x"', "3", "null"])
    async def test_non_dict_frame_rejected_session_survives(
        self, user, app_state, frame
    ):
        """#3071: a JSON frame that is not an object has no "cmd" and must
        get an error frame — not an AttributeError that drops the session.
        The following valid frame still processes."""
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                frame,
                json.dumps({"cmd": "bogus"}),
                WebSocketDisconnect(),
            ]
        )

        await handle_websocket(websocket, app_state)

        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Invalid frame" in str(c) for c in calls)
        assert any("Unknown command" in str(c) for c in calls)

    async def test_handler_exception_error_frame_session_survives(
        self, user, app_state
    ):
        """#3071: a handler exception must be answered with an error frame
        and the loop must keep dispatching — not end the session."""
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "heartbeat"}),
                json.dumps({"cmd": "bogus"}),
                WebSocketDisconnect(),
            ]
        )

        with patch.object(
            Connection,
            "handle_heartbeat",
            new=AsyncMock(side_effect=ValueError("handler bug")),
        ):
            await handle_websocket(websocket, app_state)

        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Error handling command" in str(c) for c in calls)
        # The loop survived: the frame after the failing one was dispatched.
        assert any("Unknown command" in str(c) for c in calls)

    async def test_handler_slow_client_error_ends_session(
        self, user, app_state
    ):
        """#3071: the per-frame guard must not swallow connection-level
        failures — a SlowClientError from a handler ends the session."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "heartbeat"}),
                WebSocketDisconnect(),
            ]
        )

        with patch.object(
            Connection,
            "handle_heartbeat",
            new=AsyncMock(side_effect=SlowClientError("outbound queue full")),
        ):
            await handle_websocket(websocket, app_state)

        websocket.accept.assert_awaited_once()
        assert websocket not in sockets.connections
        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert not any("Error handling command" in str(c) for c in calls)

    async def test_state_handler_exception_error_frame_session_survives(
        self, user, app_state
    ):
        """#3071: the per-frame guard covers the WS_STATE_COMMANDS table
        too — a raising state handler gets an error frame, not a dropped
        session."""
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": "browser_response", "id": "x"}),
                json.dumps({"cmd": "bogus"}),
                WebSocketDisconnect(),
            ]
        )

        with patch.object(
            app_state.state.sockets,
            "handle_browser_response",
            side_effect=ValueError("state handler bug"),
        ):
            await handle_websocket(websocket, app_state)

        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Error handling command" in str(c) for c in calls)
        assert any("Unknown command" in str(c) for c in calls)

    async def test_unhashable_cmd_error_frame_session_survives(
        self, user, app_state
    ):
        """#3071: an unhashable cmd (list) raises TypeError inside the
        table lookup — the guard answers it with an error frame and the
        session keeps dispatching."""
        app_state = _make_app_state()

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(
            side_effect=[
                json.dumps({"cmd": ["bogus"]}),
                json.dumps({"cmd": "bogus"}),
                WebSocketDisconnect(),
            ]
        )

        await handle_websocket(websocket, app_state)

        calls = [c[0][0] for c in websocket.send_json.call_args_list]
        assert any("Error handling command" in str(c) for c in calls)
        assert any("Unknown command" in str(c) for c in calls)

    async def test_ui_ready_with_pending(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry

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

    async def test_ui_ready_no_pending(self, user, app_state):
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

    async def test_general_exception_logged(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets

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
        with patch.object(conn, "has_perm", new=AsyncMock(return_value=False)):
            await conn.handle_exec_start({"command": ["ls"]})
        sock.send_json.assert_called()
        assert "exec-and-sync permission" in sock.send_json.call_args[0][
            0
        ].get("message", "")
        assert conn.exec_session is None

    async def test_exec_start_no_command(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        with patch.object(conn, "has_perm", new=AsyncMock(return_value=True)):
            await conn.handle_exec_start({"command": []})
        sock.send_json.assert_called()
        assert "command" in sock.send_json.call_args[0][0].get("message", "")

    async def test_exec_start_requires_exec_permission(self):
        """#2706/#2712: the one-shot exec channel — which ``klangk sync``
        rides on — is gated on the dedicated ``exec-and-sync`` permission, not on
        ``code-in-isolation``: a member who may open isolated terminals
        but lacks ``exec-and-sync`` gets no exec session (and thus no sync
        in either direction)."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        with patch.object(
            conn,
            "has_perm",
            new=AsyncMock(side_effect=lambda p: p == "code-in-isolation"),
        ):
            await conn.handle_exec_start(
                {
                    "command": [
                        "rsync",
                        "--server",
                        "--sender",
                        "-vlogDtprz",
                        ".",
                        "/src",
                    ]
                }
            )
        sent = sock.send_json.call_args[0][0]
        assert "exec-and-sync permission" in sent.get("message", "")
        assert conn.exec_session is None

    async def test_exec_start_with_exec_and_sync_permission_runs(
        self, app_state
    ):
        """``exec-and-sync`` alone authorizes the session — the gate replaced the
        old ``code-in-isolation`` check on this path, it does not stack
        on top of it (terminals keep using ``code-in-isolation``)."""
        app_state = _make_app_state()
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        mock_session = AsyncMock()

        async def empty_output():
            return
            yield  # pragma: no cover

        mock_session.output = empty_output
        mock_session.returncode = 0
        with patch(
            "klangk.wshandler.controllers.ExecSession",
            return_value=mock_session,
        ):
            with patch.object(
                conn,
                "has_perm",
                new=AsyncMock(side_effect=lambda p: p == "exec-and-sync"),
            ):
                await conn.handle_exec_start({"command": ["ls"]})
        assert conn.exec_session is mock_session
        conn.exec_task.cancel()
        try:
            await conn.exec_task
        except asyncio.CancelledError:
            pass

    async def test_exec_start_success(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
            "klangk.wshandler.controllers.ExecSession",
            return_value=mock_session,
        ):
            with patch.object(registry, "record_activity"):
                with patch.object(
                    conn, "has_perm", new=AsyncMock(return_value=True)
                ):
                    await conn.handle_exec_start({"command": ["ls"]})
        assert conn.exec_session is mock_session
        assert conn.exec_task is not None
        conn.exec_task.cancel()
        try:
            await conn.exec_task
        except asyncio.CancelledError:
            pass

    async def test_exec_start_passes_ssh_agent_socket(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        mock_session = AsyncMock()
        mock_session.output = _empty_async_generator
        mock_session.start = AsyncMock()

        async def empty_output():
            return
            yield  # pragma: no cover

        mock_session.output = empty_output
        mock_session.returncode = 0
        with patch(
            "klangk.wshandler.controllers.ExecSession",
            return_value=mock_session,
        ) as mock_cls:
            with patch.object(registry, "record_activity"):
                with patch.object(
                    conn, "has_perm", new=AsyncMock(return_value=True)
                ):
                    await conn.handle_exec_start({"command": ["ls"]})
        call_kwargs = mock_cls.call_args[1]
        # exec wires SSH_AUTH_SOCK to the deterministic per-user path on
        # every run, regardless of relay state (#2001).
        assert (
            "SSH_AUTH_SOCK=/tmp/klangk-ssh-agent-uid.sock"
            in call_kwargs["env"]
        )
        assert "HOME=/home/admin" in call_kwargs["env"]
        assert call_kwargs["work_dir"] == "/home/admin"
        conn.exec_task.cancel()
        try:
            await conn.exec_task
        except asyncio.CancelledError:
            pass

    async def test_exec_input_sends_data(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
            b"x" * (_ws_support.MAX_INPUT_SIZE + 1)
        ).decode()
        await conn.handle_exec_input({"data": big_data})
        session.write.assert_not_awaited()

    @pytest.mark.parametrize("data", ["!!!not-base64!!!", 5, None, ["x"]])
    async def test_exec_input_invalid_base64_dropped(self, data):
        """#3071: invalid base64 (or a non-string data field) must be
        dropped, not raise binascii.Error/TypeError out of the handler
        and kill the session."""
        session = AsyncMock()
        session.is_alive = True
        conn = _base_conn()
        conn.container_id = "cid"
        conn.exec_session = session

        await conn.handle_exec_input({"data": data})

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

    async def test_forward_exec_output(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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

    async def test_forward_exec_output_ws_error(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
            has_perm=AsyncMock(return_value=has_perm),
            app=app_state,
            # exec start derives the SSH_AUTH_SOCK path from the user id
            # (#2001), so the fake connection needs a user identity.
            user={
                "id": "uid",
                "email": "testuser@example.com",
                "handle": "testuser",
            },
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
        assert "exec-and-sync permission" in msg["message"]
        assert ctrl.session is None

    async def test_start_no_command_sends_error(self):
        ctrl, sock, _ = self._controller()
        await ctrl.start({"command": []})
        msg = sock.send_json.call_args[0][0]
        assert "command" in msg["message"]
        assert ctrl.session is None

    async def test_start_stops_existing_session_first(self, app_state):
        """start() tears down any in-flight exec before starting a new one."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        old = AsyncMock()
        ctrl.session = old
        with (
            patch("klangk.wshandler.controllers.ExecSession") as MockExec,
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

    async def test_start_passes_user_home_and_ssh_agent_socket(
        self, app_state
    ):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ctrl, _, _ = self._controller(
            user_home="/home/admin",
            ssh_agent_socket="/tmp/agent.sock",
        )
        with (
            patch("klangk.wshandler.controllers.ExecSession") as MockExec,
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
        # SSH_AUTH_SOCK is always the deterministic per-user path (#2001);
        # the relay-state socket is no longer the source.
        assert "SSH_AUTH_SOCK=/tmp/klangk-ssh-agent-uid.sock" in kwargs["env"]
        assert kwargs["work_dir"] == "/home/admin"

    async def test_start_defaults_work_dir_when_no_user_home(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ctrl, _, _ = self._controller(user_home=None, app_state=app_state)
        with (
            patch("klangk.wshandler.controllers.ExecSession") as MockExec,
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
        # No HOME (user_home is None), but SSH_AUTH_SOCK is still wired
        # to the deterministic per-user path on every exec (#2001).
        assert kwargs["env"] == [
            "SSH_AUTH_SOCK=/tmp/klangk-ssh-agent-uid.sock"
        ]
        assert kwargs["work_dir"] == "/home/klangk"

    async def test_exec_start_sets_ssh_agent_socket_without_relay(
        self, app_state
    ):
        """exec_start wires ``SSH_AUTH_SOCK`` to the deterministic per-user
        path even with no agent relay active (#2001) — a set-but-inert var
        that goes live the moment a relay binds. Mirrors the terminal-start
        without-relay contract for the exec path (notably the rsync /
        git-over-ssh transports, which previously ran with the var unset)."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ctrl, _, conn = self._controller(app_state=app_state)
        assert conn._ssh_agent_socket is None  # no relay active
        with (
            patch("klangk.wshandler.controllers.ExecSession") as MockExec,
            patch.object(registry, "record_activity"),
        ):
            mock_session = MockExec.return_value
            mock_session.start = AsyncMock()
            await ctrl.start({"command": ["rsync"], "login": False})
            ctrl.task.cancel()
            try:
                await ctrl.task
            except asyncio.CancelledError:
                pass
        env = MockExec.call_args.kwargs["env"]
        assert "SSH_AUTH_SOCK=/tmp/klangk-ssh-agent-uid.sock" in env

    async def test_start_default_login_false(self, app_state):
        """#1041: a message with no ``login`` key runs raw argv (no
        shell) -- the safe default for any caller, and what rsync needs."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        with (
            patch("klangk.wshandler.controllers.ExecSession") as MockExec,
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

    async def test_start_login_true_threads_through(self, app_state):
        """#1041: ``login: True`` in the message reaches ExecSession.start
        so the command runs as a bash login shell (sources ~/.profile).
        This is the klangk exec default."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        with (
            patch("klangk.wshandler.controllers.ExecSession") as MockExec,
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

    async def test_input_oversized_dropped(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        session = AsyncMock()
        session.is_alive = True
        ctrl.session = session
        big = base64.b64encode(
            b"x" * (_ws_support.MAX_INPUT_SIZE + 1)
        ).decode()
        with patch.object(registry, "record_activity"):
            await ctrl.input({"data": big})
        session.write.assert_not_awaited()

    async def test_input_writes_and_records_activity(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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

    async def test_forward_output_relays_chunks_and_exit_code(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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

    async def test_forward_output_exit_code_defaults_to_1(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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

    async def test_forward_output_records_activity_when_container_set(
        self, app_state
    ):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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

    async def test_forward_output_swallows_ws_error(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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
                new=AsyncMock(return_value=(0, "", "")),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
        ):
            await conn.handle_ssh_agent_start()
            # Let the relay task run and finish (stdout returns b"").
            assert conn.ssh_agent.task is not None
            await conn.ssh_agent.task
        assert conn.ssh_agent.proc is mock_proc
        assert conn.ssh_agent.socket is not None
        sock.send_json.assert_called()
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_started"
        assert "socket" in msg

    async def test_ssh_agent_start_waits_for_socket_bound(self):
        """ssh_agent_start polls until the relay socket is bound (#2535).

        The first readiness poll misses (socket not bound yet), the second
        hits — the event must fire only after the bound poll.
        """
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stdin = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        # exec_container call order in start(): pkill, rm, then readiness
        # polls. First poll misses (rc=1), second sees the bound socket.
        exec_mock = AsyncMock(
            side_effect=[
                (0, "", ""),
                (0, "", ""),
                (1, "", ""),
                (0, "", ""),
            ]
        )
        with (
            patch.object(_mock_pod, "exec_container", new=exec_mock),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
            patch("klangk.wshandler.controllers.SSH_AGENT_READY_POLL", 0.0),
        ):
            await conn.handle_ssh_agent_start()
            assert conn.ssh_agent.task is not None
            await conn.ssh_agent.task
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_started"
        # Exactly one readiness poll missed then one hit: 4 exec calls.
        assert exec_mock.await_count == 4

    async def test_ssh_agent_start_ready_timeout_sends_started_anyway(self):
        """Readiness timeout still emits ssh_agent_started (#2535).

        The event is best-effort gated: a slow runtime must not leave
        clients waiting on an event that never comes.
        """
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stdin = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        # Every readiness poll misses.
        exec_mock = AsyncMock(return_value=(1, "", ""))
        with (
            patch.object(_mock_pod, "exec_container", new=exec_mock),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
            patch(
                "klangk.wshandler.controllers.SSH_AGENT_READY_TIMEOUT", 0.05
            ),
            patch("klangk.wshandler.controllers.SSH_AGENT_READY_POLL", 0.01),
        ):
            await conn.handle_ssh_agent_start()
            assert conn.ssh_agent.task is not None
            await conn.ssh_agent.task
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_started"
        # pkill + rm + at least one (timed-out) readiness poll.
        assert exec_mock.await_count >= 3

    async def test_ssh_agent_start_ready_error_sends_started_anyway(self):
        """A podman failure during the readiness poll is not fatal (#2535)."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stdin = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        # pkill/rm succeed; the readiness poll itself blows up.
        exec_mock = AsyncMock(
            side_effect=[
                (0, "", ""),
                (0, "", ""),
                PodmanError(1, "podman exec exploded"),
            ]
        )
        with (
            patch.object(_mock_pod, "exec_container", new=exec_mock),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
        ):
            await conn.handle_ssh_agent_start()
            assert conn.ssh_agent.task is not None
            await conn.ssh_agent.task
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_started"
        assert exec_mock.await_count == 3

    async def test_ssh_agent_data_writes_to_stdin(self):
        import base64

        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        conn.ssh_agent.proc = mock_proc
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
        conn.ssh_agent.proc = mock_proc
        conn.ssh_agent.socket = "/tmp/test.sock"
        conn.ssh_agent.task = asyncio.create_task(asyncio.sleep(999))
        with patch.object(
            _mock_pod,
            "exec_container",
            new=AsyncMock(),
        ):
            await conn.handle_ssh_agent_stop()
        assert conn.ssh_agent.proc is None
        assert conn.ssh_agent.socket is None
        sock.send_json.assert_called()
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_stopped"

    async def test_stop_ssh_agent_cleanup_on_disconnect(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        conn.ssh_agent.proc = mock_proc
        conn.ssh_agent.socket = "/tmp/test.sock"
        conn.ssh_agent.task = asyncio.create_task(asyncio.sleep(999))
        with patch.object(
            _mock_pod,
            "exec_container",
            new=AsyncMock(),
        ):
            await conn._stop_ssh_agent()
        assert conn.ssh_agent.proc is None
        assert conn.ssh_agent.task is None
        assert conn.ssh_agent.socket is None

    async def test_forward_ssh_agent_output(self):
        import base64

        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        read_data = [b"agent-response", b""]
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(side_effect=read_data)
        conn.ssh_agent.proc = mock_proc
        await conn._forward_ssh_agent_output()
        calls = [
            c[0][0]
            for c in sock.send_json.call_args_list
            if c[0][0].get("type") == "ssh_agent_response"
        ]
        assert len(calls) == 1
        assert base64.b64decode(calls[0]["data"]) == b"agent-response"

    async def test_forward_ssh_agent_output_swallows_slow_client(self, caplog):
        """#3069: a slow client must end the relay quietly.

        Unhandled, the relay task completed with SlowClientError and it
        resurfaced from _cancel_task's await through stop()/cleanup(),
        dropping the whole WebSocket and skipping the handler's
        connections.pop.
        """
        import logging

        sock = _mock_sock()
        sock.send_json = MagicMock(
            side_effect=SlowClientError("outbound queue full")
        )
        conn = _base_conn(ws=sock)
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(side_effect=[b"agent-response", b""])
        conn.ssh_agent.proc = mock_proc
        with caplog.at_level(
            logging.WARNING, logger="klangk.wshandler.controllers"
        ):
            await conn._forward_ssh_agent_output()  # must not raise
        assert "SSH agent output relay error" in caplog.text

    async def test_cancel_task_swallows_failed_task_exception(self, caplog):
        """#3069: awaiting an already-failed task must not leak its
        exception through stop()/cleanup()."""
        import logging

        conn = _base_conn()

        async def boom():
            raise ValueError("relay bug")

        task = asyncio.create_task(boom())
        await asyncio.sleep(0)  # let it fail
        conn.ssh_agent.task = task
        with caplog.at_level(
            logging.ERROR, logger="klangk.wshandler.controllers"
        ):
            await conn.ssh_agent._cancel_task()  # must not raise
        assert conn.ssh_agent.task is None
        assert "SSH agent relay task failed" in caplog.text

    async def test_ssh_agent_start_with_terminal(self, app_state):
        """SSH_AUTH_SOCK is passed to TerminalSession when agent is active."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn._user_home = "/home/testuser"
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
                "klangk.wshandler.controllers.TerminalSession",
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
                "has_perm",
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
            workspace_name=None,
        )

    async def test_terminal_start_wires_agent_socket_without_relay(
        self, app_state
    ):
        """terminal_start points SSH_AUTH_SOCK at the deterministic path even
        when no agent relay is active yet (#2001).

        This is the TUI / autostart case that was broken: the base tmux
        session is created (window-0 shell spawned) BEFORE the agent relay
        starts, so the shell never received SSH_AUTH_SOCK. The fix wires
        every terminal to the deterministic per-user socket path at creation
        time — inert until a relay binds it, live the moment one does — so it
        does not matter how or when the terminal (or its agent) was created.
        Here ``conn.ssh_agent.socket`` is left at its default (None, no
        relay) yet the deterministic path is still passed through.
        """
        app_state = _make_app_state()
        sock = _mock_sock()
        conn = _base_conn(ws=sock, app_state=app_state)
        conn.container_id = "cid"
        conn._user_home = "/home/testuser"
        assert conn.ssh_agent.socket is None  # no relay active
        mock_session = AsyncMock()
        mock_session.start = AsyncMock()
        mock_session.session_name = "uid"
        mock_session.tmux_session_name = "uid"

        async def empty_output():
            return
            yield  # pragma: no cover

        mock_session.output = empty_output
        with (
            patch(
                "klangk.wshandler.controllers.TerminalSession",
                return_value=mock_session,
            ) as MockTS,
            patch.object(
                app_state.state.container_registry, "record_activity"
            ),
            patch.object(_mock_term, "attach_browser", new=AsyncMock()),
            patch.object(_mock_term, "list_windows", return_value=[]),
            patch.object(conn, "has_perm", new=AsyncMock(return_value=True)),
        ):
            await conn.handle_terminal_start({"cols": 80, "rows": 24})
            for _ in range(4):
                await asyncio.sleep(0)

        assert MockTS.call_args.kwargs["ssh_agent_socket"] == (
            "/tmp/klangk-ssh-agent-uid.sock"
        )


class TestSshAgentForwarder:
    """Unit tests for the SshAgentForwarder collaborator in isolation.

    These exercise the forwarder directly against a lightweight fake
    connection (a SimpleNamespace), proving the collaborator is
    decoupled from Connection (issue #961) and covering the error
    branches that were previously excluded with
    ``# pragma: no cover``.
    """

    def test_socket_path_is_deterministic_per_user(self):
        """``ssh_agent_socket_path`` is the stable per-user relay path (#2001)."""
        assert (
            _ws_controllers.ssh_agent_socket_path("uid")
            == "/tmp/klangk-ssh-agent-uid.sock"
        )
        assert (
            _ws_controllers.ssh_agent_socket_path("user-42")
            == "/tmp/klangk-ssh-agent-user-42.sock"
        )

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
            app=SimpleNamespace(
                state=SimpleNamespace(podman=_mock_pod, terminal=_mock_term)
            ),
            # #3022: start() gates on the session permissions before
            # spawning the relay; the collaborator tests hold the
            # own-terminal permission.
            has_perm=AsyncMock(return_value=True),
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

        with patch("klangk.wshandler.controllers.asyncio.create_task", _wrap):
            yield created
        for t in created:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    async def test_start_creates_proc_and_notifies(self):
        """start() spawns socat, records the proc/socket, notifies the client."""
        fwd, sock = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stdin = AsyncMock()
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
            async with self._track_tasks() as tasks:
                await fwd.start()
                # Let the forward_output task run to completion.
                for _ in range(5):
                    await asyncio.sleep(0)
        assert fwd.proc is mock_proc
        assert fwd.socket == "/tmp/klangk-ssh-agent-uid.sock"
        # start() must wire up the forward_output relay as a background task.
        assert any(
            t.get_coro().__qualname__ == "SshAgentForwarder.forward_output"
            for t in tasks
        )
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_started"
        assert msg["socket"] == "/tmp/klangk-ssh-agent-uid.sock"

    async def test_start_reaps_stale_relay_then_removes_socket(self):
        """start() reaps a leftover relay (``pkill -f <path>``) BEFORE removing
        the socket file, in that order — the crux of the #2001 fix."""
        fwd, sock = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stdin = AsyncMock()
        calls = []

        async def fake_exec(container_id, argv, **kw):
            calls.append(list(argv))
            return (0, "", "")

        with (
            patch.object(_mock_pod, "exec_container", new=fake_exec),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
        ):
            async with self._track_tasks():
                await fwd.start()
                for _ in range(5):
                    await asyncio.sleep(0)
        # pkill the stale relay first, then rm the socket file — in order.
        assert calls[0] == [
            "pkill",
            "-f",
            "UNIX-LISTEN:/tmp/klangk-ssh-agent-uid.sock",
        ]
        assert calls[1] == ["rm", "-f", "/tmp/klangk-ssh-agent-uid.sock"]

    async def test_start_tolerates_pkill_no_match(self):
        """pkill exits 1 when no stale relay exists (first connect on a clean
        container). ``exec_container`` is ``check=False``, so start() must
        tolerate that and still bind (#2001) — guards against a future flip
        to ``check=True`` silently breaking first-connect."""
        fwd, sock = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stdin = AsyncMock()
        # pkill returns (1, ...) "no processes matched"; rm returns (0, ...).
        results = iter([(1, "", "no processes matched"), (0, "", "")])

        async def fake_exec(container_id, argv, **kw):
            return next(results)

        with (
            patch.object(_mock_pod, "exec_container", new=fake_exec),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
        ):
            async with self._track_tasks():
                await fwd.start()
                for _ in range(5):
                    await asyncio.sleep(0)
        # First-connect (nothing to reap) still binds + records the socket.
        assert fwd.socket == "/tmp/klangk-ssh-agent-uid.sock"
        assert fwd.proc is mock_proc

    async def test_start_no_container_sends_error(self):
        fwd, sock = self._forwarder(container_id=None)
        await fwd.start()
        msg = sock.send_json.call_args[0][0]
        assert msg.get("type") == "error"
        assert fwd.proc is None

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

    async def test_forward_output_swallows_oserror(self):
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(side_effect=OSError("boom"))
        fwd.proc = mock_proc
        with patch("klangk.wshandler.controllers.logger") as lg:
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

    async def test_data_no_proc_noop(self):
        fwd, _ = self._forwarder()
        fwd.proc = None
        # No-op, must not raise.
        await fwd.data({"data": base64.b64encode(b"x").decode()})

    async def test_data_writes_to_stdin(self):
        data = base64.b64encode(b"agent-request").decode()
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        fwd.proc = mock_proc
        await fwd.data({"data": data})
        mock_proc.stdin.write.assert_called_once_with(b"agent-request")

    async def test_data_empty_payload_noop(self):
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        fwd.proc = mock_proc
        await fwd.data({"data": ""})
        mock_proc.stdin.write.assert_not_called()

    @pytest.mark.parametrize("data", ["!!!not-base64!!!", 5, None])
    async def test_data_invalid_base64_dropped(self, data):
        """#3071: invalid base64 (or a non-string data field) must be
        dropped, not raise out of the handler and kill the session."""
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        fwd.proc = mock_proc

        await fwd.data({"data": data})

        mock_proc.stdin.write.assert_not_called()

    async def test_data_dead_relay_write_broken_pipe(self):
        """#3071: a write to a dead socat relay (BrokenPipeError) is
        dropped and the relay is torn down, not propagated to kill the
        whole session — and later frames short-circuit on proc None."""
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock(side_effect=BrokenPipeError())
        mock_proc.stdin.drain = AsyncMock()
        fwd.proc = mock_proc

        with patch.object(fwd, "stop", new=AsyncMock()) as stop:
            await fwd.data({"data": base64.b64encode(b"x").decode()})

        mock_proc.stdin.drain.assert_not_awaited()
        stop.assert_awaited_once()

    async def test_data_dead_relay_drain_error(self):
        """#3071: drain() raising (dead relay / closed loop) tears the
        relay down too."""
        fwd, _ = self._forwarder()
        mock_proc = AsyncMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock(side_effect=RuntimeError("dead"))
        fwd.proc = mock_proc

        with patch.object(fwd, "stop", new=AsyncMock()) as stop:
            await fwd.data({"data": base64.b64encode(b"x").decode()})

        mock_proc.stdin.write.assert_called_once()
        stop.assert_awaited_once()

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
        # stop() reaps the in-container socat (pkill) then removes the socket
        # file (rm). The pkill is best-effort; the rm OSError is the one we
        # warn about.
        with patch.object(
            _mock_pod,
            "exec_container",
            new=AsyncMock(side_effect=[(0, "", ""), OSError("boom")]),
        ) as exec_mock:
            with patch("klangk.wshandler.controllers.logger") as lg:
                await fwd.stop()
        assert exec_mock.await_count == 2
        assert exec_mock.await_args_list[0].args[1] == [
            "pkill",
            "-f",
            "UNIX-LISTEN:/tmp/agent.sock",
        ]
        assert exec_mock.await_args_list[1].args[1] == [
            "rm",
            "-f",
            "/tmp/agent.sock",
        ]
        lg.warning.assert_called_once()
        assert "Failed to remove SSH agent socket" in str(lg.warning.call_args)
        assert fwd.socket is None

    async def test_stop_tolerates_pkill_failure(self):
        """The pkill reap is best-effort: if it raises (container already
        gone, podman launch error) stop() swallows it and still removes the
        socket file — teardown must not break on a cleanup failure."""
        fwd, _ = self._forwarder()
        fwd.socket = "/tmp/agent.sock"
        with patch.object(
            _mock_pod,
            "exec_container",
            new=AsyncMock(side_effect=[OSError("boom"), (0, "", "")]),
        ) as exec_mock:
            await fwd.stop()
        assert exec_mock.await_count == 2
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


class TestExecDispatch:
    async def test_dispatch_exec_start(self, user, app_state):
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

    async def test_dispatch_exec_input(self, user, app_state):
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

    async def test_dispatch_exec_stop(self, user, app_state):
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

    async def test_dispatch_exec_close_stdin(self, user, app_state):
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

    async def test_dispatch_heartbeat(self, user, app_state):
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


class TestHandleHeartbeat:
    async def test_records_activity(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
    async def test_dispatch_browser_response(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets

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

    async def test_handle_browser_response_resolves_future(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_handle_browser_response_wrong_sender_rejected(
        self, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_handle_browser_response_missing_id(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        # Should not raise
        sockets.handle_browser_response({})

    async def test_handle_browser_response_unknown_id(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        # Should not raise
        sockets.handle_browser_response({"id": "unknown"})

    async def test_dispatch_browser_request_no_subscribers(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        session = sockets.get_or_create_session("ws-empty", app_state)
        try:
            result = await session.dispatch_browser_request(
                {"action": "fetch", "url": "http://example.com"}
            )
            assert "error" in result
            assert "No browser client" in result["error"]
        finally:
            sockets.sessions.pop("ws-empty", None)

    async def test_dispatch_browser_request_cli_only(self, app_state):
        """CLI-only connections get immediate error, not 30s timeout."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_dispatch_browser_request_success(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_dispatch_browser_request_timeout(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
    async def test_success(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_dead_socket(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_timeout(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_cancelled_cleans_up(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
    async def test_cleanup_revokes_browser_registration(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
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
    async def test_noop_for_unknown_workspace(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        await reset_workspace_state(sockets, "ws-unknown")  # should not raise

    async def test_remove_session_noop_for_unknown(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        await sockets.remove_session("nonexistent")  # should not raise

    async def test_removes_session_with_no_subscribers(self, app_state):
        """remove_session acquires lock and removes empty session."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sockets.get_or_create_session("ws-reset-empty", app_state)
        assert "ws-reset-empty" in sockets.sessions
        registry.track_activity("cid-reset", "ws-reset-empty")
        try:
            await reset_workspace_state(sockets, "ws-reset-empty")
            assert "ws-reset-empty" not in sockets.sessions
        finally:
            sockets.sessions.pop("ws-reset-empty", None)
            registry.states.pop("ws-reset-empty", None)

    async def test_expected_container_id_guards_state(self, app_state):
        """#331: the reset chain threads the dead container id down to
        remove_state's re-bind guard — a workspace whose state was
        re-bound to a fresh container keeps it."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sockets.get_or_create_session("ws-reset-guard", app_state)
        registry.track_activity("cid-fresh", "ws-reset-guard")
        try:
            await reset_workspace_state(
                sockets,
                "ws-reset-guard",
                expected_container_id="cid-dead",
            )
            # Re-bound: the fresh container's state survives; the session
            # was still removed (a fresh workspace_connect recreates it).
            assert registry.states["ws-reset-guard"].container_id == (
                "cid-fresh"
            )
            # Matching id: the state is removed.
            await reset_workspace_state(
                sockets,
                "ws-reset-guard",
                expected_container_id="cid-fresh",
            )
            assert "ws-reset-guard" not in registry.states
        finally:
            sockets.sessions.pop("ws-reset-guard", None)
            registry.states.pop("ws-reset-guard", None)

    async def test_remove_session_skips_if_subscribers_reappear(
        self, app_state
    ):
        """remove_session re-checks subscribers under lock and aborts if non-empty."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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


class TestNotifyUserWorkspacesChanged:
    """notify_user_workspaces_changed sends to a user's connections only."""

    def _register(self, sock, user, app_state):
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        app_state.state.sockets.connections[sock] = conn
        return conn

    def test_sends_to_matching_user_only(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    def test_no_connections_is_noop(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        # Should not raise when the user has no active connections.
        sockets.notify_user_workspaces_changed("nobody")

    def test_dead_socket_is_pruned(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        from klangk.wshandler import WS_ERRORS

        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("dead"))
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            sockets.notify_user_workspaces_changed("uid-1")
            assert sock not in sockets.connections
        finally:
            sockets.connections.pop(sock, None)


class TestNotifyUserTerminalsChanged:
    """notify_user_terminals_changed nudges a user's status connections
    (e.g. the TUI's /ws feed) to re-fetch terminals (#1885)."""

    def _register(self, sock, user, app_state):
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        app_state.state.sockets.connections[sock] = conn
        return conn

    def test_sends_to_matching_user_with_workspace_id(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock_a = _mock_sock()
        sock_other = _mock_sock()
        try:
            self._register(sock_a, {"id": "uid-1", "email": "a@x"}, app_state)
            self._register(
                sock_other, {"id": "uid-2", "email": "c@x"}, app_state
            )
            sockets.notify_user_terminals_changed("uid-1", "ws-9")
        finally:
            sockets.connections.pop(sock_a, None)
            sockets.connections.pop(sock_other, None)
        sock_a.send_json.assert_called_once_with(
            {"type": "terminals_changed", "workspace_id": "ws-9"}
        )
        sock_other.send_json.assert_not_called()

    def test_includes_windows_when_provided(self, app_state):
        # A windows payload is included verbatim (push path); the key is
        # omitted entirely when None (above) so legacy consumers that test
        # `"windows" in event` aren't misled (#1896).
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        payload = [{"id": "@0", "index": 0, "name": "bash"}]
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            sockets.notify_user_terminals_changed("uid-1", "ws-9", payload)
        finally:
            sockets.connections.pop(sock, None)
        sock.send_json.assert_called_once_with(
            {
                "type": "terminals_changed",
                "workspace_id": "ws-9",
                "windows": payload,
            }
        )

    def test_no_connections_is_noop(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sockets.notify_user_terminals_changed("nobody", "ws-9")  # no raise

    def test_dead_socket_is_pruned(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        from klangk.wshandler import WS_ERRORS

        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("dead"))
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            sockets.notify_user_terminals_changed("uid-1", "ws-9")
            assert sock not in sockets.connections
        finally:
            sockets.connections.pop(sock, None)


class TestNotifyContainerStatus:
    """notify_container_status broadcasts to workspace members only (#1714)."""

    def _register(self, sock, user, app_state):
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        app_state.state.sockets.connections[sock] = conn
        return conn

    async def test_sends_to_members_of_both_users(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock_a = _mock_sock()
        sock_b = _mock_sock()
        try:
            self._register(sock_a, {"id": "uid-1", "email": "a@x"}, app_state)
            self._register(sock_b, {"id": "uid-2", "email": "b@x"}, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-123")
            await _grant_monitor(app_state, "uid-2", "ws-123")
            await sockets.notify_container_status("ws-123", True)
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

    async def test_non_member_receives_nothing(self, app_state):
        """#1714: a connected user with no grant on the workspace is skipped."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        member = _mock_sock()
        stranger = _mock_sock()
        try:
            self._register(member, {"id": "uid-1", "email": "a@x"}, app_state)
            self._register(
                stranger, {"id": "uid-2", "email": "b@x"}, app_state
            )
            await _grant_monitor(app_state, "uid-1", "ws-123")
            await sockets.notify_container_status("ws-123", True)
        finally:
            sockets.connections.pop(member, None)
            sockets.connections.pop(stranger, None)
        member.send_json.assert_called_once()
        stranger.send_json.assert_not_called()

    async def test_view_only_grant_is_not_membership(self, app_state):
        """The ``/``-level view-for-authenticated ACE must not leak frames.

        Seeds the deployment default (view at ``/`` for authenticated)
        and asserts it still does not count as membership — only a real
        grant on the workspace does.
        """
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            await app_state.state.model.init_db()
            await app_state.state.model.acl.add_acl_entry(
                "/",
                0,
                model.ACTION_ALLOW,
                "view",
                model.PRINCIPAL_SYSTEM,
                system_principal=model.SYSTEM_AUTHENTICATED,
            )
            await sockets.notify_container_status("ws-123", True)
        finally:
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    async def test_same_users_two_connections_both_receive(self, app_state):
        """All connections of an allowed user receive the frame."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock_a = _mock_sock()
        sock_b = _mock_sock()
        try:
            self._register(sock_a, {"id": "uid-1", "email": "a@x"}, app_state)
            self._register(sock_b, {"id": "uid-1", "email": "a@x"}, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-123")
            await sockets.notify_container_status("ws-123", True)
        finally:
            sockets.connections.pop(sock_a, None)
            sockets.connections.pop(sock_b, None)
        sock_a.send_json.assert_called_once()
        sock_b.send_json.assert_called_once()

    async def test_includes_service_started_at(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-789")
            await sockets.notify_container_status("ws-789", True, 1000.0)
        finally:
            sockets.connections.pop(sock, None)
        msg = sock.send_json.call_args[0][0]
        assert msg["service_started_at"] == 1000.0
        assert msg["running"] is True

    async def test_sends_stopped(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-456")
            await sockets.notify_container_status("ws-456", False)
        finally:
            sockets.connections.pop(sock, None)
        msg = sock.send_json.call_args[0][0]
        assert msg["running"] is False
        assert msg["workspace_id"] == "ws-456"

    async def test_skips_unauthenticated(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        try:
            self._register(sock, {"id": None, "email": ""}, app_state)
            await sockets.notify_container_status("ws-1", True)
        finally:
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    async def test_dead_member_socket_is_pruned(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        from klangk.wshandler import WS_ERRORS

        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("dead"))
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-1")
            await sockets.notify_container_status("ws-1", True)
            assert sock not in sockets.connections
        finally:
            sockets.connections.pop(sock, None)


class TestNotifyServiceHealth:
    """notify_service_health fans health events out to workspace members."""

    def _register(self, sock, user, app_state):
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        app_state.state.sockets.connections[sock] = conn
        return conn

    async def test_sends_healthy_to_members(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock_a = _mock_sock()
        sock_b = _mock_sock()
        try:
            self._register(sock_a, {"id": "uid-1", "email": "a@x"}, app_state)
            self._register(sock_b, {"id": "uid-2", "email": "b@x"}, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-123")
            await _grant_monitor(app_state, "uid-2", "ws-123")
            await sockets.notify_service_health("ws-123", healthy=True)
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

    async def test_non_member_receives_nothing(self, app_state):
        """#1714: another tenant never sees health frames."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        member = _mock_sock()
        stranger = _mock_sock()
        try:
            self._register(member, {"id": "uid-1", "email": "a@x"}, app_state)
            self._register(
                stranger, {"id": "uid-2", "email": "b@x"}, app_state
            )
            await _grant_monitor(app_state, "uid-1", "ws-123")
            await sockets.notify_service_health(
                "ws-123", healthy=False, message="boom"
            )
        finally:
            sockets.connections.pop(member, None)
            sockets.connections.pop(stranger, None)
        member.send_json.assert_called_once()
        stranger.send_json.assert_not_called()

    async def test_monitor_without_terminal_receives_frames(self, app_state):
        """#2783: ``monitor`` alone — without ``terminal`` — is the status
        gate; a monitoring-only member observes health without being
        able to open a terminal."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-123")
            await sockets.notify_service_health("ws-123", healthy=False)
        finally:
            sockets.connections.pop(sock, None)
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "service_health"
        assert msg["workspace_id"] == "ws-123"

    async def test_terminal_without_monitor_receives_nothing(self, app_state):
        """The two permissions are distinct: ``terminal`` alone no longer
        grants status reception (grant ``monitor`` too, or rely on the
        seeded/migrated pairing)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            await app_state.state.model.init_db()
            async with app_state.state.db.transaction() as tx:
                await tx.execute(
                    "INSERT OR IGNORE INTO users (id, email, verified)"
                    " VALUES (?, ?, 1)",
                    ("uid-1", "uid-1@test.example"),
                )
            await app_state.state.model.acl.add_acl_entry(
                "/workspaces/ws-123",
                0,
                model.ACTION_ALLOW,
                "terminal",
                model.PRINCIPAL_USER,
                user_id="uid-1",
            )
            await sockets.notify_service_health("ws-123", healthy=False)
        finally:
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    async def test_sends_unhealthy_with_reason(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        # The failure reason rides along on the broadcast so operators
        # can see *why* it's unhealthy (#1088).
        sock = _mock_sock()
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-9")
            await sockets.notify_service_health(
                "ws-9", healthy=False, message="curl: connection refused"
            )
        finally:
            sockets.connections.pop(sock, None)
        msg = sock.send_json.call_args[0][0]
        assert msg["healthy"] is False
        assert msg["type"] == "service_health"
        assert msg["health_message"] == "curl: connection refused"

    async def test_skips_unauthenticated(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        try:
            self._register(sock, {"id": None, "email": ""}, app_state)
            await sockets.notify_service_health("ws-1", healthy=True)
        finally:
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    async def test_dead_member_socket_is_pruned(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        from klangk.wshandler import WS_ERRORS

        sock = _mock_sock()
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("dead"))
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-1")
            await sockets.notify_service_health("ws-1", healthy=True)
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

    def _register(self, sock, app_state, user_id="uid-1"):
        conn = _base_conn(
            user={"id": user_id, "email": "a@x"}, ws=sock, app_state=app_state
        )
        app_state.state.sockets.connections[sock] = conn
        return conn

    async def test_replays_only_checked_member_workspaces(self, app_state):
        """Healthy + unhealthy are sent; unchecked, no-check, and
        non-member workspaces are skipped (#1714)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
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
            # another tenant's workspace: checked, but not granted
            registry.states["ws-foreign"] = self._state(
                "ws-foreign",
                registry=registry,
                health_check="true",
                health_status="healthy",
            )
            self._register(sock, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-healthy")
            await _grant_monitor(app_state, "uid-1", "ws-sick")
            await sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
            sockets.connections.pop(sock, None)

        frames = [c[0][0] for c in sock.send_json.call_args_list]
        assert len(frames) == 2
        by_ws = {f["workspace_id"]: f for f in frames}
        assert by_ws["ws-healthy"]["healthy"] is True
        assert by_ws["ws-healthy"]["health_message"] is None
        assert by_ws["ws-sick"]["healthy"] is False
        assert by_ws["ws-sick"]["health_message"] == "conn refused"
        assert "ws-unchecked" not in by_ws
        assert "ws-nocheck" not in by_ws
        assert "ws-foreign" not in by_ws
        for f in frames:
            assert f["type"] == "service_health"

    async def test_unregistered_socket_gets_nothing(self, app_state):
        """A socket with no registered connection has no user to scope by."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        saved = dict(registry.states)
        sock = _mock_sock()
        try:
            registry.states.clear()
            registry.states["ws-1"] = self._state(
                "ws-1",
                registry=registry,
                health_check="true",
                health_status="healthy",
            )
            await sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
        sock.send_json.assert_not_called()

    async def test_targets_only_the_given_socket(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
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
            self._register(sock, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-1")
            await sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
            sockets.connections.pop(sock, None)
        sock.send_json.assert_called_once()
        other.send_json.assert_not_called()

    async def test_dead_socket_breaks_cleanly(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        from klangk.wshandler import WS_ERRORS

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
            self._register(sock, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-1")
            await _grant_monitor(app_state, "uid-1", "ws-2")
            # Must not raise; the dead socket ends the snapshot early.
            await sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
            sockets.connections.pop(sock, None)

    async def test_empty_registry_sends_nothing(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        saved = dict(registry.states)
        sock = _mock_sock()
        try:
            registry.states.clear()
            self._register(sock, app_state)
            await sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    async def test_state_dropped_during_acl_pass_is_not_replayed(
        self, app_state
    ):
        """#1714 review: a container dying while the snapshot's ACL pass is
        in flight must not be resurrected by a stale running=true frame —
        its terminal death frame already went to this member."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        saved = dict(registry.states)
        sock = _mock_sock()
        real = app_state.state.acl.permissions_for_resources

        async def dying_during_acl(resources, principals, permissions):
            # The container dies between the candidate snapshot and the
            # sends: remove_state pops the registry entry.
            registry.states.pop("ws-1", None)
            return await real(resources, principals, permissions)

        try:
            registry.states.clear()
            registry.states["ws-1"] = self._state(
                "ws-1",
                registry=registry,
                health_check="true",
                health_status="healthy",
            )
            self._register(sock, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-1")
            with patch.object(
                app_state.state.acl,
                "permissions_for_resources",
                new=dying_during_acl,
            ):
                await sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    async def test_transition_during_acl_pass_is_not_replayed(self, app_state):
        """#1714 review: a health transition firing mid-snapshot bumps seq;
        replaying the older status after the newer delta would flip the
        client backwards, so the stale frame is dropped."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        saved = dict(registry.states)
        sock = _mock_sock()
        st = self._state(
            "ws-1",
            registry=registry,
            health_check="true",
            health_status="healthy",
        )
        real = app_state.state.acl.permissions_for_resources

        async def transition_during_acl(resources, principals, permissions):
            # A transition lands between the candidate snapshot and the
            # sends: same state object, seq bumped.
            st.health_seq += 1
            return await real(resources, principals, permissions)

        try:
            registry.states.clear()
            registry.states["ws-1"] = st
            self._register(sock, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-1")
            with patch.object(
                app_state.state.acl,
                "permissions_for_resources",
                new=transition_during_acl,
            ):
                await sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()


class TestServiceHealthFrame:
    """service_health_frame: the additive contract fields (#1175)."""

    def test_defaults_preserve_legacy_shape(self):
        # Only the required healthy/message need to be supplied; the new
        # fields default so an old-style caller produces a superset of
        # the legacy frame (additive, non-breaking).
        from klangk.wshandler.session import service_health_frame

        out = service_health_frame("ws-1", healthy=True, message=None)
        assert out["type"] == "service_health"
        assert out["workspace_id"] == "ws-1"
        assert out["healthy"] is True
        assert out["health_message"] is None
        assert out["running"] is True
        assert out["health_checked_at"] is None
        assert out["seq"] == 0

    def test_health_checked_at_serialized_as_iso(self):
        from klangk.wshandler.session import (
            service_health_frame,
            iso_utc,
        )

        # A known epoch renders as a fixed ISO-8601 UTC string.
        ts = 1_700_000_000.0
        assert iso_utc(ts) == "2023-11-14T22:13:20+00:00"
        assert iso_utc(None) is None
        out = service_health_frame(
            "ws-1", healthy=False, message="x", health_checked_at=ts
        )
        assert out["health_checked_at"] == "2023-11-14T22:13:20+00:00"

    def test_running_false_and_seq_forwarded(self):
        from klangk.wshandler.session import service_health_frame

        out = service_health_frame(
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
        app_state.state.sockets.connections[sock] = conn
        return conn

    async def test_forwards_death_frame_fields(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        # A container-death call passes running=False + a seq; the frame
        # a subscriber receives carries them (#1175 items 2, 4).
        sock = _mock_sock()
        try:
            self._register(sock, {"id": "uid-1", "email": "a@x"}, app_state)
            await _grant_monitor(app_state, "uid-1", "ws-9")
            await sockets.notify_service_health(
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

    async def test_snapshot_frame_carries_live_fields(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        saved = dict(registry.states)
        sock = _mock_sock()
        try:
            registry.states.clear()
            registry.states["ws-1"] = self._state(
                "ws-1", registry=registry, checked_at=1_700_000_000.0, seq=5
            )
            conn = _base_conn(
                user={"id": "uid-1", "email": "a@x"},
                ws=sock,
                app_state=app_state,
            )
            sockets.connections[sock] = conn
            await _grant_monitor(app_state, "uid-1", "ws-1")
            await sockets.send_service_health_snapshot(sock)
        finally:
            registry.states.clear()
            registry.states.update(saved)
            sockets.connections.pop(sock, None)
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
        app_state.state.sockets.connections[sock] = conn
        return conn

    def _frame(self, sock):
        return sock.send_json.call_args[0][0]

    def test_only_opted_in_connections_receive_it(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    def test_skips_unauthenticated(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        try:
            self._register(
                sock, {"id": None, "email": ""}, app_state, wants=True
            )
            sockets.send_health_heartbeats()
        finally:
            sockets.connections.pop(sock, None)
        sock.send_json.assert_not_called()

    def test_dead_opted_in_socket_is_pruned(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        from klangk.wshandler import WS_ERRORS

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

    def test_subscribe_command_toggles_flag(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    def test_subscribe_unknown_socket_is_noop(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        # Not registered -- must not raise.
        sockets.handle_subscribe_health_heartbeat({}, sock)


class TestRemoveSessionLocked:
    async def test_removes_session(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        session = sockets.get_or_create_session("ws-locked-rm", app_state)
        try:
            async with session.lock:
                await sockets.remove_session_locked(session)
            assert "ws-locked-rm" not in sockets.sessions
        finally:
            sockets.sessions.pop("ws-locked-rm", None)

    async def test_keeps_session_with_subscribers(self, app_state):
        """The locked variant re-checks subscribers too (#3070): a
        session someone re-attached to under the lock is not popped."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        session = sockets.get_or_create_session("ws-locked-sub", app_state)
        try:
            async with session.lock:
                session.subscribers.add(_mock_sock())
                await sockets.remove_session_locked(session)
            assert "ws-locked-sub" in sockets.sessions
        finally:
            session.subscribers.clear()
            sockets.sessions.pop("ws-locked-sub", None)

    async def test_skips_moved_on_mapping(self, app_state):
        """The locked variant re-checks mapping identity too (#3070): a
        caller holding a stale session's lock must not pop the slot a
        replacement session now owns."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        session = sockets.get_or_create_session("ws-locked-moved", app_state)
        replacement = WorkspaceSession("ws-locked-moved", app_state)
        try:
            async with session.lock:
                sockets.sessions["ws-locked-moved"] = replacement
                await sockets.remove_session_locked(session)
            assert sockets.sessions["ws-locked-moved"] is replacement
        finally:
            sockets.sessions.pop("ws-locked-moved", None)


class TestGetOrCreateSessionAtomicity:
    async def test_returns_same_session_for_same_workspace(self, app_state):
        """Concurrent calls return the same WorkspaceSession, not duplicates."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sockets.sessions.pop("ws-atomic", None)
        try:
            s1 = sockets.get_or_create_session("ws-atomic", app_state)
            s2 = sockets.get_or_create_session("ws-atomic", app_state)
            assert s1 is s2
        finally:
            sockets.sessions.pop("ws-atomic", None)

    async def test_concurrent_get_or_create_via_gather(self, app_state):
        """Two coroutines that both call get_or_create_session end up
        with the identical session object (no orphaned duplicates)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
    async def test_new_subscriber_not_lost_during_cleanup(self, app_state):
        """A subscriber added under the lock while cleanup runs is not lost."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_concurrent_cleanup_and_add(self, app_state):
        """When cleanup holds the lock, a concurrent add waits and is not lost."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        await conn1.cleanup()

        # Session should still exist because sock2 is still subscribed
        assert "ws-conc" in sockets.sessions
        assert sock2 in session.subscribers
        assert sock1 not in session.subscribers

        sockets.sessions.pop("ws-conc", None)


class TestSessionOrphanRace:
    """#3070: ``add_subscriber`` must never attach to a session that a
    racing last-disconnect ``remove_session`` already popped from the
    registry — such a subscriber misses every later workspace broadcast
    (``get_session`` resolves a different session) and the orphan's
    token-renewal task and window watcher leak for the process life."""

    async def test_popped_session_reclaims_its_slot(self, app_state):
        """The #3070 interleaving: connection B resolves the session,
        then A's last-disconnect ``remove_subscriber`` +
        ``remove_session`` run to completion (pop + reset) before B's
        ``add_subscriber`` acquires the lock — B's session reclaims the
        empty slot instead of taking B as an orphan subscriber."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock_a = _mock_sock()
        session = sockets.get_or_create_session("ws-3070-reclaim", app_state)
        with patch.object(
            WorkspaceSession, "start_window_sync", lambda s: None
        ):
            await session.add_subscriber(sock_a, "cid")

            # Connection A (last disconnect) runs to completion.
            assert await session.remove_subscriber(sock_a) is True
            await sockets.remove_session("ws-3070-reclaim")
            assert "ws-3070-reclaim" not in sockets.sessions

            # Connection B's add finally acquires the lock — with a
            # token expiry, so the reclaimed session re-arms renewal.
            sock_b = _mock_sock()
            expiry = datetime.now(timezone.utc) + timedelta(hours=12)
            await session.add_subscriber(sock_b, "cid", token_expiry=expiry)

        assert sockets.sessions["ws-3070-reclaim"] is session
        assert sock_b in session.subscribers
        # The leak property: renewal re-established on the reclaimed
        # session, then torn down (task cancelled and cleared) by B's
        # own disconnect — pre-fix the orphan's task ran forever.
        assert session._token_renewal_task is not None

        # And B's own disconnect still finds the mapped session and
        # tears it down — pre-fix, cleanup resolved nothing, so the
        # orphan's token-renewal task and window watcher leaked.
        assert await session.remove_subscriber(sock_b) is True
        await sockets.remove_session("ws-3070-reclaim")
        assert "ws-3070-reclaim" not in sockets.sessions
        assert session._token_renewal_task is None

    async def test_reclaim_restarts_window_watcher(self, app_state):
        """A reclaimed session is fresh for the watcher logic: reset()
        stopped the old watcher, so the re-attached subscriber's
        add_subscriber builds a new one instead of reusing the dead
        field (the #3015 path reads ``_window_watcher is None``)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock_a, sock_b = _mock_sock(), _mock_sock()
        session = sockets.get_or_create_session("ws-3070-watch", app_state)
        with patch("klangk.wshandler.session.WindowEventWatcher") as wc:
            wc.return_value.start = AsyncMock()
            wc.return_value.stop = AsyncMock()

            await session.add_subscriber(sock_a, "cid")  # watcher #1
            assert await session.remove_subscriber(sock_a) is True
            await sockets.remove_session("ws-3070-watch")  # pop + reset

            await session.add_subscriber(sock_b, "cid")  # reclaim

            # A fresh watcher was built for the reclaimed session.
            assert wc.call_count == 2
            assert session._window_watcher is wc.return_value

            # Final teardown stops it and leaves no watcher behind.
            assert await session.remove_subscriber(sock_b) is True
            await sockets.remove_session("ws-3070-watch")
            for _ in range(3):
                await asyncio.sleep(0)  # drain the spawned stop task
            assert session._window_watcher is None
            assert wc.return_value.stop.await_count == 2

    async def test_superseded_session_routes_to_replacement(self, app_state):
        """When a replacement session was created in the pop→add gap,
        the stale reference must not attach: the subscriber is routed
        to the mapped replacement instead."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        session = sockets.get_or_create_session("ws-3070-swap", app_state)
        # The racing last-disconnect pop, then a replacement connection
        # creating a fresh session before B's add runs.
        sockets.sessions.pop("ws-3070-swap")
        await session.reset()
        replacement = sockets.get_or_create_session("ws-3070-swap", app_state)

        sock = _mock_sock()
        with patch.object(
            WorkspaceSession, "start_window_sync", lambda s: None
        ):
            await session.add_subscriber(sock, "cid")

        assert sock not in session.subscribers
        assert sock in replacement.subscribers
        assert sockets.sessions["ws-3070-swap"] is replacement

    async def test_registryless_session_attaches_directly(self, app_state):
        """A bare session (``app=None``, no registry to verify against)
        keeps the direct attach."""
        sock = _mock_sock()
        session = WorkspaceSession("ws-3070-noapp")
        with patch.object(
            WorkspaceSession, "start_window_sync", lambda s: None
        ):
            await session.add_subscriber(sock, "cid")
        assert sock in session.subscribers

    async def test_remove_session_skips_moved_on_mapping(self, app_state):
        """#3070: when the mapping moves while ``remove_session`` waits
        for the lock, the mapped replacement owns its lifecycle — the
        stale remover must not pop or reset it."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        session = sockets.get_or_create_session("ws-3070-moved", app_state)

        with patch.object(WorkspaceSession, "reset", new_callable=AsyncMock):
            async with session.lock:
                remover = asyncio.create_task(
                    sockets.remove_session("ws-3070-moved")
                )
                await asyncio.sleep(0)  # remover queues on the lock
                # The slot moves while it waits (reclaimed/replaced).
                replacement = WorkspaceSession("ws-3070-moved", app_state)
                sockets.sessions["ws-3070-moved"] = replacement
            await remover

            assert sockets.sessions["ws-3070-moved"] is replacement
            WorkspaceSession.reset.assert_not_awaited()


class TestWsDebugLogging:
    async def test_recv_logged_when_debug(self, user, monkeypatch, app_state):
        app_state = _make_app_state()

        monkeypatch.setattr(wshandler.support, "WS_DEBUG", True)
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
        monkeypatch.setattr(wshandler.support, "WS_DEBUG", True)
        sock = _mock_sock()
        send_error(sock, "test error")
        sock.send_json.assert_called_once()

    async def test_broadcast_sends_to_subscribers(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        session = sockets.get_or_create_session("ws-bcast", app_state)
        mock_sock = _mock_sock()
        session.subscribers.add(mock_sock)
        try:
            delivered = session.broadcast({"type": "test"})
            assert delivered == 1
        finally:
            sockets.sessions.pop("ws-bcast", None)

    async def test_broadcast_to_browsers_sends_to_browser_subscribers(
        self, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        with patch.object(_ws_support, "WS_DEBUG", True):
            log_ws_msg(
                "RECV",
                {"type": "terminal_output", "data": "x" * 200},
                {"email": "test@example.com"},
            )

    def test_terminal_input_truncated(self):
        with patch.object(_ws_support, "WS_DEBUG", True):
            log_ws_msg(
                "SEND",
                {"type": "terminal_input", "data": "y" * 50},
            )

    def test_other_message(self):
        with patch.object(_ws_support, "WS_DEBUG", True):
            log_ws_msg("RECV", {"type": "heartbeat"})

    def test_other_message_with_user(self):
        with patch.object(_ws_support, "WS_DEBUG", True):
            log_ws_msg(
                "RECV",
                {"cmd": "workspace_connect", "workspaceId": "ws-1"},
                {"email": "test@example.com"},
            )

    def test_noop_when_debug_disabled(self):
        with patch.object(_ws_support, "WS_DEBUG", False):
            log_ws_msg("RECV", {"type": "heartbeat"})


class TestBroadcastDeadSubscribers:
    async def test_dead_subscriber_removed(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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


class TestBroadcastEvent:
    """#3008: broadcast_event fans a CUSTOM event out to a workspace
    session, with a direct-send fallback so the acting connection is
    covered even when it isn't a subscriber (no session — e.g. a
    unit-test wiring — or an out-of-session socket)."""

    def _names(self, sock):
        return [
            c[0][0].get("event", {}).get("name")
            for c in sock.send_json.call_args_list
            if isinstance(c[0][0], dict) and c[0][0].get("type") == "event"
        ]

    def test_no_session_direct_send(self):
        sock = _mock_sock()
        broadcast_event(None, sock, "container_restart", "Restarting...")
        assert self._names(sock) == ["container_restart"]

    def test_subscribed_socket_gets_exactly_one_copy(self):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        session = sockets.get_or_create_session("ws-bcast", app_state)
        sock = _mock_sock()
        sibling = _mock_sock()
        # Direct set adds (not add_subscriber) so no window watcher
        # task spawns against the mock podman.
        session.subscribers.add(sock)
        session.subscribers.add(sibling)
        try:
            broadcast_event(session, sock, "container_ready", "ready")
            assert self._names(sock) == ["container_ready"]
            assert self._names(sibling) == ["container_ready"]
        finally:
            sockets.sessions.pop("ws-bcast", None)

    def test_out_of_session_socket_gets_direct_send(self):
        """The acting socket is not a subscriber: it still gets the event
        (direct send) alongside the broadcast to real subscribers."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        session = sockets.get_or_create_session("ws-bcast-2", app_state)
        acting = _mock_sock()
        sibling = _mock_sock()
        session.subscribers.add(sibling)
        try:
            broadcast_event(
                session, acting, "container_restart", "Restarting..."
            )
            assert self._names(acting) == ["container_restart"]
            assert self._names(sibling) == ["container_restart"]
        finally:
            sockets.sessions.pop("ws-bcast-2", None)

    def test_failed_acting_socket_send_is_swallowed_and_logged(self, caplog):
        """A subscribed acting socket whose send fails is pruned by the
        broadcast and gets zero delivered copies — the failure must not
        propagate (a slow/dead acting client must not abort the restart)
        and no fallback re-send is attempted, but the drop is logged for
        diagnosis (#3014 review)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        session = sockets.get_or_create_session("ws-bcast-3", app_state)
        acting = _mock_sock()
        acting.send_json = MagicMock(side_effect=RuntimeError("ws closed"))
        sibling = _mock_sock()
        session.subscribers.add(acting)
        session.subscribers.add(sibling)
        try:
            with caplog.at_level(logging.WARNING):
                broadcast_event(session, acting, "container_ready", "ready")
            # One send attempt (the broadcast) — no fallback double-send.
            assert acting.send_json.call_count == 1
            # The failed socket was pruned; the sibling was delivered to.
            assert acting not in session.subscribers
            assert self._names(sibling) == ["container_ready"]
            assert any(
                "container_ready" in r.getMessage()
                and "pruned" in r.getMessage()
                for r in caplog.records
            )
        finally:
            sockets.sessions.pop("ws-bcast-3", None)


class TestHandleRestartContainer:
    async def test_restart_not_connected(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        await conn.handle_restart_container()
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any("Not connected" in str(c) for c in calls)

    async def test_restart_no_terminal_perm(self):
        """A spectator (no terminal perm) must not restart the container,
        and must not trigger cleanup or container (re)start side effects."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.workspace_id = "ws-noadmin"
        with (
            patch.object(conn, "has_perm", new=AsyncMock(return_value=False)),
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
        # #2891: machine-readable refusal code for the client.
        assert any(
            isinstance(m, dict) and m.get("code") == "forbidden" for m in sent
        )
        # No destructive side effects: nothing torn down or (re)started.
        mock_cleanup.assert_not_called()
        mock_start.assert_not_called()

    async def test_restart_deny_leaves_other_connections_untouched(
        self, user, app_state
    ):
        """A spectator's denied restart (no terminal perm) must not
        change other users' container_id or disrupt their session (#873)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
            # conn1 is a spectator: terminal denied.
            with (
                patch.object(
                    conn1, "has_perm", new=AsyncMock(return_value=False)
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
        registry = app_state.state.container_registry
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

    async def test_restart_workspace_gone(self, user, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-gone"
        conn.container_id = "cid-gone"
        conn.workspace = None
        # "ws-gone" is not a real workspace; grant admin so the perm
        # gate passes and we reach the "not found" path under test.
        conn.has_perm = AsyncMock(return_value=True)

        with (
            patch.object(
                app_state.state.workspaces,
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
        # #2891: machine-readable refusal code for the client.
        assert any(
            isinstance(c, dict) and c.get("code") == "not_found" for c in calls
        )

    async def test_restart_reads_workspace_fresh_from_db(
        self, user, app_state
    ):
        """#2676: restart must not trust the connection's cached workspace
        dict. After an unclean host restart the cached container_id can be
        stale, sending the restart down the create path (sidecar collision)
        instead of the reuse path a reconnect takes 3 seconds later. The
        DB row is the source of truth for container_id."""
        app_state = _make_app_state()
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "restart-fresh"
        )
        fresh = {**workspace, "container_id": "cid-fresh"}
        stale = {**workspace, "container_id": "cid-stale"}
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid-stale"
        conn.workspace = stale
        conn.has_perm = AsyncMock(return_value=True)

        started: list[tuple[str, dict]] = []

        async def fake_start(wid, ws_obj):
            started.append((wid, ws_obj))
            conn.container_id = "cid-fresh"
            conn.workspace_id = wid

        with (
            patch.object(
                app_state.state.workspaces,
                "get_workspace",
                AsyncMock(return_value=fresh),
            ) as mock_get,
            patch.object(
                Connection,
                "start_workspace_container",
                side_effect=fake_start,
            ),
            patch.object(Connection, "cleanup", new_callable=AsyncMock),
            patch.object(
                app_state.state.container_registry, "record_activity"
            ),
            patch.object(
                app_state.state.container_registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            await conn.handle_restart_container()

        # Read fresh (not the cached stale dict) and started with it.
        mock_get.assert_awaited_once_with(workspace["id"])
        assert started == [(workspace["id"], fresh)]

    async def test_restart_podman_error_sends_error_frame(
        self, user, app_state
    ):
        """#2676: a failed (re)start must not drop the WebSocket with a
        traceback — the error frame keeps the session alive and surfaces
        the actionable podman message."""
        from klangk.podman import PodmanError

        app_state = _make_app_state()
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "restart-podman-err"
        )
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid-old"
        conn.workspace = workspace
        conn.has_perm = AsyncMock(return_value=True)

        with (
            patch.object(Connection, "cleanup", new_callable=AsyncMock),
            patch.object(
                Connection,
                "start_workspace_container",
                AsyncMock(
                    side_effect=PodmanError(
                        500,
                        "cannot remove the existing network sidecar "
                        "for workspace abcd1234",
                    )
                ),
            ),
        ):
            # Must not raise (which would drop the WS in dispatch).
            await conn.handle_restart_container()

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        errors = [
            c
            for c in calls
            if isinstance(c, dict) and c.get("type") == "error"
        ]
        assert len(errors) == 1
        assert "Container restart failed" in errors[0]["message"]
        assert "network sidecar" in errors[0]["message"]

    async def test_restart_capacity_refusal_error_frame(self, user, app_state):
        """#2525: an admission-control refusal on the WS restart path is
        a clear error frame (same as the API's 503), not a drop."""
        from klangk.exceptions import WorkspaceCapacityError

        app_state = _make_app_state()
        registry = app_state.state.container_registry
        sock = _mock_sock(headers={"host": "localhost:8997"})
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "restart-cap"
        )
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid-cap"
        conn.workspace = workspace

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                AsyncMock(
                    side_effect=WorkspaceCapacityError(
                        "workspace quota reached: 2 of this user's "
                        "workspaces are already running and the server "
                        "caps it at 2 "
                        "(KLANGKD_MAX_RUNNING_WORKSPACES_PER_USER). "
                        "Stop a workspace first, or ask the operator to "
                        "raise the cap."
                    )
                ),
            ),
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
        ):
            await conn.handle_restart_container()

        calls = [c[0][0] for c in sock.send_json.call_args_list]
        errors = [
            c
            for c in calls
            if isinstance(c, dict) and c.get("type") == "error"
        ]
        assert len(errors) == 1
        assert "quota" in errors[0]["message"]
        assert errors[0]["code"] == "capacity"
        sock.close.assert_not_awaited()

    async def test_restart_fractional_timeout(
        self, user, monkeypatch, app_state
    ):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        monkeypatch.setattr(
            app_state.state.container_registry, "idle_timeout_seconds", 90
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
        registry = app_state.state.container_registry
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
        registry = app_state.state.container_registry
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
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
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

    async def test_restart_notifies_sibling_connections(self, user, app_state):
        """#3008: restart lifecycle events reach every connection in the
        workspace, not only the restarting one — a sibling's page recovers
        on the broadcast container_ready (overlay clear + terminal
        re-start) instead of its terminal going dead."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock1 = _mock_sock(headers={"host": "localhost:8997"})
        sock2 = _mock_sock()
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "restart-fanout"
        )
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)
        conn2 = _base_conn(user=user, ws=sock2, app_state=app_state)
        conn1.workspace_id = workspace["id"]
        conn1.container_id = "old-cid"
        conn1.workspace = workspace
        conn2.workspace_id = workspace["id"]
        conn2.container_id = "old-cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        await session.add_subscriber(sock1, "old-cid")
        await session.add_subscriber(sock2, "old-cid")
        sockets.connections[sock1] = conn1
        sockets.connections[sock2] = conn2

        async def fake_start(self_arg, wid, ws_obj):
            self_arg.container_id = "new-cid"
            self_arg.workspace_id = wid
            # start_workspace_container re-subscribes the restarting
            # socket (the real path calls add_subscriber).
            await session.add_subscriber(sock1, "new-cid")

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                autospec=True,
                side_effect=fake_start,
            ),
            # add_subscriber spawns the tmux window watcher against the
            # mock podman; suppress it (not what this test exercises).
            patch.object(
                WorkspaceSession, "start_window_sync", lambda s: None
            ),
            patch.object(registry, "record_activity"),
            patch.object(
                registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            await conn1.handle_restart_container()

        def custom_names(sock):
            return [
                c[0][0].get("event", {}).get("name")
                for c in sock.send_json.call_args_list
                if isinstance(c[0][0], dict)
                and c[0][0].get("type") == "event"
                and c[0][0].get("event", {}).get("type") == "CUSTOM"
            ]

        # The restarting client gets each notice exactly once (never a
        # broadcast + direct-send double) …
        assert custom_names(sock1).count("container_restart") == 1
        assert custom_names(sock1).count("container_ready") == 1
        # … and the sibling gets both lifecycle events too.
        assert custom_names(sock2).count("container_restart") == 1
        assert custom_names(sock2).count("container_ready") == 1

        await session.remove_subscriber(sock1)
        await session.remove_subscriber(sock2)
        sockets.connections.pop(sock1, None)
        sockets.connections.pop(sock2, None)
        sockets.sessions.pop(workspace["id"], None)

    async def test_restart_replaces_dead_window_watcher(self, user, app_state):
        """#3015: the session's tmux window watcher is bound to one
        container; a restart that recycles the container (with a sibling
        still subscribed, so the session survives) must replace the
        watcher instead of no-oping on the stale field forever."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock1 = _mock_sock(headers={"host": "localhost:8997"})
        sock2 = _mock_sock()
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "restart-watcher"
        )
        conn1 = _base_conn(user=user, ws=sock1, app_state=app_state)
        conn2 = _base_conn(user=user, ws=sock2, app_state=app_state)
        conn1.workspace_id = workspace["id"]
        conn1.container_id = "old-cid"
        conn1.workspace = workspace
        conn2.workspace_id = workspace["id"]
        conn2.container_id = "old-cid"
        session = sockets.get_or_create_session(workspace["id"], app_state)
        # Seed the watcher the pre-restart session would hold: bound to
        # the old container (its exec died with it, but the session
        # cannot know that — it only sees the binding).
        stale = MagicMock()
        stale.container_id = "old-cid"
        stale.alive = True
        stale.stop = AsyncMock()
        session._window_watcher = stale
        await session.add_subscriber(sock1, "old-cid")
        await session.add_subscriber(sock2, "old-cid")
        sockets.connections[sock1] = conn1
        sockets.connections[sock2] = conn2

        async def fake_start(self_arg, wid, ws_obj):
            self_arg.container_id = "new-cid"
            self_arg.workspace_id = wid
            # start_workspace_container re-subscribes the restarting
            # socket (the real path calls add_subscriber).
            await session.add_subscriber(sock1, "new-cid")

        with (
            patch.object(
                Connection,
                "start_workspace_container",
                autospec=True,
                side_effect=fake_start,
            ),
            patch("klangk.wshandler.session.WindowEventWatcher") as wc,
            patch.object(registry, "record_activity"),
            patch.object(
                registry,
                "get_workspace_ports",
                return_value=[],
            ),
        ):
            wc.return_value.start = AsyncMock()
            await conn1.handle_restart_container()

        for _ in range(3):
            await asyncio.sleep(0)  # drain the spawned stop/start tasks

        # The stale watcher was torn down and a fresh one was built
        # against the recycled container — while sock2 stayed subscribed
        # the whole time (the session was never reset).
        stale.stop.assert_awaited_once()
        wc.assert_called_once()
        assert wc.call_args.args[1] == "new-cid"
        assert session._window_watcher is wc.return_value

        await session.remove_subscriber(sock1)
        await session.remove_subscriber(sock2)
        sockets.connections.pop(sock1, None)
        sockets.connections.pop(sock2, None)
        sockets.sessions.pop(workspace["id"], None)


class TestTerminalWindowHandlers:
    async def test_new_window_no_container(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock)
        conn.container_id = None
        await conn.handle_terminal_new_window({})
        # Not-attached refusals send a definite error frame — a silent
        # return strands confirmation-less clients (#3057).
        assert sock.send_json.call_args[0][0]["type"] == "error"

    async def test_new_window_success(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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

    async def test_new_window_nudges_status_connections(self):
        # #1885/#1894: creating a window pushes the window list to the
        # user's /ws status connections (e.g. the TUI). Guarded on workspace_id.
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        conn.workspace_id = "ws-1"
        sockets = conn.app.state.sockets
        with (
            patch.object(
                _mock_term,
                "new_window",
                return_value=[
                    {"id": "@0", "index": 0, "name": "bash", "active": True}
                ],
            ),
            patch.object(
                sockets, "notify_user_terminals_changed"
            ) as mock_nudge,
        ):
            await conn.handle_terminal_new_window({})
        mock_nudge.assert_called_once_with(
            conn.user["id"],
            "ws-1",
            [{"id": "@0", "index": 0, "name": "bash", "active": True}],
        )

    async def test_new_window_with_name(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        two_windows = [
            {"id": "@0", "index": 0, "name": "bash", "active": True},
            {"id": "@1", "index": 1, "name": "aux", "active": False},
        ]
        with (
            patch.object(_mock_term, "list_windows", return_value=two_windows),
            patch.object(
                _mock_term,
                "close_window",
                return_value=[
                    {"id": "@0", "index": 0, "name": "bash", "active": True}
                ],
            ),
        ):
            await conn.handle_terminal_close_window({"index": 1})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "terminal_windows"

    async def test_close_last_window_refused(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        one_window = [
            {"id": "@0", "index": 0, "name": "bash", "active": True},
        ]
        with patch.object(_mock_term, "list_windows", return_value=one_window):
            await conn.handle_terminal_close_window({"index": 0})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"
        assert "last terminal" in sent["message"].lower()

    async def test_close_shared_window_broadcasts(self, user, app_state):
        """Closing a shared window broadcasts updated shared_terminals."""
        async with _conn_in_workspace(
            user,
            "ws-1",
            user_home="/home/admin",
            perms=("code-in-isolation",),
        ) as (sock, conn, session, app_state):
            session.terminal_windows[user["id"]] = [
                {"name": "bash", "index": 0, "id": "@0", "shared": True},
                {"name": "1", "index": 1, "id": "@1", "shared": False},
            ]
            two_windows = [
                {"id": "@0", "index": 0, "name": "bash", "active": True},
                {"id": "@1", "index": 1, "name": "1", "active": False},
            ]
            with (
                patch.object(
                    _mock_term, "list_windows", return_value=two_windows
                ),
                patch.object(
                    _mock_term,
                    "close_window",
                    return_value=[
                        {"id": "@1", "index": 1, "name": "1", "active": True}
                    ],
                ),
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
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        two_windows = [
            {"id": "@0", "index": 0, "name": "bash", "active": True},
            {"id": "@1", "index": 1, "name": "aux", "active": False},
        ]
        with (
            patch.object(_mock_term, "list_windows", return_value=two_windows),
            patch.object(
                _mock_term,
                "close_window",
                side_effect=TerminalError("no such window"),
            ),
        ):
            await conn.handle_terminal_close_window({"index": 99})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"

    async def test_close_window_by_id(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        two_windows = [
            {"id": "@0", "index": 0, "name": "bash", "active": True},
            {"id": "@1", "index": 1, "name": "aux", "active": False},
        ]
        with (
            patch.object(_mock_term, "list_windows", return_value=two_windows),
            patch.object(
                _mock_term,
                "close_window",
                return_value=[
                    {"id": "@0", "index": 0, "name": "bash", "active": True}
                ],
            ),
        ):
            await conn.handle_terminal_close_window({"window_id": "@1"})
            # Targeted the window by its stable id, not an index (#1965).
            assert _mock_term.close_window.call_args[0][2] == "@1"
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "terminal_windows"

    async def test_close_window_prefers_window_id(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        two_windows = [
            {"id": "@0", "index": 0, "name": "bash", "active": True},
            {"id": "@1", "index": 1, "name": "aux", "active": False},
        ]
        with (
            patch.object(_mock_term, "list_windows", return_value=two_windows),
            patch.object(
                _mock_term,
                "close_window",
                return_value=[
                    {"id": "@0", "index": 0, "name": "bash", "active": True}
                ],
            ),
        ):
            # Both present → window_id wins over index (#1965).
            await conn.handle_terminal_close_window(
                {"window_id": "@1", "index": 0}
            )
            assert _mock_term.close_window.call_args[0][2] == "@1"

    async def test_rename_window(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        await conn.handle_terminal_rename_window({"index": 0})
        sent = sock.send_json.call_args[0][0]
        assert sent["type"] == "error"
        assert "Name" in sent["message"]

    async def test_rename_window_error(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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

    async def test_rename_shared_window_broadcasts_shared_terminals(
        self, user, app_state
    ):
        """Renaming a shared window updates other users' shared list.

        The broadcast comes from the handler's sync when it is the path
        that applies the rename (controller-first ordering of the #2651
        race).
        """
        async with _conn_in_workspace(
            user,
            "ws-1",
            user_home="/home/admin",
            perms=("code-in-isolation",),
        ) as (sock, conn, session, app_state):
            session.terminal_windows[user["id"]] = [
                {"name": "bash", "index": 0, "id": "@0", "shared": True}
            ]
            with (
                patch.object(_mock_term, "rename_window"),
                patch.object(
                    _mock_term,
                    "list_windows",
                    return_value=[
                        {
                            "id": "@0",
                            "index": 0,
                            "name": "my-build",
                            "active": True,
                        }
                    ],
                ),
            ):
                await conn.handle_terminal_rename_window(
                    {"index": 0, "name": "my-build"}
                )
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            shared = [c for c in calls if c.get("type") == "shared_terminals"]
            assert len(shared) == 1
            assert [t["window_name"] for t in shared[0]["terminals"]] == [
                "my-build"
            ]

    async def test_rename_shared_window_after_watcher_applied(
        self, user, app_state
    ):
        """No duplicate shared broadcast when the watcher applied first.

        Watcher-first ordering of the #2651 race: the debounced window
        sync already merged the renamed list into the session map (and
        broadcast the shared update itself — covered in
        test_session_sync.py), so the handler's sync finds no delta and
        must not broadcast again. The rename still reaches the client as
        a terminal_windows frame.
        """
        async with _conn_in_workspace(
            user,
            "ws-1",
            user_home="/home/admin",
            perms=("code-in-isolation",),
        ) as (sock, conn, session, app_state):
            renamed = [
                {
                    "id": "@0",
                    "index": 0,
                    "name": "my-build",
                    "active": True,
                }
            ]
            # State as the watcher's re-sync left it: map merged,
            # baseline current.
            session.terminal_windows[user["id"]] = [
                {"name": "my-build", "index": 0, "id": "@0", "shared": True}
            ]
            session._last_windows[user["id"]] = renamed
            with (
                patch.object(_mock_term, "rename_window"),
                patch.object(
                    _mock_term,
                    "list_windows",
                    return_value=renamed,
                ),
            ):
                await conn.handle_terminal_rename_window(
                    {"index": 0, "name": "my-build"}
                )
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert [
                c for c in calls if c.get("type") == "shared_terminals"
            ] == []
            assert any(
                c.get("type") == "terminal_windows"
                and any(w["name"] == "my-build" for w in c["windows"])
                for c in calls
            )

    async def test_list_windows(self):
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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
        conn = _base_conn(ws=sock, perms=("code-in-isolation",))
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


class TestJoinOnlyMemberFrameGates:
    """#3022: the connect gate is ``join-workspace``, so a member can
    hold it while holding no terminal powers at all. Every frame that
    execs into the container must then refuse on its own — historically
    these rode on the connect handshake checking ``terminal``.

    A join-only member (or a spectator whose grouped joiner session
    exists) must get the machine-readable ``forbidden`` refusal and
    cause no podman/tmux work.
    """

    def _join_only_conn(self):
        sock = _mock_sock()
        # Join-only member: holds the connect gate and nothing else.
        conn = _base_conn(ws=sock, perms=("join-workspace",))
        conn.container_id = "cid"
        conn._user_home = "/home/alice"
        conn.workspace_id = "ws-1"
        return sock, conn

    async def test_own_window_frames_refused(self):
        sock, conn = self._join_only_conn()
        handlers = (
            conn.handle_terminal_new_window,
            conn.handle_terminal_select_window,
            conn.handle_terminal_close_window,
            conn.handle_terminal_rename_window,
            conn.handle_terminal_list_windows,
        )
        with (
            patch.object(
                _mock_term, "new_window", new=AsyncMock()
            ) as mock_new,
            patch.object(
                _mock_term, "select_window", new=AsyncMock()
            ) as mock_sel,
            patch.object(
                _mock_term, "close_window", new=AsyncMock()
            ) as mock_close,
            patch.object(
                _mock_term, "rename_window", new=AsyncMock()
            ) as mock_rename,
            patch.object(
                _mock_term, "list_windows", new=AsyncMock()
            ) as mock_list,
        ):
            for handler, msg in (
                (handlers[0], {}),
                (handlers[1], {"index": 0}),
                (handlers[2], {"index": 0}),
                (handlers[3], {"index": 0, "name": "x"}),
                (handlers[4], None),
            ):
                if msg is None:
                    await handler()
                else:
                    await handler(msg)
        for mock in (mock_new, mock_sel, mock_close, mock_rename, mock_list):
            mock.assert_not_called()
        refusals = [
            c.args[0]
            for c in sock.send_json.call_args_list
            if c.args[0].get("type") == "error"
        ]
        assert len(refusals) == 5
        for frame in refusals:
            assert frame["message"] == "Permission denied"
            # Deliberately code-less: a stamped `forbidden` would make
            # the frontend swap the whole page for the access-revoked
            # view (#2891 reserves that code for connect-level refusals).
            assert "code" not in frame

    async def test_ssh_agent_start_refused(self):
        """No own-terminal/exec permission → no relay spawned in the
        container (no pkill/rm/socat podman work at all)."""
        sock, conn = self._join_only_conn()
        with (
            patch.object(_mock_pod, "exec_container", new=AsyncMock()) as pod,
            patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn,
        ):
            await conn.handle_ssh_agent_start()
        pod.assert_not_called()
        spawn.assert_not_called()
        frame = sock.send_json.call_args[0][0]
        assert frame["message"] == "Permission denied"
        assert "code" not in frame
        assert conn.ssh_agent.proc is None

    async def test_ssh_agent_start_allowed_for_exec_only_member(self):
        """``exec-and-sync`` alone also permits the relay: exec sessions
        wire SSH_AUTH_SOCK to the same per-user socket (#2001)."""
        sock = _mock_sock()
        conn = _base_conn(ws=sock, perms=("exec-and-sync",))
        conn.container_id = "cid"
        mock_proc = AsyncMock()
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.stdin = AsyncMock()
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock()
        with (
            patch.object(
                _mock_pod,
                "exec_container",
                new=AsyncMock(return_value=(0, "", "")),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=mock_proc),
            ),
        ):
            await conn.handle_ssh_agent_start()
            assert conn.ssh_agent.task is not None
            await conn.ssh_agent.task
        msg = sock.send_json.call_args[0][0]
        assert msg["type"] == "ssh_agent_started"

    async def test_join_only_member_refused_via_real_acl(
        self, user, app_state
    ):
        """The gate walks the REAL ACL, not the has_perm override — pins
        the permission string against a typo (a misspelled name would
        silently deny seeded roles instead). Mirrors the #2975
        connect-gate tests: stored ACEs, no overrides."""
        from klangk import model

        sock = _mock_sock()
        member = await app_state.state.model.users.create_user(
            "join-only@example.com", "pw", verified=True
        )
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "join-only-real-acl"
        )
        # Stored grants: exactly the connect gate, nothing else.
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{workspace['id']}",
            100,
            model.ACTION_ALLOW,
            "join-workspace",
            model.PRINCIPAL_USER,
            user_id=member["id"],
        )
        conn = _base_conn(user=member, ws=sock, app_state=app_state)
        conn.workspace_id = workspace["id"]
        conn.container_id = "cid"
        conn._user_home = "/home/joinonly"

        with patch.object(_mock_term, "new_window", new=AsyncMock()) as mock:
            await conn.handle_terminal_new_window({})
        mock.assert_not_called()
        frame = sock.send_json.call_args[0][0]
        assert frame["type"] == "error"
        assert frame["message"] == "Permission denied"
        assert "code" not in frame


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
            has_perm=AsyncMock(return_value=has_perm),
            broadcast_shared_terminals=MagicMock(),
            app=app_state,
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

    async def test_setup_state_db_error_defaults_to_complete(self, app_state):
        """If the setup_state lookup raises, default to 'complete'."""
        ctrl, _, conn = self._controller()
        with patch.object(
            conn.app.state.model.workspaces,
            "get_workspace",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            result = await ctrl._setup_state_for_workspace()
        assert result == "complete"

    async def test_setup_state_workspace_missing_defaults_to_complete(
        self, app_state
    ):
        """If get_workspace returns None, default to 'complete'."""
        ctrl, _, conn = self._controller()
        with patch.object(
            conn.app.state.model.workspaces,
            "get_workspace",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await ctrl._setup_state_for_workspace()
        assert result == "complete"

    async def test_setup_state_returns_workspace_value(self, app_state):
        """Returns the workspace's actual setup_state when present (#1033)."""
        ctrl, _, conn = self._controller()
        with patch.object(
            conn.app.state.model.workspaces,
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

    async def test_start_stops_existing_terminal_first(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ctrl, _, conn = self._controller(app_state=app_state)

        def _swallow(coro, **kw):
            # Close the coroutine so it doesn't warn about being
            # never awaited.
            coro.close()
            return MagicMock()

        with (
            patch.object(ctrl, "stop", new=AsyncMock()) as stop,
            patch("klangk.wshandler.controllers.TerminalSession") as MockTS,
            patch(
                "klangk.wshandler.controllers.asyncio.create_task",
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

    async def test_input_read_only_allows_da_response(self, app_state):
        """DA1/DA2/DA3 device-attribute responses pass (#1716)."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        session = AsyncMock()
        session.is_alive = True
        session.read_only = True
        ctrl.session = session
        with patch.object(registry, "record_activity"):
            await ctrl.input({"data": "\x1b[?1;2c"})
        session.write.assert_awaited_once_with("\x1b[?1;2c")

    async def test_input_read_only_allows_color_report(self):
        """OSC 10/11/12/4 color reports pass; ST may be BEL or ESC \\\
        (#1716)."""
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.is_alive = True
        session.read_only = True
        ctrl.session = session
        for data in (
            "\x1b]11;rgb:0000/0000/0000\x07",
            "\x1b]10;rgb:aaaa/bbbb/cccc\x1b\\",
            "\x1b]4;0;rgb:ff/00/00\x07",
            # Color value forms beyond rgb: (#rrggbb / rgbi:).
            "\x1b]11;#ff00ff\x07",
            "\x1b]11;#aabbccddeeff\x1b\\",
            "\x1b]11;rgbi:255/0/255\x07",
        ):
            session.write.reset_mock()
            await ctrl.input({"data": data})
            session.write.assert_awaited_once_with(data)

    async def test_input_read_only_allows_dcs_capability_reports(self):
        """XTVERSION and XTGETTCAP responses pass (#1716)."""
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.is_alive = True
        session.read_only = True
        ctrl.session = session
        for data in (
            "\x1bP>|xterm.js 5.5.0\x1b\\",  # XTVERSION
            "\x1bP>|tmux 3.4\x07",
            "\x1bP1+r5443=787465726d\x1b\\",  # XTGETTCAP success
            "\x1bP0+r\x1b\\",  # XTGETTCAP not-found
        ):
            session.write.reset_mock()
            await ctrl.input({"data": data})
            session.write.assert_awaited_once_with(data)

    async def test_input_read_only_blocks_osc52_clipboard(self):
        """OSC 52 clipboard read/write is dropped for spectators (#1716)."""
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.is_alive = True
        session.read_only = True
        ctrl.session = session
        for data in (
            "\x1b]52;c;Zm9v\x07",  # clipboard write (base64 "foo")
            "\x1b]52;c;?\x07",  # clipboard read
        ):
            session.write.reset_mock()
            await ctrl.input({"data": data})
            session.write.assert_not_awaited()

    async def test_input_read_only_blocks_arbitrary_escapes(self):
        """Title sets, size reports, clear, DSR query, DCS passthrough
        are all dropped (#1716)."""
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.is_alive = True
        session.read_only = True
        ctrl.session = session
        for data in (
            "\x1b]0;title\x07",  # OSC 0 title set
            "\x1b[18t",  # report terminal size
            "\x1b[2J",  # clear screen
            "\x1b[6n",  # DSR cursor query (not a response)
            "\x1bPtmux;evil\x1b\\",  # DCS tmux passthrough
            "\x1b[c",  # bare DA query
            "ls\x1b[?6c",  # text smuggled before a valid response
            "\x1b[?6c\x1b]52;c;Zm9v\x07",  # OSC 52 chained after DA
        ):
            session.write.reset_mock()
            await ctrl.input({"data": data})
            session.write.assert_not_awaited()

    async def test_input_oversized_dropped(self):
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.is_alive = True
        session.read_only = False
        ctrl.session = session
        await ctrl.input({"data": "x" * (_ws_support.MAX_INPUT_SIZE + 1)})
        session.write.assert_not_awaited()

    async def test_input_oversized_read_only_dropped_before_regex(self):
        """Oversized read-only input hits the size guard before the
        whitelist regex (#1716) — the cheap O(1) check protects the
        linear scan, restoring the prior cost profile."""
        ctrl, _, _ = self._controller()
        session = AsyncMock()
        session.is_alive = True
        session.read_only = True
        ctrl.session = session
        big = "\x1b[?6c" + "x" * (_ws_support.MAX_INPUT_SIZE + 1)
        with patch(
            "klangk.wshandler.controllers.is_allowed_read_only_input"
        ) as allow:
            await ctrl.input({"data": big})
        # Size guard returned early, so the whitelist was never consulted.
        allow.assert_not_called()
        session.write.assert_not_awaited()

    async def test_input_writes_and_records_activity(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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

    async def test_stop_broadcasts_when_was_viewing(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        result = await ctrl.activate_session(stale)
        assert result is False
        stale.stop.assert_awaited_once()

    async def test_activate_session_wires_forward_task(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        ctrl, _, _ = self._controller(app_state=app_state)
        session = _mock_terminal()
        ctrl.session = session
        with patch.object(registry, "record_activity") as rec:
            result = await ctrl.activate_session(session)
        assert result is True
        assert ctrl.output_task is not None
        session.resize.assert_awaited_once_with(80, 24)
        rec.assert_called_once_with("cid")
        ctrl.output_task.cancel()
        try:
            await ctrl.output_task
        except asyncio.CancelledError:
            pass

    async def test_activate_session_uses_current_client_size(self):
        """activate_session resizes to the controller's CURRENT dims, not
        the dims captured at terminal_start (#2671).

        The client can shrink between terminal_start and activation (the
        tab strip appearing fires a terminal_resize while the attach
        exec's PTY doesn't exist yet, so TerminalSession.resize drops
        it). If the forced redraw then used the stale start-time size,
        tmux would repaint taller than the client grid and scroll the
        prompt off the top of the viewport.
        """
        ctrl, _, _ = self._controller()
        session = _mock_terminal()
        ctrl.session = session
        # terminal_start captured 100x30; the client has since resized
        # to 100x27 (tab strip appearing).
        ctrl.cols = 100
        ctrl.rows = 27
        result = await ctrl.activate_session(session)
        assert result is True
        session.resize.assert_awaited_once_with(100, 27)
        ctrl.output_task.cancel()
        try:
            await ctrl.output_task
        except asyncio.CancelledError:
            pass

    # --- forward_output ---

    async def test_forward_output_relays_and_cleans_up(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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

    async def test_forward_output_records_activity_when_container_set(
        self, app_state
    ):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
        from klangk.wshandler import WS_ERRORS

        ctrl, sock, _ = self._controller()
        session = AsyncMock()
        ctrl.session = session

        async def fake_output():
            yield "data"

        session.output = fake_output
        sock.send_json = MagicMock(side_effect=WS_ERRORS[0]("ws dead"))
        with patch("klangk.wshandler.controllers.send_event"):
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
        assert sock.send_json.call_args[0][0]["type"] == "error"

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
        assert sock.send_json.call_args[0][0]["type"] == "error"

    async def test_rename_window_no_name_sends_error(self):
        ctrl, sock, _ = self._controller()
        await ctrl.rename_window({"index": 0, "name": ""})
        assert sock.send_json.call_args[0][0]["type"] == "error"

    async def test_list_windows_no_container(self):
        ctrl, sock, _ = self._controller(container_id=None)
        await ctrl.list_windows()
        assert sock.send_json.call_args[0][0]["type"] == "error"

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
        assert sock.send_json.call_args[0][0]["type"] == "error"

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
        self, user, app_state
    ):
        """When a ws_session exists, only this user's sockets receive it."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_sync_terminal_windows_marks_service_cmd_shared(
        self, app_state
    ):
        """The service-cmd window is shared by definition, so syncing the
        owner's own windows marks it shared even with no prior entry (#1114)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_sync_service_windows_injects_service_cmd_shared(
        self, app_state
    ):
        """Discovery: connecting syncs the agent's service:service-cmd window
        into the session map (keyed by AGENT_USER_ID, marked shared, handle
        cached) even though the agent has no WS connection (#1133)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        from klangk import model

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
                    app_state.state.model.users,
                    "agent_handle",
                    new=AsyncMock(return_value="klangk"),
                ),
            ):
                synced = await ctrl.sync_service_windows(ws_session)
            assert synced is True
            agent_wins = ws_session.terminal_windows[model.AGENT_USER_ID]
            assert agent_wins[0]["name"] == "service-cmd"
            assert agent_wins[0]["shared"] is True
            # Handle cached so the window stays attributable.
            assert ws_session.agent_handle == "klangk"
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_sync_service_windows_no_container_returns_false(
        self, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, _, _ = self._controller(container_id=None, app_state=app_state)
        ws_session = sockets.get_or_create_session("ws-1", app_state)
        try:
            assert await ctrl.sync_service_windows(ws_session) is False
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_sync_service_windows_list_error_returns_false(
        self, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, _, _ = self._controller(app_state=app_state)
        ws_session = sockets.get_or_create_session("ws-1", app_state)
        try:
            with patch.object(
                _mock_term,
                "list_windows",
                new=AsyncMock(side_effect=TerminalError),
            ):
                assert await ctrl.sync_service_windows(ws_session) is False
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_sync_service_windows_empty_returns_false(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, _, _ = self._controller(app_state=app_state)
        ws_session = sockets.get_or_create_session("ws-1", app_state)
        try:
            with patch.object(
                _mock_term,
                "list_windows",
                new=AsyncMock(return_value=[]),
            ):
                assert await ctrl.sync_service_windows(ws_session) is False
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_sync_service_windows_agent_handle_error_returns_false(
        self, app_state
    ):
        """If the agent handle can't be resolved, discovery is skipped
        (best-effort) rather than breaking the caller (#1133)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
                    app_state.state.model.users,
                    "agent_handle",
                    new=AsyncMock(side_effect=RuntimeError("db down")),
                ),
            ):
                assert await ctrl.sync_service_windows(ws_session) is False
            assert ws_session.agent_handle is None
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_get_shared_terminals_visible_when_agent_offline(
        self, app_state
    ):
        """The service window stays in the shared list (attributed to the
        agent) via the cached agent_handle, though the agent has no WS
        connection (#1133)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        from klangk import model
        from klangk.wshandler.session import get_shared_terminals

        ws_session = sockets.get_or_create_session("ws-offline", app_state)
        try:
            ws_session.terminal_windows[model.AGENT_USER_ID] = [
                {"id": "@1", "index": 1, "name": "service-cmd", "shared": True}
            ]
            ws_session.agent_handle = "klangk"
            # No active connection for the agent.
            terminals = get_shared_terminals(ws_session, sockets)
            assert len(terminals) == 1
            assert terminals[0]["handle"] == "klangk"
            assert terminals[0]["window_name"] == "service-cmd"
            # Agent-owned windows are flagged so the UI can present the
            # service tab distinctly (#1159).
            assert terminals[0]["is_service"] is True
        finally:
            sockets.sessions.pop("ws-offline", None)

    async def test_fire_service_command_invokes_ensure_service_session(
        self, app_state
    ):
        """fire_service_command reads fresh setup_state from the DB and
        targets the service session (#1133); the session HOME is pinned
        inside ensure_service_session, so no home argument (#2717)."""
        ctrl, _, conn = self._controller()
        conn._service_command = "./run.sh"
        with (
            patch.object(
                conn.app.state.model.workspaces,
                "get_workspace",
                new=AsyncMock(return_value={"setup_state": "complete"}),
            ),
            patch.object(
                conn.app.state.model.users,
                "agent_handle",
                new=AsyncMock(return_value="klangk"),
            ),
            patch.object(
                _mock_term,
                "ensure_service_session",
                new=AsyncMock(),
            ) as mock_ess,
        ):
            await ctrl.fire_service_command()
        mock_ess.assert_awaited_once_with(
            "cid",
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
            await ctrl.fire_service_command()
        mock_ess.assert_not_awaited()

    async def test_fire_service_command_no_container_noop(self):
        ctrl, _, conn = self._controller(container_id=None)
        conn._service_command = "./run.sh"
        with patch.object(
            _mock_term,
            "ensure_service_session",
            new=AsyncMock(),
        ) as mock_ess:
            await ctrl.fire_service_command()
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

    async def test_browser_reattach_registers_and_attaches(self, app_state):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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
            result = await conn.activate_session(session)
        assert result is True
        m.assert_awaited_once_with(session)

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
        conn.terminal_cols = 120
        conn.terminal_rows = 40
        assert conn.terminal.cols == 120
        assert conn.terminal.rows == 40
        assert conn.terminal_cols == 120
        assert conn.terminal_rows == 40


class TestShareWindowHandlers:
    """Tests for the unified share/unshare/join terminal handlers."""

    async def test_share_window_broadcasts(
        self, user, temp_data_dir, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_unshare_window_kicks_joiners(
        self, user, temp_data_dir, app_state
    ):
        """Unsharing disconnects only the viewers of THAT window — the
        viewer of another still-shared window keeps their session (#3072)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": True},
            {"name": "2", "index": 1, "id": "@1", "shared": True},
        ]
        viewer = _base_conn(app_state=app_state)
        viewer.workspace_id = "ws-1"
        viewer.viewing_shared = {"user_id": user["id"], "window_id": "@0"}
        other = _base_conn(app_state=app_state)
        other.workspace_id = "ws-1"
        other.viewing_shared = {"user_id": user["id"], "window_id": "@1"}
        await session.add_subscriber(sock, "cid")
        with patch.object(
            WorkspaceSession, "start_window_sync", lambda s: None
        ):
            await session.add_subscriber(viewer.sock, "cid")
            await session.add_subscriber(other.sock, "cid")
        sockets.connections[viewer.sock] = viewer
        sockets.connections[other.sock] = other
        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(
                    viewer, "stop_terminal", new=AsyncMock()
                ) as stop_viewer,
                patch.object(
                    other, "stop_terminal", new=AsyncMock()
                ) as stop_other,
            ):
                await conn.handle_unshare_window({"window_id": "@0"})
            assert session.terminal_windows[user["id"]][0]["shared"] is False
            stop_viewer.assert_awaited_once()
            stop_other.assert_not_awaited()
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            deleted = [
                c for c in calls if c.get("type") == "shared_terminal_deleted"
            ]
            assert len(deleted) == 1
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_share_window_not_attached_sends_error(self, user):
        """share_window with no attached container answers with an error
        frame, never silently — a confirmation-less client hangs on silence
        (#3057)."""
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = None
        await conn.handle_share_window({"window_id": "@0"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            c.get("type") == "error"
            and "No workspace terminal attached" in c.get("message", "")
            for c in calls
        )

    async def test_unshare_window_not_attached_sends_error(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = None
        await conn.handle_unshare_window({"window_id": "@0"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            c.get("type") == "error"
            and "No workspace terminal attached" in c.get("message", "")
            for c in calls
        )

    async def test_unshare_window_no_session_sends_error(self, user):
        """A missing workspace session is a definite error, not a silent
        no-op — the CLI waits on a shared_terminals frame after unshare and
        blind-times-out on silence (#3057)."""
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-no-session"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        await conn.handle_unshare_window({"window_id": "@0"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            c.get("type") == "error"
            and "No workspace session" in c.get("message", "")
            for c in calls
        )

    async def test_unshare_window_already_unshared_broadcasts(
        self, user, temp_data_dir, app_state
    ):
        """Unsharing an already-unshared window is idempotent AND confirmed:
        the current shared_terminals list is broadcast so the waiting client
        exits promptly with a definite outcome (#3057)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": False},
        ]
        await session.add_subscriber(sock, "cid")
        try:
            await conn.handle_unshare_window({"window_id": "@0"})
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert any(c.get("type") == "shared_terminals" for c in calls)
            assert not any(
                c.get("type") == "shared_terminal_deleted" for c in calls
            )
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_join_shared_terminal_not_attached_sends_error(self, user):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock)
        conn.workspace_id = "ws-1"
        conn.container_id = None
        await conn.handle_join_shared_terminal({"window_id": "@0"})
        calls = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            c.get("type") == "error"
            and "No workspace terminal attached" in c.get("message", "")
            for c in calls
        )

    async def test_list_shared_terminals(self, user, temp_data_dir, app_state):
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

    async def test_shared_terminals_include_viewers(
        self, user, temp_data_dir, app_state
    ):
        """shared_terminals response includes viewer list."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_stop_terminal_broadcasts_viewer_change(
        self, user, app_state
    ):
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

    async def test_create_shared_terminal_legacy(
        self, user, temp_data_dir, app_state
    ):
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

    async def test_create_shared_terminal_marks_new_window_not_namesake(
        self, user, temp_data_dir, app_state
    ):
        """When a same-named window already exists, the just-created (active)
        window is the one marked shared — identified by id, not name (#2192)."""
        async with _conn_in_workspace(
            user, "ws-1", user_home="/home/admin"
        ) as (sock, conn, session, app_state):
            # A pre-existing window already named "dev" (not shared).
            session.terminal_windows[user["id"]] = [
                {"name": "dev", "index": 0, "id": "@0", "shared": False}
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
                        # Two "dev" windows after create; the new one is active.
                        {
                            "id": "@0",
                            "index": 0,
                            "name": "dev",
                            "active": False,
                        },
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
            by_id = {w["id"]: w for w in windows}
            # The newly created window (@1) is shared; the namesake (@0) is not.
            assert by_id["@1"]["shared"] is True
            assert by_id["@0"]["shared"] is False

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

    async def test_share_window_not_found(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_unshare_window_without_permission(self, user, app_state):
        """Unsharing an own window needs no share-terminals permission —
        a member whose permission was revoked after sharing must not be
        left with a tab that stays readable to everyone (#2875)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
                new=AsyncMock(return_value=False),
            ):
                await conn.handle_unshare_window({"window_id": "@0"})
            assert session.terminal_windows[user["id"]][0]["shared"] is False
            calls = [c[0][0] for c in sock.send_json.call_args_list]
            assert not any("Permission" in c.get("message", "") for c in calls)
        finally:
            sockets.sessions.pop("ws-1", None)

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

    async def test_unshare_window_not_found(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_unshare_kill_error_handled(self, user, app_state):
        """A viewer whose teardown raises still gets past the kick: the
        unshare completes and broadcasts for the others (#3072)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/admin"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "1", "index": 0, "id": "@0", "shared": True}
        ]
        viewer = _base_conn(app_state=app_state)
        viewer.workspace_id = "ws-1"
        viewer.viewing_shared = {"user_id": user["id"], "window_id": "@0"}
        await session.add_subscriber(sock, "cid")
        # add_subscriber spawns the tmux window watcher against the mock
        # podman; suppress it (not what this test exercises).
        with patch.object(
            WorkspaceSession, "start_window_sync", lambda s: None
        ):
            await session.add_subscriber(viewer.sock, "cid")
        sockets.connections[viewer.sock] = viewer
        try:
            with (
                patch.object(
                    acl_mod.ACL,
                    "check_permission",
                    new=AsyncMock(return_value=True),
                ),
                patch.object(
                    viewer,
                    "stop_terminal",
                    new=AsyncMock(side_effect=OSError("boom")),
                ),
            ):
                await conn.handle_unshare_window({"window_id": "@0"})
            assert session.terminal_windows[user["id"]][0]["shared"] is False
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_join_shared_terminal(self, user, temp_data_dir, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        owner = await app_state.state.model.users.create_user(
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

    async def test_join_shared_terminal_read_only_follows_permissions(
        self, user, temp_data_dir, app_state
    ):
        """#2939: the shared-terminal write gate is evaluated ONCE at
        join — ``code-in-shared-terminals`` OR ``share-terminals``
        freezes into TerminalSession(read_only=...). A spectate-only
        member gets a read-only session; a member with the code
        permission gets a writable one."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        owner = await app_state.state.model.users.create_user(
            "owner2@test.com", "hash", verified=True
        )

        async def join_and_inspect(granted: set[str]) -> bool:
            sock = _mock_sock()
            conn = _base_conn(user=user, ws=sock, app_state=app_state)
            conn.workspace_id = "ws-ro"
            conn.container_id = "cid-ro"
            conn._user_home = "/home/joiner"
            session = sockets.get_or_create_session("ws-ro", app_state)
            session.terminal_windows[owner["id"]] = [
                {"name": "build", "index": 0, "id": "@0", "shared": True},
            ]
            registry.track_activity("cid-ro", "ws-ro")

            async def fake_has_perm(perm: str) -> bool:
                return perm in granted

            conn.has_perm = fake_has_perm  # type: ignore[method-assign]
            try:
                with (
                    patch.object(_ws_controllers, "TerminalSession") as MTS,
                    patch.object(_mock_term, "select_window"),
                    patch.object(_mock_term, "tmux_command", return_value=""),
                ):
                    mock_sess = _mock_terminal()
                    MTS.return_value = mock_sess

                    async def fake_output():
                        return
                        yield

                    mock_sess.output = fake_output
                    await conn.handle_join_shared_terminal(
                        {"user_id": owner["id"], "window_id": "@0"}
                    )
                    await asyncio.sleep(0)
                return MTS.call_args[1]["read_only"]
            finally:
                sockets.sessions.pop("ws-ro", None)
                registry.states.pop("ws-ro", None)

        # Spectate-only: read-only session.
        spectate = {"spectate-on-shared-terminals"}
        assert await join_and_inspect(spectate)
        # Either write permission unfreezes it (spectate is the join
        # gate itself, so realistic grant sets carry it too).
        assert not await join_and_inspect(
            spectate | {"code-in-shared-terminals"}
        )
        assert not await join_and_inspect(spectate | {"share-terminals"})

    async def test_join_service_terminal_routes_to_service_session(
        self, user, temp_data_dir, app_state
    ):
        """Joining the agent's service window targets the standalone
        ``service`` tmux session, not a session named after the agent's
        user_id (which doesn't exist) -- #1158/#1159."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[model.AGENT_USER_ID] = [
            {"name": "service-cmd", "index": 0, "id": "@0", "shared": True},
        ]
        session.agent_handle = "klangk"
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

    async def test_join_shared_terminal_superseded(
        self, user, temp_data_dir, app_state
    ):
        """If session is superseded during start, activate_session returns False."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        owner = await app_state.state.model.users.create_user(
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
        self, user, temp_data_dir, app_state
    ):
        """Falls back to bare @N when joiner session select fails."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        owner = await app_state.state.model.users.create_user(
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
        self, user, temp_data_dir, app_state
    ):
        """Falls back to bare @N when joiner session name is None."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        owner = await app_state.state.model.users.create_user(
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

    async def test_join_shared_terminal_start_error(
        self, user, temp_data_dir, app_state
    ):
        """If session.start() fails, error is sent and session stopped."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        owner = await app_state.state.model.users.create_user(
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

    async def test_join_shared_terminal_not_found(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_delete_shared_terminal(
        self, user, temp_data_dir, app_state
    ):
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

    async def test_delete_shared_terminal_not_found(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        self, user, temp_data_dir, app_state
    ):
        """A collaborator may not delete another user's terminal
        (regression for #874)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        other = await app_state.state.model.users.create_user(
            "other@example.com", "x", verified=True
        )
        # Workspace owned by `other`; caller is neither the terminal
        # owner nor the workspace owner.
        workspace = await app_state.state.model.workspaces.create_workspace(
            other["id"], "ws-other"
        )
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
        self, user, temp_data_dir, app_state
    ):
        """The workspace owner may delete another member's terminal."""
        other = await app_state.state.model.users.create_user(
            "other@example.com", "x", verified=True
        )
        # Workspace owned by the caller (`user`).
        workspace = await app_state.state.model.workspaces.create_workspace(
            user["id"], "ws-mine"
        )
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
                patch.object(_mock_term, "close_window", return_value=[]),
            ):
                await conn.handle_delete_shared_terminal(
                    {"user_id": other["id"], "window_id": "@0"}
                )
            assert session.terminal_windows[other["id"]] == []

    async def test_delete_shared_terminal_error(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
                    "close_window",
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
        assert await conn.has_perm("view")

    async def test_has_perm_no_workspace(self):
        conn = _base_conn()
        assert not await conn.has_perm("view")


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
                self.app = app_state
                self.has_perm = AsyncMock(return_value=has_perm)
                self.stop_terminal = AsyncMock()
                self.activate_session = AsyncMock(return_value=True)
                self.tmux_session_name = MagicMock(return_value="uid")
                self.sync_terminal_windows = MagicMock()
                self.terminal_cols = 80
                self.terminal_rows = 24
                self.terminal = SimpleNamespace(
                    session=None,
                    task=None,
                    sync_service_windows=AsyncMock(return_value=False),
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
        return app_state.state.sockets.get_or_create_session(ws_id, app_state)

    async def _viewer(
        self, app_state, ws, *, owner_user_id, window_id, stop=None
    ):
        """Register a viewer connection for (owner, window) as a session
        subscriber; returns (sock, stop mock)."""
        sock = _mock_sock()
        stop = AsyncMock() if stop is None else stop
        conn = SimpleNamespace(
            viewing_shared={"user_id": owner_user_id, "window_id": window_id},
            stop_terminal=stop,
            user={"id": "viewer", "email": "viewer@example.com"},
        )
        # add_subscriber spawns the tmux window watcher against the mock
        # podman; suppress it (not what these tests exercise).
        with patch.object(
            WorkspaceSession, "start_window_sync", lambda s: None
        ):
            await ws.add_subscriber(sock, "cid")
        app_state.state.sockets.connections[sock] = conn
        return sock, stop

    # --- find_window ---

    async def test_find_window_returns_match(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_find_window_not_found_sends_error(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, sock, conn = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        try:
            assert ctrl.find_window(ws, user["id"], "@99") is None
            msg = sock.send_json.call_args[0][0]
            assert msg == {"type": "error", "message": "Window not found"}
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_find_window_shared_true_rejects_unshared(
        self, user, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_share_window_marks_and_broadcasts(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_share_window_not_found(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_unshare_window_without_perm_still_unshares(
        self, user, temp_data_dir, app_state
    ):
        """Unsharing an own window skips the share-terminals gate —
        it only reduces exposure (#2875)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, sock, _ = self._controller(
            user=user, app_state=app_state, has_perm=False
        )
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": True}
        ]
        _, stop = await self._viewer(
            app_state, ws, owner_user_id=user["id"], window_id="@0"
        )
        try:
            await ctrl.unshare_window({"window_id": "@0"})
            assert ws.terminal_windows[user["id"]][0]["shared"] is False
            stop.assert_awaited_once()
            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert not any("Permission" in c.get("message", "") for c in sent)
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_unshare_window_marks_unshared_and_kicks(
        self, user, temp_data_dir, app_state
    ):
        """Only the viewers of the unshared window are kicked: a viewer
        of the owner's other still-shared window keeps their session, and
        a subscriber with no registered connection is skipped (#3072)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, _, conn = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": True},
            {"id": "@1", "name": "b", "shared": True},
        ]
        _, stop_viewing = await self._viewer(
            app_state, ws, owner_user_id=user["id"], window_id="@0"
        )
        _, stop_other = await self._viewer(
            app_state, ws, owner_user_id=user["id"], window_id="@1"
        )
        orphan = _mock_sock()  # subscriber without a registered connection
        with patch.object(
            WorkspaceSession, "start_window_sync", lambda s: None
        ):
            await ws.add_subscriber(orphan, "cid")
        try:
            await ctrl.unshare_window({"window_id": "@0"})
            assert ws.terminal_windows[user["id"]][0]["shared"] is False
            stop_viewing.assert_awaited_once()
            stop_other.assert_not_awaited()
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_unshare_window_other_owners_viewer_not_kicked(
        self, user, temp_data_dir, app_state
    ):
        """A viewer of another owner's window (same window id) is not
        kicked by this owner's unshare."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, _, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": True}
        ]
        _, stop = await self._viewer(
            app_state, ws, owner_user_id="someone-else", window_id="@0"
        )
        try:
            await ctrl.unshare_window({"window_id": "@0"})
            stop.assert_not_awaited()
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_unshare_window_other_users_window_not_found(
        self, user, temp_data_dir, app_state
    ):
        """The unshare loosening (#2875) is scoped to the caller's own
        windows: another user's window id is rejected and their shared
        flag is untouched — the property the permission removal rests on."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows["other-user"] = [
            {"id": "@5", "name": "theirs", "shared": True}
        ]
        _, stop = await self._viewer(
            app_state, ws, owner_user_id="other-user", window_id="@5"
        )
        try:
            await ctrl.unshare_window({"window_id": "@5"})
            assert sock.send_json.call_args[0][0]["message"] == (
                "Window not found"
            )
            assert ws.terminal_windows["other-user"][0]["shared"] is True
            stop.assert_not_awaited()
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_unshare_window_already_unshared_is_noop(
        self, user, temp_data_dir, app_state
    ):
        """Unsharing a not-shared window is a cheap no-op — no viewer
        kicks, no shared_terminal_deleted broadcast."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": False}
        ]
        _, stop = await self._viewer(
            app_state, ws, owner_user_id=user["id"], window_id="@0"
        )
        try:
            await ctrl.unshare_window({"window_id": "@0"})
            stop.assert_not_awaited()
            sent = [c[0][0] for c in sock.send_json.call_args_list]
            assert not any(
                s.get("type") == "shared_terminal_deleted" for s in sent
            )
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_unshare_kill_error_handled(
        self, user, temp_data_dir, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        with patch.object(
            WorkspaceSession, "start_window_sync", lambda s: None
        ):
            await ws.add_subscriber(sock, "cid")
        ws.terminal_windows[user["id"]] = [
            {"id": "@0", "name": "a", "shared": True}
        ]
        await self._viewer(
            app_state,
            ws,
            owner_user_id=user["id"],
            window_id="@0",
            stop=AsyncMock(side_effect=OSError("boom")),
        )
        try:
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

    async def test_list_shared_terminals_sends_list(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_broadcast_shared_terminals_broadcasts(
        self, user, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        self, user, temp_data_dir, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        self, user, temp_data_dir, app_state
    ):
        ctrl, sock, conn = self._controller(user=user)
        with patch.object(
            conn.app.state.model.workspaces,
            "get_workspace_by_id",
            new=AsyncMock(),
        ) as gw:
            gw.return_value = {"user_id": "someone-else"}
            await ctrl.delete_shared_terminal(
                {"user_id": "owner-1", "window_id": "@0"}
            )
        assert "Permission" in sock.send_json.call_args[0][0]["message"]

    async def test_delete_shared_terminal_not_found(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        self, user, temp_data_dir, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        with patch.object(
            WorkspaceSession, "start_window_sync", lambda s: None
        ):
            await ws.add_subscriber(sock, "cid")
        ws.terminal_windows["owner-1"] = [
            {"id": "@0", "index": 0, "name": "build", "shared": True},
            {"id": "@1", "index": 1, "name": "other", "shared": False},
        ]
        try:
            # owner_user_id != user["id"], so the delete handler calls
            # app_state.state.model.workspaces.get_workspace_by_id to authorize; return a workspace
            # owned by the current user so the delete is permitted.
            _, stop = await self._viewer(
                app_state, ws, owner_user_id="owner-1", window_id="@0"
            )
            with (
                patch.object(
                    app_state.state.model.workspaces,
                    "get_workspace_by_id",
                    new=AsyncMock(return_value={"user_id": user["id"]}),
                ),
                patch.object(_mock_term, "close_window") as close,
            ):
                await ctrl.delete_shared_terminal(
                    {"user_id": "owner-1", "window_id": "@0"}
                )
            stop.assert_awaited_once()
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
        self, user, temp_data_dir, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows["owner-1"] = [
            {"id": "@0", "index": 0, "name": "build", "shared": True}
        ]
        try:
            with patch.object(
                _mock_term,
                "close_window",
                side_effect=OSError("boom"),
            ):
                await ctrl.delete_shared_terminal(
                    {"user_id": "owner-1", "window_id": "@0"}
                )
            assert sock.send_json.call_args[0][0]["type"] == "error"
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_delete_agent_shared_terminal_closes_service_session(
        self, user, temp_data_dir, app_state
    ):
        """Agent-owned windows live in the ``service`` tmux session — the
        close must target it, not a session named after the agent's
        user_id (which never exists, so the delete always failed) (#3072)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        ctrl, sock, _ = self._controller(user=user, app_state=app_state)
        ws = self._ws_session(app_state=app_state)
        ws.terminal_windows[model.AGENT_USER_ID] = [
            {"id": "@0", "index": 0, "name": "service-cmd", "shared": True}
        ]
        try:
            _, stop = await self._viewer(
                app_state,
                ws,
                owner_user_id=model.AGENT_USER_ID,
                window_id="@0",
            )
            with (
                patch.object(
                    app_state.state.model.workspaces,
                    "get_workspace_by_id",
                    new=AsyncMock(return_value={"user_id": user["id"]}),
                ),
                patch.object(_mock_term, "close_window") as close,
            ):
                await ctrl.delete_shared_terminal(
                    {"user_id": model.AGENT_USER_ID, "window_id": "@0"}
                )
            stop.assert_awaited_once()
            close.assert_awaited_once_with(
                "cid", ctrl._join_target_for(model.AGENT_USER_ID), "@0"
            )
            assert ws.terminal_windows[model.AGENT_USER_ID] == []
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

    async def test_join_shared_terminal_not_found(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        self, user, temp_data_dir, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        self, user, temp_data_dir, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_connection_broadcast_shared_terminals_delegate(
        self, user, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        conn = _base_conn(user=user, app_state=app_state)
        ws = sockets.get_or_create_session("ws-1", app_state)
        try:
            with patch.object(conn.shared, "broadcast_shared_terminals") as m:
                conn.broadcast_shared_terminals(ws)
            m.assert_called_once_with(ws)
        finally:
            sockets.sessions.pop("ws-1", None)

    async def test_connection_find_window_delegate(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        session = app_state.state.sockets.get_or_create_session(
            "ws-find", app_state
        )
        if windows is not None:
            session.terminal_windows[user["id"]] = windows
        return sock, conn, session

    def _messages(self, sock):
        return [c[0][0] for c in sock.send_json.call_args_list]

    async def test_found_returns_window(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_not_found_sends_error_and_returns_none(
        self, user, app_state
    ):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sock, conn, session = self._setup(user, [], app_state=app_state)
        try:
            assert conn._find_window(session, user["id"], "@99") is None
            assert self._messages(sock) == [
                {"type": "error", "message": "Window not found"}
            ]
        finally:
            sockets.sessions.pop("ws-find", None)

    async def test_shared_true_finds_shared_window(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        sockets = app_state.state.sockets
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
        sockets = app_state.state.sockets
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
        self, user, monkeypatch, agent_user, app_state
    ):
        app_state = _make_app_state()
        registry = app_state.state.container_registry
        monkeypatch.setattr(
            app_state.state.container_registry, "idle_timeout_seconds", 90
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
    async def test_cancelled_cleans_up(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
    async def test_all_subscribers_dead(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_slow_client_closes_connection(self, user, app_state):
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

    async def test_slow_client_in_broadcast(self, app_state):
        """Broadcast drops slow subscribers instead of blocking."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_slow_client_in_exec_forwarding(self, app_state):
        """Exec forwarder handles SlowClientError gracefully."""
        app_state = _make_app_state()
        registry = app_state.state.container_registry
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


class TestRefreshUserHandle:
    async def test_refresh_updates_connections(self, user, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        finally:
            sockets.sessions.pop(workspace["id"], None)
            sockets.connections.pop(sock, None)

    async def test_refresh_no_connections(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        await wshandler.refresh_user_handle(sockets, "nonexistent", "whatever")


class TestBridgeIdleTimeout:
    def test_default(self):
        assert _util().bridge_idle_timeout_for(None) == 30.0

    def test_env_override(self):
        assert (
            _util(
                {"KLANGKD_BRIDGE_TIMEOUT_SECONDS": "45"}
            ).bridge_idle_timeout_for(None)
            == 45.0
        )

    def test_invalid_env_falls_back(self):
        assert (
            _util(
                {"KLANGKD_BRIDGE_TIMEOUT_SECONDS": "nope"}
            ).bridge_idle_timeout_for(None)
            == 30.0
        )


class TestHandleBrowserChunk:
    def test_missing_id(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sockets.handle_browser_chunk({})  # no raise

    def test_unknown_id(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        sockets.handle_browser_chunk({"id": "nope", "delta": "x"})

    def test_wrong_sender_ignored(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    def test_success(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
    def test_done_enqueued(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    def test_wrong_sender_ignored(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
    async def test_streams_chunks_then_done(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_send_failure_yields_error(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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

    async def test_idle_timeout_yields_error(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
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
        sockets = app_state.state.sockets

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

        ws = await app_state.state.workspaces.create_workspace(
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
        sockets = app_state.state.sockets

        ws = await app_state.state.workspaces.create_workspace(
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
        sockets = app_state.state.sockets
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
                    app_state.state.settings, "workspace_token_hours", 1.0
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
        sockets = app_state.state.sockets
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
                    app_state.state.settings, "workspace_token_hours", 0.0001
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
        sockets = app_state.state.sockets
        workspace = await _create_workspace_with_acl(
            app_state, user["id"], "leak-ws"
        )
        session = sockets.get_or_create_session(workspace["id"], app_state)
        session.container_id = "test-cid"

        try:
            with (
                patch.object(
                    app_state.state.settings, "workspace_token_hours", 0.0001
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
        sockets = app_state.state.sockets
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
    async def test_dispatch_ssh_agent_start(self, user, app_state):
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

    async def test_dispatch_ssh_agent_data(self, user, app_state):
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

    async def test_dispatch_ssh_agent_stop(self, user, app_state):
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

    async def test_dispatch_share_window(self, user, app_state):
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

    async def test_dispatch_unshare_window(self, user, app_state):
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

    async def test_dispatch_create_shared_terminal(self, user, app_state):
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

    async def test_dispatch_join_shared_terminal(self, user, app_state):
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

    async def test_dispatch_delete_shared_terminal(self, user, app_state):
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


class TestTokenRenewalFailureLogged:
    async def test_exception_during_renewal_is_logged(self, user, app_state):
        """The except Exception branch in _token_renewal_loop."""
        app_state = _make_app_state()
        ws_session = WorkspaceSession("ws-tok", app_state)
        ws_session.container_id = "test-cid"
        ws_session.workspace_token_expiry = datetime.now(
            timezone.utc
        ) + timedelta(seconds=0.05)
        with (
            patch.object(
                app_state.state.settings, "workspace_token_hours", 0.0001
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


class TestFormatContainerInfo:
    """format_container_info must mirror the real container name (#2286).

    The status-message name is what users see and grep in `podman ps`, so it
    must equal what container.py stamps on the container.
    """

    def test_named_workspace_matches_real_container_name(self):
        from klangk.container import (
            workspace_container_name,
            workspace_name_slug,
        )
        from klangk.wshandler import format_container_info

        ws_id = "abcdef1234567890"
        iid = "inst1"
        name, ports_str = format_container_info(
            ws_id, [9000, 9001], iid, "My Dev Env"
        )
        slug = workspace_name_slug("My Dev Env")
        assert name == workspace_container_name(iid, ws_id, slug)
        assert name == f"klangk-{iid}-{slug}-{ws_id[:8]}"
        assert ports_str == " (ports 9000,9001)"

    def test_symbol_only_name_falls_back_to_id_only(self):
        from klangk.wshandler import format_container_info

        ws_id = "abcdef1234567890"
        iid = "inst1"
        name, ports_str = format_container_info(ws_id, [], iid, "!!!")
        assert name == f"klangk-{iid}-{ws_id[:8]}"
        assert ports_str == ""


class TestDrainingStartPaths:
    """#2527: WS connect/restart on a draining node (graceful restart in
    progress) send an error frame (new starts are refused)."""

    async def test_connect_refused_while_draining(self, user, app_state):
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        with (
            patch.object(
                conn.app.state.acl,
                "get_principals",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                conn.app.state.acl,
                "check_permission",
                new=AsyncMock(return_value=True),
            ),
            patch.object(
                conn.app.state.workspaces,
                "get_workspace",
                new=AsyncMock(return_value={"id": "ws-c", "name": "c"}),
            ),
            patch.object(
                Connection,
                "start_workspace_container",
                new=AsyncMock(
                    side_effect=NodeDrainingError(
                        "node is draining: new workspace starts are disabled"
                    )
                ),
            ),
            patch.object(conn, "handle_workspace_disconnect", new=AsyncMock()),
        ):
            await conn.handle_workspace_connect({"workspaceId": "ws-c"})
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and "draining" in m.get("message", "")
            for m in sent
        )

    async def test_restart_refused_while_draining(self, user, app_state):
        sock = _mock_sock()
        # #3008: the restart path now fans the container_restart notice
        # out through app.state.sockets — wire the real state object in
        # (the minimal app_state fixture doesn't include it).
        app_state.state.sockets = WebSocketState(app_state)
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-c"
        conn.workspace = {"id": "ws-c", "name": "c"}
        with (
            patch.object(conn, "has_perm", new=AsyncMock(return_value=True)),
            patch.object(
                app_state.state.workspaces,
                "get_workspace",
                new=AsyncMock(return_value={"id": "ws-c", "name": "c"}),
            ),
            patch.object(
                Connection,
                "cleanup",
                new=AsyncMock(),
            ),
            patch.object(
                Connection,
                "start_workspace_container",
                new=AsyncMock(
                    side_effect=NodeDrainingError(
                        "node is draining: new workspace starts are disabled"
                    )
                ),
            ),
        ):
            await conn.handle_restart_container()
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            isinstance(m, dict) and "draining" in m.get("message", "")
            for m in sent
        )


class TestNotifyHostShutdown:
    """host_shutdown broadcast for the TERM/INT graceful shutdown (#2527)."""

    def _sockets_with(self, sockets, entries):
        sockets.connections.clear()
        for sock, conn in entries:
            sockets.connections[sock] = conn
        return sockets

    def test_broadcast_reaches_authed_and_drops_dead(self):
        sockets = _make_app_state().state.sockets
        live = _mock_sock()
        anon = _mock_sock()
        dead = _mock_sock()
        dead.send_json.side_effect = RuntimeError("closed")
        self._sockets_with(
            sockets,
            [
                (live, types.SimpleNamespace(user={"id": "u1"})),
                (anon, types.SimpleNamespace(user={})),
                (dead, types.SimpleNamespace(user={"id": "u2"})),
            ],
        )
        sockets.notify_host_shutdown()
        sent = [c[0][0] for c in live.send_json.call_args_list]
        assert sent == [{"type": "host_shutdown"}]
        anon.send_json.assert_not_called()
        assert dead not in sockets.connections
        sockets.connections.clear()


class TestNotifyHostRestart:
    """server_recycle / host_started broadcasts for the SIGHUP graceful
    restart (#2527)."""

    def _sockets_with(self, sockets, entries):
        sockets.connections.clear()
        for sock, conn in entries:
            sockets.connections[sock] = conn
        return sockets

    @pytest.mark.parametrize(
        "method,args,expected",
        [
            (
                "notify_server_recycle",
                ("draining",),
                {"type": "server_recycle", "phase": "draining"},
            ),
            (
                "notify_server_recycle",
                ("recycling",),
                {"type": "server_recycle", "phase": "recycling"},
            ),
            ("notify_host_started", (), {"type": "host_started"}),
        ],
    )
    def test_broadcasts_reach_authed_and_drop_dead(
        self, method, args, expected
    ):
        sockets = _make_app_state().state.sockets
        live = _mock_sock()
        anon = _mock_sock()
        dead = _mock_sock()
        dead.send_json.side_effect = RuntimeError("closed")
        self._sockets_with(
            sockets,
            [
                (live, types.SimpleNamespace(user={"id": "u1"})),
                (anon, types.SimpleNamespace(user={})),
                (dead, types.SimpleNamespace(user={"id": "u2"})),
            ],
        )
        getattr(sockets, method)(*args)
        sent = [c[0][0] for c in live.send_json.call_args_list]
        assert sent == [expected]
        anon.send_json.assert_not_called()
        assert dead not in sockets.connections
        sockets.connections.clear()


class TestServerScheduleSnapshotOnConnect:
    """#2661: a just-connected socket receives the pending-schedule
    snapshot immediately (not only on the scheduler's next tick)."""

    async def test_connect_sends_schedule_snapshot(self, user, app_state):
        from types import SimpleNamespace

        app_state = _make_app_state()
        scheduler = SimpleNamespace(
            send_snapshot_to=AsyncMock(),
        )
        app_state.state.server_scheduler = scheduler

        token = _auth().create_token(user["id"], user["email"])
        websocket = _mock_raw_sock(query_params={"token": token})
        websocket.receive_text = AsyncMock(side_effect=WebSocketDisconnect())

        await handle_websocket(websocket, app_state)

        scheduler.send_snapshot_to.assert_awaited_once()
        # The SafeWebSocket wrapper was passed, not the raw one.
        (safe_ws,) = scheduler.send_snapshot_to.await_args.args
        assert safe_ws is not websocket


class TestWshandlerBranchGaps2834:
    """#2834 branch gate: connection/controller/session outcomes the
    mainline tests only take one side of."""

    async def test_ui_ready_without_session_still_flushes_status(
        self, app_state
    ):
        # A workspace_id whose session is gone (post-teardown UI frame):
        # no browser subscription, the pending status msg still flushes.
        app_state = _make_app_state()
        sock = _mock_sock()
        conn = _base_conn(user=None, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-nosess"
        conn.pending_status_msg = {"type": "status", "state": "connected"}
        with patch.object(
            app_state.state.sockets, "get_session", return_value=None
        ):
            await conn.handle_ui_ready()
        assert conn.pending_status_msg is None
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(
            m.get("event", {}).get("name") == "container_ready" for m in sent
        )

    async def test_set_handle_existing_home_skips_skel(
        self, user, temp_data_dir, app_state, monkeypatch
    ):
        # A rename whose per-handle home already exists (created=False):
        # the skel exec is skipped -- only the symlink is refreshed.
        app_state = _make_app_state()
        # #3135: arm the per-handle ceiling (the stored true column is
        # inert while the deploy flag is off).
        app_state.state.settings.per_handle_home = True
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-skel"
        conn.container_id = "cid-skel"
        conn.workspace = {"id": "ws-skel", "per_handle_home": True}
        monkeypatch.setattr(
            app_state.state.model.users,
            "set_user_handle",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            app_state.state.workspaces,
            "ensure_home_symlink",
            AsyncMock(return_value=("/home/newhandle", False)),
        )
        skel = AsyncMock()
        monkeypatch.setattr(
            app_state.state.workspaces, "populate_home_skel", skel
        )
        await conn.handle_set_handle({"handle": "newhandle"})
        skel.assert_not_awaited()

    @staticmethod
    def _ctrl_conn(app_state, container_id=None):
        sock = _mock_sock()
        conn = _base_conn(user=None, ws=sock, app_state=app_state)
        conn.container_id = container_id
        conn.workspace_id = "ws-c"
        return conn, sock

    async def test_forward_exec_output_without_container_skips_activity(
        self, app_state
    ):
        # An exec on a connection with no container: chunks relay, no
        # activity recording attempt.
        from klangk.wshandler.controllers import ExecController

        app_state = _make_app_state()
        conn, sock = self._ctrl_conn(app_state, container_id=None)
        ctrl = ExecController(conn)
        session = AsyncMock()
        session.returncode = 0

        async def fake_output():
            yield b"x"

        session.output = fake_output
        recorded = []
        with patch.object(
            app_state.state.container_registry,
            "record_activity",
            side_effect=lambda cid: recorded.append(cid),
        ):
            await ctrl.forward_output(session)
        assert recorded == []
        assert any(
            c[0][0]["type"] == "exec_output"
            for c in sock.send_json.call_args_list
        )

    async def test_forward_terminal_output_without_container_skips_activity(
        self, app_state
    ):
        from klangk.wshandler.controllers import TerminalController

        app_state = _make_app_state()
        conn, sock = self._ctrl_conn(app_state, container_id=None)
        conn._user_home = "/home/x"
        ctrl = TerminalController(conn)
        session = AsyncMock()

        async def fake_output():
            yield "text"

        session.output = fake_output
        recorded = []
        with patch.object(
            app_state.state.container_registry,
            "record_activity",
            side_effect=lambda cid: recorded.append(cid),
        ):
            await ctrl.forward_output(session)
        assert recorded == []
        assert any(
            c[0][0]["type"] == "terminal_output"
            for c in sock.send_json.call_args_list
        )

    async def test_join_shared_terminal_without_session_skips_broadcast(
        self, user, temp_data_dir, app_state
    ):
        # The join completes but the workspace session is gone: the
        # terminal_started ack still goes out, no shared-list broadcast
        # (nothing to broadcast to).
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        owner = await app_state.state.model.users.create_user(
            "owner-nosess@test.com", "hash", verified=True
        )
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-nosess"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        registry.track_activity("cid", "ws-nosess")
        broadcast = MagicMock()
        conn.broadcast_shared_terminals = broadcast
        real_session = sockets.get_or_create_session("ws-nosess", app_state)
        real_session.terminal_windows[owner["id"]] = [
            {"name": "build", "index": 0, "id": "@0", "shared": True},
        ]
        # The session is present for the join's lookup, then VANISHES
        # before the post-start broadcast (the mid-join race).
        get_session_calls = {"n": 0}

        def _vanishing_session(workspace_id):
            get_session_calls["n"] += 1
            return real_session if get_session_calls["n"] == 1 else None

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
                patch.object(
                    sockets, "get_session", side_effect=_vanishing_session
                ),
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
            started = [
                c[0][0]
                for c in sock.send_json.call_args_list
                if c[0][0].get("type") == "terminal_started"
            ]
            assert len(started) == 1
            broadcast.assert_not_called()
        finally:
            sockets.sessions.pop("ws-nosess", None)
            registry.states.pop("ws-nosess", None)

    async def test_mark_shared_unknown_window_still_broadcasts(
        self, app_state
    ):
        # The freshly-created window id is absent from the session's list
        # (a racing refresh): nothing is flagged, the broadcast still
        # refreshes the view.
        from klangk.wshandler.controllers import SharedTerminalController

        app_state = _make_app_state()
        conn, sock = self._ctrl_conn(app_state, container_id="cid")
        ctrl = SharedTerminalController(conn)
        broadcast = MagicMock()
        ctrl.broadcast_shared_terminals = broadcast
        sess = MagicMock()
        sess.terminal_windows = {"uid": [{"id": "other", "shared": False}]}
        with patch.object(
            app_state.state.sockets, "get_session", return_value=sess
        ):
            ctrl._mark_window_shared("never-seen")
        assert sess.terminal_windows["uid"][0]["shared"] is False
        broadcast.assert_called_once()

    async def test_refresh_user_handle_skips_other_users(self, app_state):
        # Only the renamed user's connections update; others untouched.
        from klangk.wshandler.support import refresh_user_handle

        app_state = _make_app_state()
        sockets = app_state.state.sockets
        mine = _base_conn(user=None, ws=_mock_sock(), app_state=app_state)
        mine.user["id"] = "u1"
        mine.user["handle"] = "old"
        other = _base_conn(user=None, ws=_mock_sock(), app_state=app_state)
        other.user["id"] = "u2"
        other.user["handle"] = "keepme"
        sockets.connections[mine.sock] = mine
        sockets.connections[other.sock] = other
        try:
            await refresh_user_handle(sockets, "u1", "newhandle")
        finally:
            sockets.connections.clear()
        assert mine.user["handle"] == "newhandle"
        assert other.user["handle"] == "keepme"

    def test_connected_user_ids_skips_agent_subscriber(self, app_state):
        # The agent user's subscription is real (it keeps the workspace
        # alive) but is not a "connected user" for inactivity purposes.
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        agent_conn = _base_conn(
            user=None, ws=_mock_sock(), app_state=app_state
        )
        agent_conn.user["id"] = model.AGENT_USER_ID
        human = _base_conn(user=None, ws=_mock_sock(), app_state=app_state)
        human.user["id"] = "u-human"
        session = WebSocketState(app_state).get_or_create_session(
            "ws-agent", app_state
        )
        session.subscribers.add(agent_conn.sock)
        session.subscribers.add(human.sock)
        sockets.connections[agent_conn.sock] = agent_conn
        sockets.connections[human.sock] = human
        try:
            assert session._connected_user_ids(sockets) == {"u-human"}
        finally:
            sockets.connections.clear()
            sockets.sessions.pop("ws-agent", None)

    def test_send_windows_to_user_skips_other_subscribers(self, app_state):
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        mine = _base_conn(user=None, ws=_mock_sock(), app_state=app_state)
        mine.user["id"] = "u1"
        other_sock = _mock_sock()
        session = WebSocketState(app_state).get_or_create_session(
            "ws-win", app_state
        )
        session.subscribers.add(mine.sock)
        session.subscribers.add(other_sock)
        sockets.connections[mine.sock] = mine
        try:
            session._send_windows_to_user(sockets, "u1", [{"id": "w"}])
        finally:
            sockets.connections.clear()
            sockets.sessions.pop("ws-win", None)
        assert mine.sock.send_json.called
        other_sock.send_json.assert_not_called()

    async def test_clear_cancels_only_undone_browser_requests(self, app_state):
        # A browser-delegate future that already resolved is left alone;
        # the open one is cancelled by the state clear.
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        done.set_result({"type": "reply"})
        open_fut = loop.create_future()
        sockets.pending_browser_requests["a"] = (done, _mock_sock())
        sockets.pending_browser_requests["b"] = (open_fut, _mock_sock())
        await sockets.disconnect_all()
        assert done.done() and done.exception() is None
        assert open_fut.cancelled()

    async def test_browser_reply_done_future_skips_set(self, app_state):
        # A reply racing the request's own timeout: the future is already
        # done, the late frame is dropped without raising.
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        fut.set_result({"type": "first"})
        sock = _mock_sock()
        sockets.pending_browser_requests["rid"] = (fut, sock)
        sockets.handle_browser_response({"type": "reply", "id": "rid"}, sock)
        assert fut.result()["type"] == "first"  # not clobbered


class TestNoCoverAudit2910Part3:
    async def test_input_slow_write_logs_warning(self, app_state, caplog):
        """A terminal write slower than 100ms trips the SLOW warning."""
        import logging

        from klangk.wshandler.controllers import TerminalController

        app_state = _make_app_state()
        conn = SimpleNamespace(
            app=app_state,
            container_id="cid",
            workspace_id="ws-1",
            user={"id": "uid", "email": "a@b.com"},
        )
        ctrl = TerminalController(conn)

        async def slow_write(data):
            await asyncio.sleep(0.15)

        ctrl.session = SimpleNamespace(
            is_alive=True, read_only=False, write=slow_write
        )
        with caplog.at_level(
            logging.WARNING, logger="klangk.wshandler.controllers"
        ):
            await ctrl.input({"data": "x"})
        assert any("terminal_input SLOW" in r.message for r in caplog.records)

    async def test_join_cancelled_stops_session_and_reraises(
        self, user, temp_data_dir, app_state
    ):
        """Cancellation mid-join stops the half-built session and
        re-raises (the connection teardown owns the rest)."""
        app_state = _make_app_state()
        sockets = app_state.state.sockets
        registry = app_state.state.container_registry
        sock = _mock_sock()
        conn = _base_conn(user=user, ws=sock, app_state=app_state)
        conn.workspace_id = "ws-1"
        conn.container_id = "cid"
        conn._user_home = "/home/joiner"
        session = sockets.get_or_create_session("ws-1", app_state)
        session.terminal_windows[user["id"]] = [
            {"name": "work", "index": 0, "id": "@0", "shared": True},
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
                conn.activate_session = AsyncMock(
                    side_effect=asyncio.CancelledError
                )

                await conn.handle_join_shared_terminal(
                    {"user_id": user["id"], "window_id": "@0"}
                )
                # The join runs as a background task: cancellation lands
                # there, and the task re-raises after stopping the session.
                with pytest.raises(asyncio.CancelledError):
                    await conn.terminal_task
            mock_sess.stop.assert_awaited_once()
        finally:
            sockets.sessions.pop("ws-1", None)
            registry.states.pop("ws-1", None)

    async def test_handle_list_error_sends_error_frame(self):
        """The legacy shared-terminal list error handler reports to the
        client socket."""
        from klangk.wshandler.controllers import SharedTerminalController

        sock = _mock_sock()
        conn = SimpleNamespace(sock=sock)
        ctrl = SharedTerminalController(conn)
        await ctrl.handle_list_error(RuntimeError("listing broke"))
        sent = [c[0][0] for c in sock.send_json.call_args_list]
        assert any(m.get("type") == "error" for m in sent)
