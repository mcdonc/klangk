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
    _build_shell_command,
    _make_shell_process,
    close_window,
    kill_joiner_sessions,
    list_windows,
    load_workspace_state,
    new_window,
    rename_window,
    restore_windows,
    save_workspace_state,
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
            s = TerminalSession(
                "cid",
                session_name="uid-123",
                user_home="/home/alice",
                user_id="uid-123",
                user_handle="alice",
            )
            await s.start(120, 40)
        assert "HOME=/home/alice" in fake.argv
        assert "KLANGK_USER_ID=uid-123" in fake.argv
        assert "KLANGK_USER_HANDLE=alice" in fake.argv
        # Work dir is always /home (bash cd's to $HOME on login)
        assert fake.argv[fake.argv.index("-w") + 1] == "/home"
        # HOME is also passed to tmux via -e so child shells inherit it
        tmux_idx = fake.argv.index("tmux")
        tmux_tail = fake.argv[tmux_idx:]
        assert "-e" in tmux_tail
        assert "HOME=/home/alice" in tmux_tail
        # Session named by user_id; -A reattaches on reconnect
        assert "-A" in tmux_tail
        assert tmux_tail[tmux_tail.index("-s") + 1] == "uid-123"
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

    async def test_stop_kills_tmux_session_with_socket(self):
        """When socket_path is set, stop() kills the tmux session."""
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession(
                "cid", session_name="uid", socket_path="/tmp/s.sock"
            )
            await s.start()
        # Manually set tmux session name (normally set by _build_shell_command
        # when join_session is used with a socket path)
        s._tmux_session_name = "uid-abc123"
        proc_mock = AsyncMock()
        proc_mock.wait = AsyncMock(return_value=0)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc_mock,
        ) as mock_exec:
            await s.stop()
        mock_exec.assert_called_once()
        argv = mock_exec.call_args[0]
        assert "kill-session" in argv
        assert "-t" in argv
        assert "uid-abc123" in argv


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

    async def test_retries_on_socket_not_found(self):
        fail_proc = AsyncMock()
        fail_proc.communicate = AsyncMock(
            return_value=(b"", b"No such file or directory")
        )
        fail_proc.returncode = 1
        ok_proc = AsyncMock()
        ok_proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
        ok_proc.returncode = 0
        with (
            patch(
                "asyncio.create_subprocess_exec",
                side_effect=[fail_proc, ok_proc],
            ),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await tmux_command("cid", "sess", ["list-windows"])
        assert result == "ok\n"
        mock_sleep.assert_awaited_once_with(0.5)


class TestListWindows:
    async def test_parses_output(self):
        with patch(
            "klangk_backend.terminal.tmux_command",
            return_value="@0|||0|||bash|||1\n@1|||1|||build|||0\n",
        ):
            result = await list_windows("cid", "sess")
        assert result == [
            {"id": "@0", "index": 0, "name": "bash", "active": True},
            {"id": "@1", "index": 1, "name": "build", "active": False},
        ]

    async def test_empty_session(self):
        with patch("klangk_backend.terminal.tmux_command", return_value=""):
            result = await list_windows("cid", "sess")
        assert result == []


class TestNewWindow:
    async def test_creates_window_auto_name(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"@0|||0|||1|||1\n", b""))
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await new_window("cid", "sess")
        assert len(result) == 1
        assert result[0]["name"] == "1"

    async def test_auto_name_skips_existing(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(b"@0|||0|||1|||0\n@1|||1|||2|||1\n", b"")
        )
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await new_window("cid", "sess")
        assert len(result) == 2

    async def test_creates_named_window(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(b"@0|||0|||bash|||0\n@1|||1|||build|||1\n", b"")
        )
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await new_window("cid", "sess", name="build")
        assert len(result) == 2
        assert result[1]["name"] == "build"

    async def test_rejects_duplicate_name(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"DUPLICATE\n", b""))
        proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(ValueError, match="already exists"):
                await new_window("cid", "sess", name="build")

    async def test_raises_on_tmux_error(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"session not found"))
        proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(RuntimeError, match="session not found"):
                await new_window("cid", "sess")


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

    async def test_selects_by_window_id(self):
        with patch(
            "klangk_backend.terminal.tmux_command",
        ) as mock_cmd:
            await select_window("cid", "sess", "@5")
        mock_cmd.assert_called_once_with(
            "cid", "sess", ["select-window", "-t", "@5"]
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

    async def test_closes_by_window_id(self):
        with (
            patch(
                "klangk_backend.terminal.tmux_command",
                return_value="",
            ) as mock_cmd,
            patch(
                "klangk_backend.terminal.list_windows",
                return_value=[],
            ),
        ):
            await close_window("cid", "sess", "@3")
        mock_cmd.assert_called_once_with(
            "cid", "sess", ["kill-window", "-t", "@3"]
        )


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


class TestBuildShellCommandSocketPath:
    def test_socket_path_sets_dash_s(self):
        cmd, _ = _build_shell_command(
            session_name="uid", socket_path="/tmp/test.sock"
        )
        assert "-S" in cmd
        assert "/tmp/test.sock" in cmd


class TestBuildShellCommandJoinSession:
    def test_join_session_no_socket(self):
        """Joining a session group on the default server (no -S)."""
        cmd, unique = _build_shell_command(
            session_name="joiner-uid",
            user_home="/home/bob",
            join_session="owner-uid",
        )
        assert "-t" in cmd
        assert "owner-uid" in cmd
        assert "-S" not in cmd
        s_idx = cmd.index("-s")
        assert cmd[s_idx + 1].startswith("joiner-uid-")
        assert unique == cmd[s_idx + 1]
        assert "-A" not in cmd

    def test_read_only_join(self):
        cmd, unique = _build_shell_command(
            session_name="joiner-uid",
            user_home="/home/ceo",
            join_session="owner-uid",
            read_only=True,
        )
        assert "new-session" in cmd
        assert "-S" not in cmd
        assert "switch-client" not in cmd
        assert unique is not None


class TestLoadWorkspaceState:
    async def test_loads_state(self):
        import json

        state = {"admin": [{"name": "1", "shared": False}]}
        proc = AsyncMock()
        proc.communicate = AsyncMock(
            return_value=(json.dumps(state).encode(), b"")
        )
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await load_workspace_state("cid")
        assert result == state

    async def test_missing_file(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b"No such file"))
        proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await load_workspace_state("cid")
        assert result == {}

    async def test_corrupt_json(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"not json{", b""))
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            result = await load_workspace_state("cid")
        assert result == {}


class TestSaveWorkspaceState:
    async def test_saves_state(self):
        proc = AsyncMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            await save_workspace_state(
                "cid", {"admin": [{"name": "1", "shared": False}]}
            )
        proc.communicate.assert_awaited_once()


class TestRestoreWindows:
    async def test_creates_missing_windows(self):
        with patch(
            "klangk_backend.terminal.list_windows",
            return_value=[{"name": "1", "index": 0, "active": True}],
        ):
            with patch(
                "klangk_backend.terminal.new_window",
                return_value=[],
            ) as mock_new:
                await restore_windows(
                    "cid",
                    "admin",
                    [
                        {"name": "1", "shared": False},
                        {"name": "build", "shared": True},
                    ],
                )
        # Only "build" should be created (1 already exists)
        mock_new.assert_called_once_with("cid", "admin", name="build")

    async def test_no_missing_windows(self):
        with patch(
            "klangk_backend.terminal.list_windows",
            return_value=[
                {"name": "1", "index": 0, "active": True},
                {"name": "build", "index": 1, "active": False},
            ],
        ):
            with patch(
                "klangk_backend.terminal.new_window",
            ) as mock_new:
                await restore_windows(
                    "cid",
                    "admin",
                    [
                        {"name": "1", "shared": False},
                        {"name": "build", "shared": True},
                    ],
                )
        mock_new.assert_not_called()


class TestKillJoinerSessions:
    async def test_kills_non_owner_sessions(self):
        with patch(
            "klangk_backend.terminal.tmux_command",
            side_effect=[
                "admin\nbob-abc123\ncarol-def456\n",  # list-sessions
                "",  # kill bob
                "",  # kill carol
            ],
        ) as mock_cmd:
            await kill_joiner_sessions("cid", "admin")
        # Should have called list-sessions + kill for bob and carol
        assert mock_cmd.call_count == 3

    async def test_no_joiners(self):
        with patch(
            "klangk_backend.terminal.tmux_command",
            return_value="admin\n",
        ) as mock_cmd:
            await kill_joiner_sessions("cid", "admin")
        # Only list-sessions, no kills
        assert mock_cmd.call_count == 1

    async def test_kill_session_error_ignored(self):
        """If kill-session fails for a joiner, continue with others."""
        with patch(
            "klangk_backend.terminal.tmux_command",
            side_effect=[
                "admin\nbob-abc\ncarol-def\n",  # list-sessions
                RuntimeError("already exited"),  # kill bob fails
                "",  # kill carol succeeds
            ],
        ) as mock_cmd:
            await kill_joiner_sessions("cid", "admin")
        assert mock_cmd.call_count == 3

    async def test_no_sessions(self):
        with patch(
            "klangk_backend.terminal.tmux_command",
            side_effect=RuntimeError("no sessions"),
        ):
            # Should not raise
            await kill_joiner_sessions("cid", "admin")


class TestTerminalSessionJoin:
    async def test_join_session_no_socket(self):
        """Joining a session group on default server."""
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession(
                "cid",
                session_name="joiner-uid",
                user_home="/home/bob",
                join_session="owner-uid",
            )
            await s.start(80, 24)
        assert "-S" not in fake.argv
        assert "-t" in fake.argv
        assert "owner-uid" in fake.argv
        await s.stop()

    async def test_read_only_join(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession(
                "cid",
                session_name="joiner-uid",
                user_home="/home/ceo",
                join_session="owner-uid",
                read_only=True,
            )
            await s.start(80, 24)
        assert "switch-client" not in fake.argv
        assert s.read_only is True
        await s.stop()
