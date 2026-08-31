"""Tests for the tmux control-mode window watcher."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from klangk.wshandler.window_watcher import WindowEventWatcher, is_window_event


def test_is_window_event_matches_relevant_events():
    assert is_window_event("%unlinked-window-add @10")
    assert is_window_event("%unlinked-window-close @10")
    assert is_window_event("%unlinked-window-renamed @10 newname")
    assert is_window_event("%window-close @10")
    assert is_window_event("%session-window-changed $0 @10")


def test_is_window_event_ignores_noise():
    # Pane output, the control client's own window, and command framing
    # must never trip a re-sync.
    assert not is_window_event("%output %9 some bytes")
    assert not is_window_event("%window-add @9")
    assert not is_window_event("%session-changed $5 __klangk_ctrl-abc")
    assert not is_window_event("%begin 1 1 0")
    assert not is_window_event("%end 1 1 0")
    assert not is_window_event("")
    assert not is_window_event("not a control line at all")


async def test_read_loop_dispatches_only_relevant_events():
    calls: list[int] = []
    watcher = WindowEventWatcher(MagicMock(), "cid", lambda: calls.append(1))
    lines = [
        b"%output %9 noise\n",
        b"%window-add @9\n",  # control client's own window — ignored
        b"%unlinked-window-add @10\n",  # relevant
        b"%unlinked-window-renamed @10 newname\n",  # relevant (rename)
        b"%session-window-changed $0 @10\n",  # relevant
        b"%begin 1 1 0\n",
        b"",  # EOF
    ]
    stdout = MagicMock()
    stdout.readline = AsyncMock(side_effect=lines)
    watcher._proc = MagicMock(stdout=stdout, returncode=None)
    await watcher.read_loop()
    assert calls == [1, 1, 1]


async def test_read_loop_ignores_dead_socket():
    # No stdout / no proc → no-op, no crash.
    watcher = WindowEventWatcher(MagicMock(), "cid", lambda: None)
    watcher._proc = None
    await watcher.read_loop()  # returns immediately


async def test_read_loop_propagates_cancellation():
    watcher = WindowEventWatcher(MagicMock(), "cid", lambda: None)

    async def block_forever():
        await asyncio.Future()  # never resolves; cancellation lands here

    stdout = MagicMock()
    stdout.readline = block_forever
    watcher._proc = MagicMock(stdout=stdout, returncode=None)
    task = asyncio.create_task(watcher.read_loop())
    await asyncio.sleep(0)  # let it start awaiting readline
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
