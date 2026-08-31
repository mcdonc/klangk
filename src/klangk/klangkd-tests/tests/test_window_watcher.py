"""Tests for the tmux control-mode window watcher."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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


# --- No-cover audit tests (#2910, part 3) --------------------------------


class TestTerminateWatcherProc:
    def _proc(self, **kwargs):
        proc = MagicMock()
        proc.returncode = kwargs.get("returncode")
        proc.terminate = MagicMock(side_effect=kwargs.get("terminate_error"))
        proc.kill = MagicMock(side_effect=kwargs.get("kill_error"))
        proc.wait = AsyncMock(side_effect=kwargs.get("wait_error"))
        return proc

    async def test_already_exited_returns(self):
        from klangk.wshandler.window_watcher import _terminate_watcher_proc

        proc = self._proc(returncode=0)
        await _terminate_watcher_proc(proc)
        proc.terminate.assert_not_called()

    async def test_clean_terminate_waits(self):
        from klangk.wshandler.window_watcher import _terminate_watcher_proc

        proc = self._proc()
        await _terminate_watcher_proc(proc)
        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    async def test_vanished_pid_is_tolerated(self):
        from klangk.wshandler.window_watcher import _terminate_watcher_proc

        proc = self._proc(terminate_error=ProcessLookupError())
        await _terminate_watcher_proc(proc)

    async def test_wait_timeout_falls_back_to_kill(self):
        import asyncio

        from klangk.wshandler.window_watcher import _terminate_watcher_proc

        proc = self._proc(wait_error=asyncio.TimeoutError())
        await _terminate_watcher_proc(proc)
        proc.kill.assert_called_once()

    async def test_kill_race_is_tolerated(self):
        import asyncio

        from klangk.wshandler.window_watcher import _terminate_watcher_proc

        proc = self._proc(
            wait_error=asyncio.TimeoutError(), kill_error=ProcessLookupError()
        )
        await _terminate_watcher_proc(proc)


class TestWatcherLifecycle:
    def _watcher(self, podman=None):
        return WindowEventWatcher(podman or MagicMock(), "cid", lambda: None)

    async def test_start_spawns_and_is_idempotent(self):
        watcher = self._watcher()
        proc = MagicMock(returncode=None)
        stdout = MagicMock()
        stdout.readline = AsyncMock(return_value=b"")  # immediate EOF
        proc.stdout = stdout
        with patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ) as spawn:
            await watcher.start()
            await watcher.start()  # live task: no second spawn
            spawn.assert_called_once()

        watcher2 = self._watcher()
        watcher2._task = asyncio.ensure_future(asyncio.sleep(3600))
        with patch("asyncio.create_subprocess_exec") as spawn:
            await watcher2.start()  # pre-existing live task: no spawn
            spawn.assert_not_called()

    async def test_read_loop_swallows_reader_crash(self):
        watcher = self._watcher()
        stdout = MagicMock()
        stdout.readline = AsyncMock(side_effect=RuntimeError("pty gone"))
        watcher._proc = MagicMock(stdout=stdout, returncode=None)
        await watcher.read_loop()  # must not raise

    async def test_stop_cancels_task_and_terminates_proc(self):
        watcher = self._watcher()
        watcher._task = asyncio.ensure_future(asyncio.sleep(3600))
        proc = MagicMock(returncode=None)
        proc.wait = AsyncMock()
        watcher._proc = proc
        podman = MagicMock()
        podman.exec_container = AsyncMock()
        watcher.podman = podman
        await watcher.stop()
        assert watcher._task is None and watcher._proc is None
        proc.terminate.assert_called_once()

    async def test_stop_without_task_or_proc(self):
        await self._watcher().stop()  # no-arg teardown must not raise

    async def test_kill_ctrl_session_swallows_errors(self):
        watcher = self._watcher()
        watcher.podman.exec_container = AsyncMock(
            side_effect=RuntimeError("container gone")
        )
        await watcher._kill_ctrl_session()  # best-effort: must not raise
        watcher.podman.exec_container = AsyncMock()
        await watcher._kill_ctrl_session()
        watcher.podman.exec_container.assert_awaited_once()
