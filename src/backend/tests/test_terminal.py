"""Tests for terminal: PTY-based ``podman exec`` shell sessions.

The OS/PTY glue (:class:`ShellProcess`) needs a real PTY + podman and is
marked ``# pragma: no cover``; these tests drive :class:`TerminalSession`'s
lifecycle/queue logic against an injected fake shell.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from klangk_backend.exceptions import TerminalError
from klangk_backend.terminal import (
    CONTAINER_USER,
    TerminalSession,
    set_workspace_token,
    build_environment,
    build_shell_command,
    make_shell_process,
    validate_window_name,
    attach_browser,
    close_window,
    kill_joiner_sessions,
    list_windows,
    new_window,
    rename_window,
    select_window,
    terminal_tmux_enabled,
    tmux_command,
)

SHELL_FACTORY = "klangk_backend.terminal.make_shell_process"


class TestShellProcessFactory:
    """The factory + ctor are pure Python (no PTY); the OS methods that
    need a real PTY/podman are ``# pragma: no cover`` and validated
    interactively."""

    def test_factory_returns_unstarted_shell(self):
        shell = make_shell_process()
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


@contextlib.contextmanager
def _patch(fake):
    with (
        patch(SHELL_FACTORY, return_value=fake),
        patch(
            "klangk_backend.terminal.ensure_base_session",
            new_callable=AsyncMock,
        ),
    ):
        yield


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

    async def test_start_uses_plain_shell_when_tmux_disabled(
        self, monkeypatch
    ):
        monkeypatch.setenv("KLANGK_DISABLE_TMUX", "1")
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid", session_name="uid")
            await s.start(120, 40)

        argv = fake.argv
        assert argv[-2:] == ["bash", "-l"]
        assert "tmux" not in argv
        assert s.tmux_session_name is None
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

    async def test_no_command_override_by_default(self):
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        assert not any("KLANGK_CMD_OVERRIDE" in a for a in fake.argv)
        await s.stop()

    async def test_no_browser_id_env_var(self):
        """browser_id is no longer passed as an env var (uses klangk-attach-browser)."""
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession("cid")
            await s.start()
        assert not any("KLANGK_BROWSER_ID" in a for a in fake.argv)
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
        # Grouped session: -t targets the base session, -s is a unique name
        assert "-t" in tmux_tail
        assert tmux_tail[tmux_tail.index("-t") + 1] == "uid-123"
        grouped_name = tmux_tail[tmux_tail.index("-s") + 1]
        assert grouped_name.startswith("uid-123-")
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

    async def test_start_sets_tmux_environment_for_ssh_agent(self):
        """When ssh_agent_socket is set, start() calls tmux set-environment."""
        fake = FakeShell(block_after_chunks=True)
        with (
            _patch(fake),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new_callable=AsyncMock,
            ) as mock_exec,
        ):
            s = TerminalSession(
                "cid",
                session_name="uid-123",
                ssh_agent_socket="/tmp/agent.sock",
            )
            await s.start(120, 40)

        mock_exec.assert_awaited_once_with(
            "cid",
            [
                "tmux",
                "set-environment",
                "-t",
                "uid-123",
                "SSH_AUTH_SOCK",
                "/tmp/agent.sock",
            ],
        )
        await s.stop()


class TestAttachBrowser:
    async def test_runs_klangk_attach_browser(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ) as mock_exec:
            await attach_browser("cid-123", "bid-abc")
        mock_exec.assert_awaited_once_with(
            "cid-123",
            ["klangk-attach-browser", "bid-abc"],
            user=CONTAINER_USER,
            timeout=10,
        )

    async def test_logs_warning_on_failure(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(1, "", "attach failed"),
        ):
            # Should not raise
            await attach_browser("cid-123", "bid-abc")


class TestSetWorkspaceToken:
    async def test_runs_klangk_set_workspace_token(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ) as mock_exec:
            await set_workspace_token("cid-123", "jwt-token-xyz")
        mock_exec.assert_awaited_once_with(
            "cid-123",
            ["klangk-set-workspace-token", "jwt-token-xyz"],
            user=CONTAINER_USER,
            timeout=10,
        )

    async def test_logs_warning_on_failure(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(1, "", "set failed"),
        ):
            await set_workspace_token("cid-123", "jwt-token-xyz")


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
        # Manually set tmux session name (normally set by build_shell_command
        # when join_session is used with a socket path)
        s.tmux_session_name = "uid-abc123"
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(0, "", ""),
        ) as mock_exec:
            await s.stop()
        mock_exec.assert_awaited_once()
        cmd = mock_exec.call_args.args[1]
        assert "kill-session" in cmd
        assert "-t" in cmd
        assert "uid-abc123" in cmd

    async def test_stop_kill_session_failure_is_swallowed(self):
        """A failing kill-session exec must not propagate from stop()."""
        fake = FakeShell(block_after_chunks=True)
        with _patch(fake):
            s = TerminalSession(
                "cid", session_name="uid", socket_path="/tmp/s.sock"
            )
            await s.start()
        s.tmux_session_name = "uid-abc123"
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            side_effect=OSError("boom"),
        ):
            # Should not raise — failure is logged at debug level.
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


class TestTmuxCommand:
    async def test_returns_stdout(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(0, "output\n", ""),
        ) as mock_exec:
            result = await tmux_command("cid", "sess", ["list-windows"])
        assert result == "output\n"
        mock_exec.assert_awaited_once_with(
            "cid",
            ["tmux", "list-windows"],
            user=CONTAINER_USER,
            timeout=10,
        )

    async def test_raises_on_failure(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(1, "", "error msg"),
        ) as mock_exec:
            with pytest.raises(TerminalError, match="error msg"):
                await tmux_command("cid", "sess", ["bad-cmd"])
        assert mock_exec.await_count == 1  # no retry on non-socket errors

    async def test_retries_on_socket_not_found(self):
        with (
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new_callable=AsyncMock,
                side_effect=[
                    (1, "", "No such file or directory"),
                    (0, "ok\n", ""),
                ],
            ) as mock_exec,
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        ):
            result = await tmux_command("cid", "sess", ["list-windows"])
        assert result == "ok\n"
        assert mock_exec.await_count == 2
        mock_sleep.assert_awaited_once_with(0.5)
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


class TestValidateWindowName:
    def test_accepts_safe_names(self):
        for name in ["bash", "build-1", "my_window", "test.log", "A B C"]:
            validate_window_name(name)

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="1-64 characters"):
            validate_window_name("")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="1-64 characters"):
            validate_window_name("a" * 65)

    def test_rejects_shell_metacharacters(self):
        for name in ["a'b", 'a"b', "a;b", "a|b", "a`b", "a$(cmd)", "a&b"]:
            with pytest.raises(ValueError, match="only contain"):
                validate_window_name(name)


class TestNewWindow:
    async def test_creates_window_auto_name(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(0, "@0|||0|||1|||1\n", ""),
        ) as mock_exec:
            result = await new_window("cid", "sess")
        assert len(result) == 1
        assert result[0]["name"] == "1"
        assert mock_exec.call_args.args[1][:2] == ["bash", "-c"]

    async def test_auto_name_skips_existing(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(0, "@0|||0|||1|||0\n@1|||1|||2|||1\n", ""),
        ):
            result = await new_window("cid", "sess")
        assert len(result) == 2

    async def test_creates_named_window(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(
                0,
                "@0|||0|||bash|||0\n@1|||1|||build|||1\n",
                "",
            ),
        ) as mock_exec:
            result = await new_window("cid", "sess", name="build")
        assert len(result) == 2
        assert result[1]["name"] == "build"
        # the name is passed as a positional argv ($1), never interpolated
        # into the bash script string (defense-in-depth against injection)
        argv = mock_exec.call_args.args[1]
        assert argv[:2] == ["bash", "-c"]
        assert "build" not in argv[2]  # not in the script
        assert argv[3] == "bash"  # $0
        assert argv[4] == "build"  # $1 = name

    async def test_rejects_shell_injection(self):
        with pytest.raises(ValueError, match="only contain"):
            await new_window("cid", "sess", name="';rm -rf /;'")

    async def test_rejects_duplicate_name(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(1, "DUPLICATE\n", ""),
        ):
            with pytest.raises(ValueError, match="already exists"):
                await new_window("cid", "sess", name="build")

    async def test_raises_on_tmux_error(self):
        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(1, "", "session not found"),
        ):
            with pytest.raises(TerminalError, match="session not found"):
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

    async def test_rejects_shell_injection(self):
        with pytest.raises(ValueError, match="only contain"):
            await rename_window("cid", "sess", 0, "';rm -rf /;'")

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


class TestBuildEnvironment:
    def test_ssh_agent_socket_included(self):
        env = build_environment(None, ssh_agent_socket="/tmp/agent.sock")
        assert "SSH_AUTH_SOCK=/tmp/agent.sock" in env

    def test_ssh_agent_socket_omitted_when_none(self):
        env = build_environment(None)
        assert not any(e.startswith("SSH_AUTH_SOCK=") for e in env)


class TestBuildShellCommandSshAgent:
    def test_ssh_agent_socket_adds_tmux_env(self):
        cmd, _ = build_shell_command(
            session_name="uid",
            ssh_agent_socket="/tmp/agent.sock",
        )
        tmux_idx = cmd.index("tmux")
        tmux_tail = cmd[tmux_idx:]
        assert "-e" in tmux_tail
        assert "SSH_AUTH_SOCK=/tmp/agent.sock" in tmux_tail


class TestBuildShellCommandSocketPath:
    def test_socket_path_sets_dash_s(self):
        cmd, _ = build_shell_command(
            session_name="uid", socket_path="/tmp/test.sock"
        )
        assert "-S" in cmd
        assert "/tmp/test.sock" in cmd


class TestBuildShellCommandJoinSession:
    def test_join_session_no_socket(self):
        """Joining a session group on the default server (no -S)."""
        cmd, unique = build_shell_command(
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
        cmd, unique = build_shell_command(
            session_name="joiner-uid",
            user_home="/home/ceo",
            join_session="owner-uid",
            read_only=True,
        )
        assert "new-session" in cmd
        assert "-S" not in cmd
        assert "switch-client" not in cmd
        assert unique is not None


class TestTerminalTmuxEnabled:
    def test_default_enabled(self, monkeypatch):
        monkeypatch.delenv("KLANGK_DISABLE_TMUX", raising=False)
        assert terminal_tmux_enabled() is True

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "Yes"])
    def test_truthy_disables(self, monkeypatch, val):
        monkeypatch.setenv("KLANGK_DISABLE_TMUX", val)
        assert terminal_tmux_enabled() is False

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off"])
    def test_other_values_keep_enabled(self, monkeypatch, val):
        monkeypatch.setenv("KLANGK_DISABLE_TMUX", val)
        assert terminal_tmux_enabled() is True


class TestBuildShellCommandTmuxDisabled:
    def test_plain_login_shell_when_disabled(self):
        cmd, unique = build_shell_command(
            session_name="uid",
            user_home="/home/bob",
            tmux_enabled=False,
        )
        assert cmd[-2:] == ["bash", "-l"]
        assert "tmux" not in cmd
        assert unique is None

    def test_plain_shell_still_unsets_sensitive_env(self, monkeypatch):
        monkeypatch.setenv("KLANGK_LLM_API_KEY", "secret")
        cmd, _ = build_shell_command(session_name="uid", tmux_enabled=False)
        unset = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-u"]
        assert "KLANGK_LLM_API_KEY" in unset
        assert "tmux" not in cmd

    def test_shared_socket_still_uses_tmux_when_disabled(self):
        """Sharing is built on tmux; the toggle must not break it."""
        cmd, _ = build_shell_command(
            session_name="uid",
            socket_path="/tmp/test.sock",
            tmux_enabled=False,
        )
        assert "tmux" in cmd
        assert "-S" in cmd

    def test_join_session_still_uses_tmux_when_disabled(self):
        cmd, unique = build_shell_command(
            session_name="joiner-uid",
            join_session="owner-uid",
            tmux_enabled=False,
        )
        assert "tmux" in cmd
        assert unique is not None


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
                TerminalError("already exited"),  # kill bob fails
                "",  # kill carol succeeds
            ],
        ) as mock_cmd:
            await kill_joiner_sessions("cid", "admin")
        assert mock_cmd.call_count == 3

    async def test_no_sessions(self):
        with patch(
            "klangk_backend.terminal.tmux_command",
            side_effect=TerminalError("no sessions"),
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


class TestEnsureBaseSession:
    async def test_session_already_exists(self):
        """Skip creation if tmux has-session succeeds."""
        from klangk_backend.terminal import ensure_base_session

        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(0, "", ""),  # has-session succeeds
        ) as mock_exec:
            created = await ensure_base_session("cid", "my-session")
        assert created is False
        mock_exec.assert_awaited_once()
        assert "has-session" in mock_exec.call_args.args[1]

    async def test_session_does_not_exist(self):
        """Create detached session when has-session fails."""
        from klangk_backend.terminal import ensure_base_session

        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            side_effect=[(1, "", ""), (0, "", "")],  # has fail, new ok
        ) as mock_exec:
            created = await ensure_base_session(
                "cid", "my-session", user_home="/home/u"
            )
        assert created is True
        assert mock_exec.await_count == 2
        new_cmd = mock_exec.call_args_list[1].args[1]
        assert "new-session" in new_cmd
        assert "-d" in new_cmd
        assert "-s" in new_cmd

    async def test_has_session_exception(self):
        """Falls through to create if has-session raises."""
        from klangk_backend.terminal import ensure_base_session

        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            side_effect=[OSError("boom"), (0, "", "")],
        ) as mock_exec:
            created = await ensure_base_session("cid", "my-session")
        assert created is True
        assert mock_exec.await_count == 2

    async def test_create_failure_logs_warning(self):
        """Warning logged when new-session fails."""
        from klangk_backend.terminal import ensure_base_session

        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            side_effect=[(1, "", ""), OSError("create failed")],
        ):
            created = await ensure_base_session("cid", "my-session")
        assert created is False

    async def test_env_args_passed(self):
        """HOME and SSH_AUTH_SOCK are passed as tmux -e flags."""
        from klangk_backend.terminal import ensure_base_session

        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            side_effect=[(1, "", ""), (0, "", "")],
        ) as mock_exec:
            await ensure_base_session(
                "cid",
                "my-session",
                user_home="/home/u",
                ssh_agent_socket="/tmp/agent.sock",
            )
        new_cmd = mock_exec.call_args_list[1].args[1]
        assert "HOME=/home/u" in new_cmd
        assert "SSH_AUTH_SOCK=/tmp/agent.sock" in new_cmd

    async def test_service_cmd_window_exists_exception_returns_false(self):
        """service_cmd_window_exists returns False if list-windows raises."""
        from klangk_backend.terminal import service_cmd_window_exists

        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            side_effect=OSError("boom"),
        ):
            result = await service_cmd_window_exists("cid", "my-session")
        assert result is False

    async def test_service_cmd_window_exists_rc_nonzero_returns_false(self):
        """service_cmd_window_exists returns False if list-windows fails."""
        from klangk_backend.terminal import service_cmd_window_exists

        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(1, "", ""),  # list-windows fails
        ):
            result = await service_cmd_window_exists("cid", "my-session")
        assert result is False

    async def test_has_tmux_session_exception_returns_false(self):
        """has_tmux_session returns False if has-session raises."""
        from klangk_backend.terminal import has_tmux_session

        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            side_effect=OSError("boom"),
        ):
            result = await has_tmux_session("cid", "my-session")
        assert result is False


class TestEnsureServiceSession:
    """Tests for ensure_service_session -- the standalone ``service`` tmux
    session that runs the workspace's service command, owned by the agent
    identity (#1133 D6). _ensure_tmux_session and service_cmd_window_exists
    are mocked to isolate the firing logic from tmux-session internals."""

    async def test_creates_window_and_sends_command(self):
        """A fresh service session fires the service command in its
        ``service-cmd`` window -> ``service:service-cmd``."""
        from klangk_backend.terminal import ensure_service_session

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new=AsyncMock(side_effect=[(0, "", ""), (0, "", "")]),
            ) as mock_exec,
        ):
            await ensure_service_session(
                "cid", "/home/clanker", "openclaw gateway"
            )
        cmds = [c.args[1] for c in mock_exec.call_args_list]
        # service-cmd window created in the service session.
        assert any(
            "new-window" in c and "service" in c and "service-cmd" in c
            for c in cmds
        )
        # Command sent to service:service-cmd.
        assert any(
            "send-keys" in c
            and "service:service-cmd" in c
            and "openclaw gateway" in c
            for c in cmds
        )

    async def test_ensures_service_session_with_agent_home(self):
        """The service session is ensured with the constant name and the
        agent home as its HOME."""
        from klangk_backend.terminal import (
            SERVICE_SESSION,
            ensure_service_session,
        )

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ) as mock_ensure,
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=True),
            ),
        ):
            await ensure_service_session(
                "cid", "/home/clanker", "openclaw gateway"
            )
        mock_ensure.assert_awaited_once_with(
            "cid", SERVICE_SESSION, "/home/clanker"
        )

    async def test_skips_when_window_already_exists(self):
        """Exactly-once: no new-window/send-keys if service-cmd exists."""
        from klangk_backend.terminal import ensure_service_session

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new=AsyncMock(),
            ) as mock_exec,
        ):
            await ensure_service_session(
                "cid", "/home/clanker", "openclaw gateway"
            )
        cmds = [c.args[1] for c in mock_exec.call_args_list]
        assert not any("new-window" in c for c in cmds)
        assert not any("send-keys" in c for c in cmds)

    async def test_blocked_when_setup_pending(self):
        """The service command does not fire while setup is pending (#1033)."""
        from klangk_backend.terminal import ensure_service_session

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new=AsyncMock(),
            ) as mock_exec,
        ):
            await ensure_service_session(
                "cid",
                "/home/clanker",
                "openclaw gateway",
                setup_state="pending",
            )
        cmds = [c.args[1] for c in mock_exec.call_args_list]
        assert not any("new-window" in c for c in cmds)
        assert not any("send-keys" in c for c in cmds)

    async def test_new_window_failure_logs_warning(self):
        """A tmux new-window failure is logged but doesn't raise."""
        from klangk_backend.terminal import ensure_service_session

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new=AsyncMock(side_effect=RuntimeError("tmux broke")),
            ),
            patch("klangk_backend.terminal.logger") as mock_logger,
        ):
            await ensure_service_session("cid", "/home/clanker", "cmd")
        mock_logger.warning.assert_called()

    async def test_send_keys_failure_logs_warning(self):
        """A send-keys failure is logged but doesn't raise."""
        from klangk_backend.terminal import ensure_service_session

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new=AsyncMock(
                    side_effect=[
                        (0, "", ""),  # new-window succeeds
                        RuntimeError("send broke"),  # send-keys fails
                        (0, "", ""),  # kill-window cleanup
                    ]
                ),
            ),
            patch("klangk_backend.terminal.logger") as mock_logger,
        ):
            await ensure_service_session("cid", "/home/clanker", "cmd")
        mock_logger.warning.assert_called()

    async def test_firing_resets_health_grace_anchor(self):
        """Firing the service command resets the health-check startup-grace
        anchor so the monitor gives the freshly-launched service time to
        boot before a failing poll can flag it unhealthy."""
        import types

        from klangk_backend.terminal import ensure_service_session

        mock_registry = MagicMock()
        app_state = types.SimpleNamespace(container_registry=mock_registry)

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new=AsyncMock(side_effect=[(0, "", ""), (0, "", "")]),
            ),
        ):
            await ensure_service_session(
                "cid",
                "/home/clanker",
                "openclaw gateway",
                app_state=app_state,
            )
        mock_registry.mark_service_started.assert_called_once_with("cid")

    async def test_existing_window_does_not_reset_grace_anchor(self):
        """The no-op path (service-cmd window already exists) never
        re-launched the service, so it must not restart the grace window."""
        import types

        from klangk_backend.terminal import ensure_service_session

        mock_registry = MagicMock()
        app_state = types.SimpleNamespace(container_registry=mock_registry)

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new=AsyncMock(),
            ),
        ):
            await ensure_service_session(
                "cid",
                "/home/clanker",
                "openclaw gateway",
                app_state=app_state,
            )
        mock_registry.mark_service_started.assert_not_called()

    async def test_send_keys_failure_does_not_reset_grace_anchor(self):
        """If send-keys itself failed the command never launched, so the
        grace anchor must not advance (no service is booting)."""
        import types

        from klangk_backend.terminal import ensure_service_session

        mock_registry = MagicMock()
        app_state = types.SimpleNamespace(container_registry=mock_registry)

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new=AsyncMock(
                    side_effect=[
                        (0, "", ""),  # new-window succeeds
                        RuntimeError("send broke"),  # send-keys fails
                        (0, "", ""),  # kill-window cleanup
                    ]
                ),
            ),
        ):
            await ensure_service_session(
                "cid",
                "/home/clanker",
                "cmd",
                app_state=app_state,
            )
        mock_registry.mark_service_started.assert_not_called()

    async def test_concurrent_fires_create_window_exactly_once(self):
        """#1188: two concurrent ensure_service_session calls for the SAME
        container must not both create the service-cmd window.

        The boot path (workspaces.start_workspace) and the per-connection
        path (wshandler _fire_service_command) are both unserialized callers.
        Without the per-container lock, both can pass the window-exists check
        before either's new-window lands, and since tmux allows duplicate
        window names, both create a service-cmd window -- leaving later
        send-keys ambiguous. The lock makes window-exists -> new-window ->
        send-keys atomic per container.
        """
        from klangk_backend import terminal

        # Shared tmux "state": both the existence check and new-window
        # read/write this, so once the lock forces the second caller to
        # wait, it genuinely observes the first caller's mutation.
        windows: set[str] = set()
        new_window_calls = 0
        # Capture the real sleep BEFORE patching terminal.asyncio.sleep (which
        # neutralizes the source's 1s settle delay). fake_exec uses this real
        # sleep(0) as an explicit yield point so the race window is
        # deterministic: without the lock the second caller interleaves its
        # existence check during this yield; with the lock it cannot.
        _real_sleep = asyncio.sleep

        async def fake_window_exists(cid, session):
            return terminal.SERVICE_CMD_WINDOW in windows

        async def fake_exec(cid, argv, **kwargs):
            nonlocal new_window_calls
            if "new-window" in argv:
                new_window_calls += 1
                # Yield to the loop at the write step (before mutating
                # `windows`). Without the lock this lets the second caller
                # observe the empty pre-creation state and also create ->
                # reproducing the #1188 duplicate-window race.
                await _real_sleep(0)
                windows.add(terminal.SERVICE_CMD_WINDOW)
            return (0, "", "")

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                side_effect=fake_window_exists,
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                side_effect=fake_exec,
            ),
            patch("klangk_backend.terminal.asyncio.sleep", new=AsyncMock()),
        ):
            await asyncio.gather(
                terminal.ensure_service_session(
                    "cid", "/home/clanker", "openclaw gateway"
                ),
                terminal.ensure_service_session(
                    "cid", "/home/clanker", "openclaw gateway"
                ),
            )

        # Exactly-once: only one new-window landed despite concurrent calls.
        assert new_window_calls == 1
        assert windows == {terminal.SERVICE_CMD_WINDOW}

    async def test_concurrent_fires_isolated_per_container(self):
        """#1188: the firing lock is keyed by container, so concurrent fires
        for DIFFERENT containers are not serialized -- each fires exactly
        once, in parallel. Unrelated workspaces must not block each other."""
        from klangk_backend import terminal

        # Per-container tmux state so the two containers don't share a view.
        windows: dict[str, set[str]] = {"cid-a": set(), "cid-b": set()}
        new_window_calls = 0
        _real_sleep = asyncio.sleep

        async def fake_window_exists(cid, session):
            return terminal.SERVICE_CMD_WINDOW in windows.get(cid, set())

        async def fake_exec(cid, argv, **kwargs):
            nonlocal new_window_calls
            if "new-window" in argv:
                new_window_calls += 1
                await _real_sleep(0)
                windows.setdefault(cid, set()).add(terminal.SERVICE_CMD_WINDOW)
            return (0, "", "")

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                side_effect=fake_window_exists,
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                side_effect=fake_exec,
            ),
            patch("klangk_backend.terminal.asyncio.sleep", new=AsyncMock()),
        ):
            await asyncio.gather(
                terminal.ensure_service_session(
                    "cid-a", "/home/clanker", "cmd-a"
                ),
                terminal.ensure_service_session(
                    "cid-b", "/home/clanker", "cmd-b"
                ),
            )

        # Each container fired exactly once -- the per-container lock did not
        # serialize unrelated containers.
        assert new_window_calls == 2
        assert windows["cid-a"] == {terminal.SERVICE_CMD_WINDOW}
        assert windows["cid-b"] == {terminal.SERVICE_CMD_WINDOW}

    async def test_send_keys_failure_kills_half_created_window(self):
        """A send-keys failure kills the zombie window so the next fire
        re-runs the whole sequence instead of no-oping forever (#1186)."""
        from klangk_backend.terminal import ensure_service_session

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new=AsyncMock(
                    side_effect=[
                        (0, "", ""),  # new-window succeeds
                        RuntimeError("send broke"),  # send-keys fails
                        (0, "", ""),  # kill-window cleanup
                    ]
                ),
            ) as mock_exec,
            patch("klangk_backend.terminal.logger"),
        ):
            await ensure_service_session("cid", "/home/clanker", "cmd")

        # The third exec call must be the kill-window cleanup targeting
        # the service-cmd window we just created.
        cleanup_call = mock_exec.call_args_list[2]
        argv = cleanup_call.args[1]
        assert "tmux" in argv
        assert "kill-window" in argv
        assert "service:service-cmd" in argv

    async def test_send_keys_failure_cleanup_failure_is_logged(self):
        """If the kill-window cleanup itself raises, it's logged but the
        function still returns without raising (#1186)."""
        from klangk_backend.terminal import ensure_service_session

        with (
            patch(
                "klangk_backend.terminal._ensure_tmux_session",
                new=AsyncMock(),
            ),
            patch(
                "klangk_backend.terminal.service_cmd_window_exists",
                new=AsyncMock(return_value=False),
            ),
            patch(
                "klangk_backend.terminal.podman.exec_container",
                new=AsyncMock(
                    side_effect=[
                        (0, "", ""),  # new-window succeeds
                        RuntimeError("send broke"),  # send-keys fails
                        RuntimeError("cleanup broke"),  # kill-window fails
                    ]
                ),
            ),
            patch("klangk_backend.terminal.logger") as mock_logger,
        ):
            await ensure_service_session("cid", "/home/clanker", "cmd")
        # Both the send-keys failure and the cleanup failure are warned.
        assert mock_logger.warning.call_count == 2


class TestServiceSessionLock:
    """Unit coverage for the per-container firing-lock helpers added in #1188."""

    def setup_method(self):
        from klangk_backend import terminal

        terminal._service_session_locks.clear()

    def teardown_method(self):
        from klangk_backend import terminal

        terminal._service_session_locks.clear()

    def test_get_lock_returns_same_lock_for_same_container(self):
        from klangk_backend.terminal import get_service_session_lock

        lock_a = get_service_session_lock("cid")
        lock_b = get_service_session_lock("cid")
        assert lock_a is lock_b

    def test_get_lock_returns_distinct_locks_per_container(self):
        from klangk_backend.terminal import get_service_session_lock

        lock_a = get_service_session_lock("cid-a")
        lock_b = get_service_session_lock("cid-b")
        assert lock_a is not lock_b

    def test_clear_lock_removes_entry(self):
        from klangk_backend.terminal import (
            get_service_session_lock,
            _service_session_locks,
            clear_service_session_lock,
        )

        get_service_session_lock("cid")
        assert "cid" in _service_session_locks
        clear_service_session_lock("cid")
        assert "cid" not in _service_session_locks

    def test_clear_lock_is_noop_for_unknown_container(self):
        from klangk_backend.terminal import clear_service_session_lock

        # Must not raise for a container that never registered a lock.
        clear_service_session_lock("never-seen")

    def test_prune_removes_entries_for_untracked_containers(self):
        from klangk_backend.terminal import (
            _service_session_locks,
            get_service_session_lock,
            prune_service_session_locks,
        )

        get_service_session_lock("alive")
        get_service_session_lock("dead-a")
        get_service_session_lock("dead-b")
        assert len(_service_session_locks) == 3

        removed = prune_service_session_locks({"alive"})
        assert removed == 2
        assert set(_service_session_locks) == {"alive"}

    async def test_prune_keeps_held_lock_even_if_untracked(self):
        from klangk_backend.terminal import (
            _service_session_locks,
            get_service_session_lock,
            prune_service_session_locks,
        )

        held = get_service_session_lock("held-but-orphaned")
        await held.acquire()  # simulate an in-flight service-command fire
        try:
            removed = prune_service_session_locks(set())
            # Not pruned: recreating its lock would not serialize against the
            # in-flight fire (#1188 duplicate-window race).
            assert removed == 0
            assert "held-but-orphaned" in _service_session_locks
        finally:
            held.release()

    def test_prune_noop_when_all_tracked(self):
        from klangk_backend.terminal import (
            get_service_session_lock,
            prune_service_session_locks,
        )

        get_service_session_lock("a")
        get_service_session_lock("b")
        assert prune_service_session_locks({"a", "b"}) == 0


class TestServiceSessionHelpers:
    """Direct coverage for the firing-predicate helpers used by
    ensure_service_session."""

    async def test_service_cmd_window_exists_true_when_present(self):
        from klangk_backend.terminal import service_cmd_window_exists

        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(0, "bash\nservice-cmd\n", ""),
        ):
            assert await service_cmd_window_exists("cid", "service") is True

    async def test_service_cmd_window_exists_false_when_absent(self):
        from klangk_backend.terminal import service_cmd_window_exists

        with patch(
            "klangk_backend.terminal.podman.exec_container",
            new_callable=AsyncMock,
            return_value=(0, "bash\n", ""),
        ):
            assert await service_cmd_window_exists("cid", "service") is False

    def test_should_fire_returns_false_without_service_command(self):
        from klangk_backend.terminal import should_fire_service_command

        assert should_fire_service_command(None, "complete") is False
        assert should_fire_service_command("", "complete") is False
