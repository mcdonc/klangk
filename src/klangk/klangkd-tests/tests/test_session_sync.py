"""Tests for WorkspaceSession's tmux window-sync (debounce + re-broadcast)."""

import asyncio
import gc
from unittest.mock import AsyncMock, MagicMock, patch

from klangk.wshandler.session import (
    WorkspaceSession,
    WebSocketState,
    _session_tasks,
    spawn_session_task,
)


def _session_with_user(user_id: str = "u1", handle: str | None = None):
    sock = MagicMock()
    sock.send_json = MagicMock()
    conn = MagicMock()
    conn.user = {"id": user_id}
    if handle is not None:
        conn.user["handle"] = handle
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


# --- #2651: the watcher's sync must broadcast shared_terminals too ---


async def test_sync_broadcasts_shared_terminals_on_shared_rename():
    """A shared window renamed in tmux must reach shared-terminal viewers
    even when the watcher's re-sync is the path that applies it.

    The rename command handler broadcasts ``shared_terminals`` only when
    its merge sees the old→new name delta; under load this debounced
    re-sync can apply the renamed list to the session map first and
    erase that delta. The watcher must then broadcast the shared list
    itself — otherwise other users' tab lists never update (the e2e
    rename test's recvUntil starved exactly this way, #2651).
    """
    sess, sock, terminal = _session_with_user(handle="alice")
    sess.terminal_windows["u1"] = [
        {"id": "@0", "index": 0, "name": "bash", "shared": True}
    ]
    terminal.list_windows = AsyncMock(
        return_value=[
            {"id": "@0", "index": 0, "name": "my-build", "active": True}
        ]
    )

    await sess._sync_windows_once()

    calls = [c[0][0] for c in sock.send_json.call_args_list]
    shared = [m for m in calls if m.get("type") == "shared_terminals"]
    assert len(shared) == 1
    assert [t["window_name"] for t in shared[0]["terminals"]] == ["my-build"]


async def test_sync_shared_rename_broadcast_fires_exactly_once():
    """Whichever path applies a shared rename first broadcasts it; the
    second apply finds no delta and stays quiet (#2651).

    Simulates the CI race: the watcher's re-sync lands between the tmux
    rename and the rename handler's own list_windows, so the handler's
    sync_terminal_windows sees an unchanged shared set.
    """
    sess, sock, terminal = _session_with_user(handle="alice")
    sess.terminal_windows["u1"] = [
        {"id": "@0", "index": 0, "name": "bash", "shared": True}
    ]
    renamed = [{"id": "@0", "index": 0, "name": "my-build", "active": True}]
    terminal.list_windows = AsyncMock(return_value=renamed)

    await sess._sync_windows_once()  # watcher applies the rename first
    # The rename handler's own sync with the same list: no second delta.
    assert sess.apply_window_list("u1", renamed) is False

    calls = [c[0][0] for c in sock.send_json.call_args_list]
    shared = [m for m in calls if m.get("type") == "shared_terminals"]
    assert len(shared) == 1


async def test_sync_no_shared_broadcast_without_shared_delta():
    """A rename of a non-shared window updates terminal_windows only."""
    sess, sock, terminal = _session_with_user(handle="alice")
    sess.terminal_windows["u1"] = [
        {"id": "@0", "index": 0, "name": "bash", "shared": False}
    ]
    terminal.list_windows = AsyncMock(
        return_value=[{"id": "@0", "index": 0, "name": "dev", "active": True}]
    )

    await sess._sync_windows_once()

    calls = [c[0][0] for c in sock.send_json.call_args_list]
    assert [m for m in calls if m.get("type") == "shared_terminals"] == []


def test_apply_window_list_reports_shared_deltas():
    """apply_window_list flags exactly the changes viewers must hear
    about: a shared window added/closed, or any shared rename."""
    sess, _sock, _terminal = _session_with_user()
    sess.terminal_windows["u1"] = [
        {"id": "@0", "index": 0, "name": "bash", "shared": True},
        {"id": "@1", "index": 1, "name": "aux", "shared": False},
    ]

    # Same windows (active flag aside) → no delta.
    assert (
        sess.apply_window_list(
            "u1",
            [
                {"id": "@0", "index": 0, "name": "bash", "active": True},
                {"id": "@1", "index": 1, "name": "aux", "active": False},
            ],
        )
        is False
    )
    # Shared window renamed → delta.
    assert (
        sess.apply_window_list(
            "u1",
            [
                {"id": "@0", "index": 0, "name": "my-build", "active": True},
                {"id": "@1", "index": 1, "name": "aux", "active": False},
            ],
        )
        is True
    )
    # Shared window closed → delta.
    assert (
        sess.apply_window_list(
            "u1", [{"id": "@1", "index": 0, "name": "aux", "active": True}]
        )
        is True
    )
    # Map reflects the last applied list, shared flags carried by id.
    assert sess.terminal_windows["u1"] == [
        {"id": "@1", "index": 0, "name": "aux", "shared": False}
    ]


# --- #2653: stale in-flight watcher snapshots must be discarded ---


async def test_sync_discards_stale_inflight_snapshot():
    """A watcher snapshot racing a command-handler apply is dropped.

    The watcher's ``list_windows`` is a podman exec round-trip: under
    load it can start before a tmux rename commits and return after
    the rename handler already applied and broadcast the renamed
    list. Applying the stale pre-rename snapshot would revert the map,
    the baseline, and the client frames (new → old → new flap,
    #2653). The generation stamped before the exec must discard it.
    """
    sess, sock, terminal = _session_with_user()
    # Seeded shared: the concurrent apply below must report a shared
    # rename delta — the issue's worst case (a revert broadcast to
    # shared_terminals viewers), and what the recorded-delta assert
    # at the end checks for.
    sess.terminal_windows["u1"] = [
        {"id": "@0", "index": 0, "name": "bash", "shared": True}
    ]
    stale = [{"id": "@0", "index": 0, "name": "bash", "active": True}]
    renamed = [{"id": "@0", "index": 0, "name": "my-build", "active": True}]
    deltas: list[bool] = []

    async def straddling_exec(container_id, uid):
        # While the watcher's exec is in flight, the rename handler
        # commits and applies (and would broadcast) the renamed list.
        # A real handler's notify_user_terminal_windows also refreshes
        # the watcher's baseline. The apply's return value is recorded
        # rather than asserted here — this coroutine runs inside
        # _sync_windows_once's except-Exception guard, which would
        # swallow an AssertionError and turn the failure into a
        # baffling KeyError on _last_windows below.
        deltas.append(sess.apply_window_list("u1", renamed))
        sess._last_windows["u1"] = renamed
        return stale  # our exec queried tmux before the rename committed

    terminal.list_windows = AsyncMock(side_effect=straddling_exec)

    await sess._sync_windows_once()

    # The concurrent handler's apply did see (and broadcast) the
    # shared rename — and the watcher's stale snapshot was discarded
    # after it: the map and the baseline still hold the renamed list
    # and no frame was broadcast for the revert.
    assert deltas == [True]
    assert sess.terminal_windows["u1"][0]["name"] == "my-build"
    assert sess._last_windows["u1"] == renamed
    sock.send_json.assert_not_called()


async def test_sync_applies_change_when_generation_unmoved():
    """A genuine tmux-side change (no concurrent command-handler apply)
    still passes the generation check and broadcasts (#2653 guard must
    not eat real watcher deltas — e.g. a rename typed inside tmux)."""
    sess, sock, terminal = _session_with_user()
    terminal.list_windows = AsyncMock(
        return_value=[{"id": "@0", "index": 0, "name": "dev", "active": True}]
    )

    await sess._sync_windows_once()

    assert sess.terminal_windows["u1"][0]["name"] == "dev"
    sock.send_json.assert_called_once()


def test_apply_window_list_bumps_generation():
    """Every apply is the newest applied state: the per-user generation
    advances on each apply_window_list call (#2653)."""
    sess, _sock, _terminal = _session_with_user()
    windows = [{"id": "@0", "index": 0, "name": "bash", "active": True}]

    assert sess._window_generations.get("u1", 0) == 0
    sess.apply_window_list("u1", windows)
    assert sess._window_generations["u1"] == 1
    sess.apply_window_list("u1", windows)
    assert sess._window_generations["u1"] == 2


# --- #2652 review follow-ups ---


def test_get_or_create_session_falls_back_to_state_app():
    """Omitting ``app`` still yields a session wired to the state's app.

    A session built with ``app=None`` silently skips every
    ``shared_terminals`` broadcast (the guard in
    ``broadcast_shared_terminals``), so the factory must never produce
    one even when a caller forgets the argument (#2652 review).
    """
    app = MagicMock()
    sockets = WebSocketState(app=app)

    sess = sockets.get_or_create_session("ws")

    assert sess.app is app


def test_get_or_create_session_explicit_app_wins():
    """An explicitly passed ``app`` is honored over the state's own."""
    sockets = WebSocketState(app=MagicMock())
    other = MagicMock()

    sess = sockets.get_or_create_session("ws", other)

    assert sess.app is other


def test_broadcast_shared_terminals_noop_without_app():
    """A bare session (``app=None``) broadcasts nothing.

    Covers the defensive guard instead of hiding it behind a pragma —
    with the ``get_or_create_session`` fallback this is unreachable in
    production, but the constructor still permits it.
    """
    sock = MagicMock()
    sock.send_json = MagicMock()
    sess = WorkspaceSession("ws")
    sess.subscribers.add(sock)
    sess.terminal_windows["u1"] = [
        {"id": "@0", "index": 0, "name": "bash", "shared": True}
    ]

    sess.broadcast_shared_terminals()

    sock.send_json.assert_not_called()


# --- No-cover audit tests (#2910, part 3) --------------------------------


async def test_sync_continues_when_list_windows_fails():
    """A container mid-restart (list_windows raising) is skipped for that
    user, not fatal for the sweep."""
    sess, sock, terminal = _session_with_user()
    terminal.list_windows = AsyncMock(side_effect=RuntimeError("gone"))
    await sess._sync_windows_once()  # must not raise
    sock.send_json.assert_not_called()


async def test_reset_stops_window_watcher():
    sess, _, _ = _session_with_user()
    watcher = MagicMock()
    watcher.stop = AsyncMock()
    sess._window_watcher = watcher
    with patch("asyncio.create_task") as create_task:
        await sess.reset()
    create_task.assert_called_once()
    watcher.stop.assert_not_awaited()  # scheduled, not awaited inline


async def test_start_window_sync_schedules_watcher_start():
    sess, _, _ = _session_with_user()
    sess.container_id = "cid"
    with patch("klangk.wshandler.session.WindowEventWatcher") as cls:
        cls.return_value.start = AsyncMock()
        with patch("asyncio.create_task") as create_task:
            sess.start_window_sync()
        create_task.assert_called_once()


async def test_dispatch_window_sync_schedules_once():
    sess, _, _ = _session_with_user()
    with patch("asyncio.create_task") as create_task:
        sess._dispatch_window_sync()
    create_task.assert_called_once()


async def test_token_renewal_loop_exits_without_expiry():
    sess, _, _ = _session_with_user()
    sess.workspace_token_expiry = None
    await sess._token_renewal_loop()  # returns immediately


async def test_token_renewal_loop_exits_without_container():
    sess, _, _ = _session_with_user()
    import time as time_mod

    sess.workspace_token_expiry = time_mod.time() + 3600
    sess.container_id = None
    with patch("klangk.wshandler.session.asyncio.sleep", new=AsyncMock()):
        await sess._token_renewal_loop()  # renewal unreachable: exit


# --- #2913: fire-and-forget tasks must be strongly referenced ---------


async def test_spawn_session_task_holds_strong_reference_until_done():
    """A spawned task stays referenced while in flight, is discarded on done.

    An unreferenced task suspended in an await is GC-eligible
    mid-execution (#2913); the module-level set must hold it until it
    completes, then drop it so the set cannot grow unboundedly.
    """
    started = asyncio.Event()

    async def hangs() -> None:
        started.set()
        await asyncio.sleep(30)

    task = spawn_session_task(hangs())
    await started.wait()
    assert task in _session_tasks  # referenced while suspended in an await

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert task not in _session_tasks  # done-callback discarded it


async def test_reset_stop_task_survives_session_drop():
    """The ``watcher.stop()`` task outlives the dying session (#2913).

    ``reset()`` runs after the session is popped from the sockets map,
    so an instance-attribute reference set would be collected with the
    session and strand the teardown task again. The module-level set
    keeps the still-in-flight stop alive even with no other reference.
    """
    sess, _, _ = _session_with_user()
    watcher = MagicMock()
    stopping = asyncio.Event()
    before = set(_session_tasks)

    async def slow_stop() -> None:
        stopping.set()
        await asyncio.sleep(30)

    watcher.stop = slow_stop
    sess._window_watcher = watcher

    await sess.reset()
    del watcher, sess  # no external references to the session or watcher
    gc.collect()
    await stopping.wait()
    # The stop task is still alive and referenced module-level even
    # though its session (and every local reference) is gone; finish it.
    pending = {t for t in _session_tasks if t not in before and not t.done()}
    assert pending, "the in-flight watcher.stop() task lost its reference"
    for t in pending:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        assert t not in _session_tasks  # discarded by the done-callback
