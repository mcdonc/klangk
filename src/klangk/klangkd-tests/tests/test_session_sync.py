"""Tests for WorkspaceSession's tmux window-sync (debounce + re-broadcast)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from klangk.wshandler.session import WorkspaceSession


def _session_with_user(user_id: str = "u1"):
    sock = MagicMock()
    sock.send_json = MagicMock()
    conn = MagicMock()
    conn.user = {"id": user_id}
    sockets = MagicMock()
    sockets.connections = {sock: conn}
    terminal = MagicMock()
    app = MagicMock()
    app.state.sockets = sockets
    app.state.terminal = terminal
    sess = WorkspaceSession("ws", app)
    sess.container_id = "cid"
    sess.subscribers.add(sock)
    return sess, sock, terminal


async def test_sync_broadcasts_on_change_then_skips_unchanged():
    sess, sock, terminal = _session_with_user()
    windows = [{"id": "@0", "index": 0, "name": "bash", "active": True}]
    terminal.list_windows = AsyncMock(return_value=windows)

    await sess._sync_windows_once()
    sock.send_json.assert_called_once_with(
        {"type": "terminal_windows", "windows": windows}
    )

    # Same windows again → no re-broadcast.
    sock.send_json.reset_mock()
    await sess._sync_windows_once()
    sock.send_json.assert_not_called()


async def test_sync_broadcasts_when_active_window_changes():
    sess, sock, terminal = _session_with_user()
    terminal.list_windows = AsyncMock(
        return_value=[{"id": "@0", "index": 0, "name": "bash", "active": True}]
    )
    await sess._sync_windows_once()

    # Active switches to window @1 → change detected → re-broadcast.
    sock.send_json.reset_mock()
    terminal.list_windows = AsyncMock(
        return_value=[
            {"id": "@0", "index": 0, "name": "bash", "active": False},
            {"id": "@1", "index": 1, "name": "1", "active": True},
        ]
    )
    await sess._sync_windows_once()
    sock.send_json.assert_called_once()


async def test_sync_swallows_dead_socket():
    # A subscriber whose socket just died (send_json raises a WS_ERRORS
    # member) must not abort the broadcast.
    sess, sock, terminal = _session_with_user()
    terminal.list_windows = AsyncMock(
        return_value=[{"id": "@0", "index": 0, "name": "bash", "active": True}]
    )
    sock.send_json.side_effect = RuntimeError("dead socket")
    await sess._sync_windows_once()  # no raise


async def test_schedule_window_sync_debounces_bursts():
    sess, _sock, _terminal = _session_with_user()
    calls: list[int] = []
    sess._sync_windows_once = AsyncMock(side_effect=lambda: calls.append(1))

    sess._schedule_window_sync()
    first = sess._window_sync_handle
    sess._schedule_window_sync()  # burst — must cancel the first, reschedule
    assert first is not None and first.cancelled()

    await asyncio.sleep(0.3)  # past the 0.15s debounce
    assert calls == [1]


async def test_sync_noop_without_container_or_subscribers():
    sess, _sock, terminal = _session_with_user()
    terminal.list_windows = AsyncMock(return_value=[])
    sess.container_id = None
    await sess._sync_windows_once()  # no container → no-op
    assert terminal.list_windows.await_count == 0


async def test_start_window_sync_creates_watcher_once():
    sess, _sock, _terminal = _session_with_user()
    with patch("klangk.wshandler.session.WindowEventWatcher") as wc:
        wc.return_value.start = AsyncMock()
        sess.start_window_sync()
        assert wc.call_count == 1
        sess.start_window_sync()  # idempotent — no second watcher
        assert wc.call_count == 1
        await asyncio.sleep(0.05)  # let the start task drain


def test_start_window_sync_noop_without_container():
    sess, _sock, _terminal = _session_with_user()
    sess.container_id = None
    sess.start_window_sync()
    assert sess._window_watcher is None


async def test_reset_cancels_pending_sync_and_stops_watcher():
    sess, _sock, _terminal = _session_with_user()
    with patch("klangk.wshandler.session.WindowEventWatcher") as wc:
        wc.return_value.start = AsyncMock()
        wc.return_value.stop = AsyncMock()
        sess.start_window_sync()
        # arm a pending sync handle so reset's cancel branch runs
        loop = asyncio.get_running_loop()
        sess._window_sync_handle = loop.call_later(10, lambda: None)
        await sess.reset()
        assert sess._window_watcher is None
        assert sess._window_sync_handle is None


# --- #2633 CI race: the watcher's sync must update the in-memory map ---


async def test_sync_updates_terminal_windows_map():
    """A watcher frame must not advertise windows the map lacks.

    ``klangk terminal share`` resolves a window from the
    ``terminal_windows`` frame and immediately sends ``share_window``;
    the handler reads ``terminal_windows`` from the session map. When
    the watcher's frame beat ``_start_terminal``'s sync (the #2633 CI
    flake), the map was still empty and the share failed with a
    "Window not found" the CLI blindly timed out on. The map and the
    broadcast must move together.
    """
    sess, sock, terminal = _session_with_user()
    windows = [{"id": "@0", "index": 0, "name": "bash", "active": True}]
    terminal.list_windows = AsyncMock(return_value=windows)

    await sess._sync_windows_once()

    assert sess.terminal_windows["u1"] == [
        {"id": "@0", "index": 0, "name": "bash", "shared": False}
    ]
    sock.send_json.assert_called_once()


async def test_sync_map_preserves_shared_flags_and_forces_service_cmd():
    """The map update uses the shared merge: flags carry over by id and
    service-cmd is shared by definition (#1114)."""
    sess, _sock, terminal = _session_with_user()
    sess.terminal_windows["u1"] = [
        {"id": "@1", "index": 1, "name": "build", "shared": True},
        {"id": "@0", "index": 0, "name": "bash", "shared": False},
    ]
    terminal.list_windows = AsyncMock(
        return_value=[
            {"id": "@0", "index": 0, "name": "bash", "active": True},
            {"id": "@1", "index": 1, "name": "build", "active": False},
            {"id": "@2", "index": 2, "name": "service-cmd", "active": False},
        ]
    )

    await sess._sync_windows_once()

    by_id = {w["id"]: w for w in sess.terminal_windows["u1"]}
    assert by_id["@1"]["shared"] is True  # carried over
    assert by_id["@2"]["shared"] is True  # service-cmd by definition
    assert by_id["@0"]["shared"] is False
