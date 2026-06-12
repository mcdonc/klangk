"""Tests for terminal: PTY-based ``podman exec`` shell sessions.

The OS/PTY glue (:class:`ShellProcess`) needs a real PTY + podman and is
marked ``# pragma: no cover``; these tests drive :class:`TerminalSession`'s
lifecycle/queue logic against an injected fake shell.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from klangk_backend.terminal import (
    TerminalSession,
    _make_shell_process,
    _session_name,
    close_window,
    list_windows,
    new_window,
    rename_window,
    select_window,
    tmux_command,
)

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


def _drain_text(session):
    """Concatenate queued text output up to the end-of-stream sentinel."""
    out = ""
    while not session._output_queue.empty():
        item = session._output_queue.get_nowait()
        if item is None:
            break
        out += item
    return out


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
        assert argv[argv.index("-w") + 1] == "/home"
        assert "cid" in argv
        assert argv[-2:] == ["tmux", "new-session"]
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

    async def test_user_home_sets_home_env(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid", user_home="/home/alice")
            await s.start(120, 40)
        assert "HOME=/home/alice" in fake.argv
        # Work dir is always /home (bash cd's to $HOME on login)
        assert fake.argv[fake.argv.index("-w") + 1] == "/home"
        # HOME is also passed to tmux via -e so child shells inherit it
        tmux_idx = fake.argv.index("tmux")
        tmux_tail = fake.argv[tmux_idx:]
        assert "-e" in tmux_tail
        assert "HOME=/home/alice" in tmux_tail
        # Session named after handle; -A reattaches on reconnect
        assert "-A" in tmux_tail
        assert tmux_tail[tmux_tail.index("-s") + 1] == "alice"
        await s.stop()

    async def test_no_user_home_by_default(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        assert not any(a.startswith("HOME=") for a in fake.argv)
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

    async def test_read_loop_reassembles_split_utf8(self):
        # '─' (U+2500) is e2 94 80; split across two reads. A per-chunk
        # decode would mangle it into replacement chars; the incremental
        # decoder must buffer the partial sequence and emit the intact glyph.
        fake = FakeShell(chunks=[b"\xe2\x94", b"\x80 done"])
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.05)
        out = _drain_text(s)
        assert out == "─ done"
        assert "�" not in out
        await s.stop()

    async def test_read_loop_flushes_incomplete_trailing_bytes(self):
        # Stream ends mid-character: the buffered partial sequence is flushed
        # as a single replacement char rather than silently dropped.
        fake = FakeShell(chunks=[b"ok\xe2\x94"])  # ends mid '─'
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.05)
        assert _drain_text(s) == "ok�"
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


class TestSessionName:
    def test_extracts_handle(self):
        assert _session_name("/home/alice") == "alice"

    def test_none(self):
        assert _session_name(None) is None


class TestTmuxCommand:
    async def test_returns_stdout(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"output\n", b""))
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await tmux_command("cid", "sess", ["list-windows"])
        assert result == "output\n"

    async def test_raises_on_failure(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"error msg"))
        proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="error msg"):
                await tmux_command("cid", "sess", ["bad-cmd"])


class TestListWindows:
    async def test_parses_output(self):
        with patch(
            "klangk_backend.terminal.tmux_command",
            return_value="0|||bash|||1\n1|||build|||0\n",
        ):
            result = await list_windows("cid", "sess")
        assert result == [
            {"index": 0, "name": "bash", "active": True},
            {"index": 1, "name": "build", "active": False},
        ]

    async def test_empty_session(self):
        with patch("klangk_backend.terminal.tmux_command", return_value=""):
            result = await list_windows("cid", "sess")
        assert result == []


class TestNewWindow:
    async def test_creates_window_auto_name(self):
        call_count = [0]

        async def fake_list(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return [{"index": 0, "name": "bash", "active": True}]
            return [
                {"index": 0, "name": "bash", "active": False},
                {"index": 1, "name": "1", "active": True},
            ]

        with (
            patch(
                "klangk_backend.terminal.tmux_command",
                return_value="",
            ) as mock_cmd,
            patch(
                "klangk_backend.terminal.list_windows",
                side_effect=fake_list,
            ),
        ):
            result = await new_window("cid", "sess")
        mock_cmd.assert_called_once_with(
            "cid", "sess", ["new-window", "-t", "sess", "-n", "1"]
        )
        assert len(result) == 2

    async def test_auto_name_skips_existing(self):
        call_count = [0]

        async def fake_list(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return [
                    {"index": 0, "name": "bash", "active": True},
                    {"index": 1, "name": "1", "active": False},
                ]
            return [
                {"index": 0, "name": "bash", "active": False},
                {"index": 1, "name": "1", "active": False},
                {"index": 2, "name": "2", "active": True},
            ]

        with (
            patch(
                "klangk_backend.terminal.tmux_command",
                return_value="",
            ) as mock_cmd,
            patch(
                "klangk_backend.terminal.list_windows",
                side_effect=fake_list,
            ),
        ):
            result = await new_window("cid", "sess")
        mock_cmd.assert_called_once_with(
            "cid", "sess", ["new-window", "-t", "sess", "-n", "2"]
        )
        assert len(result) == 3

    async def test_creates_named_window(self):
        call_count = [0]

        async def fake_list(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: duplicate check — no existing "build"
                return [{"index": 0, "name": "bash", "active": True}]
            # Second call: after creation
            return [
                {"index": 0, "name": "bash", "active": False},
                {"index": 1, "name": "build", "active": True},
            ]

        with (
            patch(
                "klangk_backend.terminal.tmux_command",
                return_value="",
            ) as mock_cmd,
            patch(
                "klangk_backend.terminal.list_windows",
                side_effect=fake_list,
            ),
        ):
            result = await new_window("cid", "sess", name="build")
        mock_cmd.assert_called_once_with(
            "cid", "sess", ["new-window", "-t", "sess", "-n", "build"]
        )
        assert len(result) == 2

    async def test_rejects_duplicate_name(self):
        with patch(
            "klangk_backend.terminal.list_windows",
            return_value=[{"index": 0, "name": "build", "active": True}],
        ):
            with pytest.raises(ValueError, match="already exists"):
                await new_window("cid", "sess", name="build")


class TestSelectWindow:
    async def test_selects_window(self):
        with patch(
            "klangk_backend.terminal.tmux_command",
            return_value="",
        ) as mock_cmd:
            await select_window("cid", "sess", 2)
        mock_cmd.assert_called_once_with(
            "cid", "sess", ["select-window", "-t", "sess:2"]
        )


class TestCloseWindow:
    async def test_closes_window(self):
        with (
            patch(
                "klangk_backend.terminal.tmux_command",
                return_value="",
            ) as mock_cmd,
            patch(
                "klangk_backend.terminal.list_windows",
                return_value=[{"index": 0, "name": "bash", "active": True}],
            ),
        ):
            result = await close_window("cid", "sess", 1)
        mock_cmd.assert_called_once_with(
            "cid", "sess", ["kill-window", "-t", "sess:1"]
        )
        assert len(result) == 1


class TestRenameWindow:
    async def test_renames_window(self):
        with (
            patch(
                "klangk_backend.terminal.list_windows",
                return_value=[
                    {"index": 0, "name": "bash", "active": True},
                ],
            ),
            patch(
                "klangk_backend.terminal.tmux_command",
                return_value="",
            ) as mock_cmd,
        ):
            await rename_window("cid", "sess", 0, "build")
        mock_cmd.assert_called_once_with(
            "cid", "sess", ["rename-window", "-t", "sess:0", "build"]
        )

    async def test_rejects_duplicate_name(self):
        with patch(
            "klangk_backend.terminal.list_windows",
            return_value=[
                {"index": 0, "name": "bash", "active": True},
                {"index": 1, "name": "build", "active": False},
            ],
        ):
            with pytest.raises(ValueError, match="already exists"):
                await rename_window("cid", "sess", 0, "build")
