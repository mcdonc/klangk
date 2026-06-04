"""Tests for terminal: PTY-based ``podman exec`` shell sessions.

The OS/PTY glue (:class:`ShellProcess`) needs a real PTY + podman and is
marked ``# pragma: no cover``; these tests drive :class:`TerminalSession`'s
lifecycle/queue logic against an injected fake shell.
"""

import asyncio
from unittest.mock import patch

from klangk_backend.terminal import TerminalSession, _make_shell_process

SHELL_FACTORY = "klangk_backend.terminal._make_shell_process"


class TestShellProcessFactory:
    """The factory + ctor are pure Python (no PTY); the OS methods that
    need a real PTY/podman are ``# pragma: no cover`` and validated
    interactively."""

    def test_factory_returns_unstarted_shell(self):
        shell = _make_shell_process()
        assert shell._master_fd is None
        assert shell._proc is None


class FakeShell:
    """Stand-in for ShellProcess: scripted reads, recorded writes/resizes."""

    def __init__(
        self,
        chunks=(),
        *,
        start_error=None,
        write_error=None,
        close_error=None,
        block_after_chunks=False,
    ):
        self._chunks = list(chunks)
        self._start_error = start_error
        self._write_error = write_error
        self._close_error = close_error
        self._block = block_after_chunks
        self.argv = None
        self.rows = None
        self.cols = None
        self.writes = []
        self.resizes = []
        self.closed = False

    async def start(self, argv, rows, cols):
        self.argv = argv
        self.rows = rows
        self.cols = cols
        if self._start_error is not None:
            raise self._start_error

    async def read(self):
        if self._chunks:
            item = self._chunks.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        if self._block:
            await asyncio.Event().wait()  # hang until the task is cancelled
        return b""

    async def write(self, data):
        if self._write_error is not None:
            raise self._write_error
        self.writes.append(data)

    def resize(self, rows, cols):
        self.resizes.append((rows, cols))

    def close(self):
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


def _patch(fake):
    return patch(SHELL_FACTORY, return_value=fake)


class TestInit:
    def test_initial_state(self):
        s = TerminalSession("cid")
        assert s.container_id == "cid"
        assert s._shell is None
        assert s._running is False
        assert s.is_alive is False


class TestStart:
    async def test_start_builds_exec_argv(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start(120, 40)

        argv = fake.argv
        assert argv[0] == "exec"
        assert "-t" in argv and "-i" in argv
        assert argv[argv.index("-u") + 1] == "klangk"
        assert argv[argv.index("-w") + 1] == "/home/klangk/work"
        assert "cid" in argv
        assert argv[-1] == "/bin/bash"
        assert (fake.rows, fake.cols) == (40, 120)
        assert s._running is True
        await s.stop()

    async def test_start_unsets_sensitive_env_vars(self, monkeypatch):
        monkeypatch.setenv("KLANGK_LLM_API_KEY", "secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret2")
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()

        argv = fake.argv
        unset = [argv[i + 1] for i, a in enumerate(argv) if a == "-u"]
        assert "KLANGK_LLM_API_KEY" in unset
        assert "ANTHROPIC_API_KEY" in unset
        await s.stop()

    async def test_command_override_sets_env_var(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start(command_override="bash")
        assert "-e" in fake.argv
        assert "KLANGK_CMD_OVERRIDE=bash" in fake.argv
        await s.stop()

    async def test_no_command_override_by_default(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        assert not any("KLANGK_CMD_OVERRIDE" in a for a in fake.argv)
        await s.stop()

    async def test_bridge_token_sets_env_var(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start(bridge_token="tok-123")
        assert "KLANGK_BRIDGE_TOKEN=tok-123" in fake.argv
        await s.stop()

    async def test_no_bridge_token_by_default(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        assert not any("KLANGK_BRIDGE_TOKEN" in a for a in fake.argv)
        await s.stop()

    async def test_start_failure_resets_running(self):
        fake = FakeShell(start_error=RuntimeError("spawn fail"))
        with _patch(fake):
            s = TerminalSession("cid")
            try:
                await s.start()
            except RuntimeError:
                pass
        assert s._running is False
        assert s._shell is None
        assert s.is_alive is False


class TestReadLoop:
    async def test_output_from_shell(self):
        fake = FakeShell(chunks=[b"prompt", b"hello world"])
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.05)
        assert s._output_queue.get_nowait() == "prompt"
        assert s._output_queue.get_nowait() == "hello world"
        await s.stop()

    async def test_stream_end_signals_none(self):
        fake = FakeShell(chunks=[])  # immediate EOF
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.05)
        assert s._output_queue.get_nowait() is None
        await s.stop()

    async def test_read_loop_handles_exception(self):
        fake = FakeShell(chunks=[b"prompt", RuntimeError("connection lost")])
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.05)
        assert s._output_queue.get_nowait() == "prompt"
        # exception in read -> loop ends -> sentinel queued
        assert s._output_queue.get_nowait() is None
        await s.stop()


class TestWrite:
    async def test_write_sends_to_shell(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        await s.write("hello")
        assert fake.writes == [b"hello"]
        await s.stop()

    async def test_write_exception_suppressed(self):
        fake = FakeShell(block_after_chunks=True, write_error=OSError("broke"))
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        await s.write("hello")  # should not raise
        await s.stop()

    async def test_write_when_stopped(self):
        s = TerminalSession("cid")
        await s.write("hello")  # no shell -> no-op


class TestResize:
    async def test_resize_calls_shell_resize(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        await s.resize(200, 50)
        assert fake.resizes == [(50, 200)]  # (rows, cols)
        await s.stop()

    async def test_resize_exception_suppressed(self):
        fake = FakeShell(block_after_chunks=True)

        def boom(rows, cols):
            raise OSError("broke")

        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        fake.resize = boom
        await s.resize(200, 50)  # should not raise
        await s.stop()

    async def test_resize_when_stopped(self):
        s = TerminalSession("cid")
        await s.resize(80, 24)  # no shell -> no-op


class TestStop:
    async def test_stop_cleans_up(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        await s.stop()
        assert s._running is False
        assert s._shell is None
        assert s.is_alive is False
        assert fake.closed is True

    async def test_stop_handles_close_exception(self):
        fake = FakeShell(
            block_after_chunks=True, close_error=OSError("close fail")
        )
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        await s.stop()  # should not raise
        assert s._shell is None

    async def test_stop_cancels_blocked_read_loop(self):
        fake = FakeShell(chunks=[b"prompt"], block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        await asyncio.sleep(0.05)  # consume prompt, then block on read
        await s.stop()
        assert s._running is False
        assert s._shell is None

    async def test_stop_read_task_unexpected_exception(self):
        async def bad_task():
            raise RuntimeError("unexpected")

        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()

        # Replace the read task with one that raises a non-CancelledError.
        s._read_task.cancel()
        try:
            await s._read_task
        except asyncio.CancelledError:
            pass
        s._read_task = asyncio.create_task(bad_task())
        await asyncio.sleep(0)

        await s.stop()  # logs the RuntimeError, does not raise
        assert s._read_task is None

    async def test_stop_when_not_started(self):
        s = TerminalSession("cid")
        await s.stop()


class TestOutput:
    async def test_output_yields_data(self):
        fake = FakeShell(chunks=[b"prompt", b"output"])
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()

        collected = []
        async for data in s.output():
            collected.append(data)
        assert "prompt" in collected
        assert "output" in collected
        await s.stop()

    async def test_output_exits_when_running_cleared(self):
        """Sentinel dropped: output() exits via the _running check."""
        s = TerminalSession("cid")
        s._running = True
        s._read_task = None  # no task -> only _running governs exit

        async def _consume():
            return [data async for data in s.output()]

        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)
        s._running = False
        result = await asyncio.wait_for(task, timeout=3.0)
        assert result == []

    async def test_output_exits_when_read_task_done(self):
        """Sentinel dropped: output() exits via the _read_task.done() check."""
        fake = FakeShell(chunks=[])  # immediate EOF -> read task finishes
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.05)
        assert s._read_task.done()
        # Drain the queued sentinel so output() must rely on the done() check.
        while not s._output_queue.empty():
            s._output_queue.get_nowait()
        assert s._running  # still True; only read_task.done() signals exit

        result = await asyncio.wait_for(
            asyncio.create_task(_collect(s)),
            timeout=3.0,
        )
        assert result == []
        await s.stop()


async def _collect(session):
    return [data async for data in session.output()]


class TestIsAlive:
    async def test_alive_while_running(self):
        fake = FakeShell(chunks=[b"prompt"], block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        assert s.is_alive is True
        await s.stop()

    async def test_not_alive_after_stream_ends(self):
        fake = FakeShell(chunks=[])  # EOF
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        await asyncio.sleep(0.05)
        assert s.is_alive is False
        await s.stop()

    async def test_not_alive_after_stop(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        await s.stop()
        assert s.is_alive is False
