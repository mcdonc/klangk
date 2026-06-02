"""Tests for dockerexec: raw docker exec without PTY."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from klangk_backend.dockerexec import ExecSession


def _mock_proc(stdout_data=b"", returncode=None):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdin = AsyncMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    stdout = AsyncMock()
    _chunks = [stdout_data] if stdout_data else []
    _idx = [0]

    async def _read(n):
        if _idx[0] < len(_chunks):
            chunk = _chunks[_idx[0]]
            _idx[0] += 1
            return chunk
        return b""

    stdout.read = _read
    proc.stdout = stdout
    proc.wait = AsyncMock()
    return proc


class TestExecSession:
    async def test_start_and_output(self):
        session = ExecSession("cid")
        proc = _mock_proc(b"hello world")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["echo", "hello"])
        assert session.is_alive

        chunks = []
        async for data in session.output():
            chunks.append(data)
        assert b"hello world" in b"".join(chunks)

    async def test_write_sends_to_stdin(self):
        session = ExecSession("cid")
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["cat"])
        await session.write(b"input data")
        proc.stdin.write.assert_called_with(b"input data")

    async def test_close_stdin(self):
        session = ExecSession("cid")
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["cat"])
        await session.close_stdin()
        proc.stdin.close.assert_called_once()

    async def test_stop_terminates_process(self):
        session = ExecSession("cid")
        proc = _mock_proc(b"", returncode=None)
        proc.wait = AsyncMock()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["sleep", "10"])
        await session.stop()
        proc.terminate.assert_called_once()
        assert session._proc is None

    async def test_stop_kills_on_timeout(self):
        session = ExecSession("cid")
        proc = _mock_proc(b"", returncode=None)
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["sleep", "10"])
        await session.stop()
        proc.kill.assert_called_once()

    async def test_returncode(self):
        session = ExecSession("cid")
        proc = _mock_proc(b"", returncode=42)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["false"])
        assert session.returncode == 42

    async def test_returncode_none_when_no_proc(self):
        session = ExecSession("cid")
        assert session.returncode is None

    async def test_is_alive_false_when_no_proc(self):
        session = ExecSession("cid")
        assert not session.is_alive

    async def test_write_noop_when_no_proc(self):
        session = ExecSession("cid")
        await session.write(b"data")  # should not raise

    async def test_close_stdin_noop_when_no_proc(self):
        session = ExecSession("cid")
        await session.close_stdin()  # should not raise

    async def test_stop_noop_when_no_proc(self):
        session = ExecSession("cid")
        await session.stop()  # should not raise

    async def test_read_stdout_handles_oserror(self):
        session = ExecSession("cid")
        proc = _mock_proc(b"")
        proc.stdout.read = AsyncMock(side_effect=OSError("broken"))
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["cat"])
        # _read_stdout should have queued None
        data = await session._output_queue.get()
        assert data is None

    async def test_read_task_held_after_start(self):
        session = ExecSession("cid")
        assert session._read_task is None
        proc = _mock_proc(b"hello")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["echo", "hello"])
        assert session._read_task is not None
        assert isinstance(session._read_task, asyncio.Task)

    async def test_stop_cancels_read_task(self):
        session = ExecSession("cid")
        proc = _mock_proc(b"", returncode=None)
        proc.wait = AsyncMock()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["sleep", "10"])
        assert session._read_task is not None
        await session.stop()
        assert session._read_task is None

    async def test_read_stdout_reraises_cancelled_error(self):
        session = ExecSession("cid")
        proc = _mock_proc(b"")
        blocked = asyncio.Event()

        async def _blocking_read(n):
            blocked.set()
            await asyncio.sleep(999)

        proc.stdout.read = _blocking_read
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["cat"])
        # Wait until the read task is blocked inside stdout.read
        await blocked.wait()
        session._read_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session._read_task

    async def test_is_alive_false_when_read_task_done(self):
        session = ExecSession("cid")
        proc = _mock_proc(b"output")
        # returncode stays None so is_alive would be True without
        # the read_task.done() check
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["echo", "output"])
        # Wait for the read task to finish (it reads one chunk then EOF)
        await session._read_task
        assert session._read_task.done()
        assert not session.is_alive
