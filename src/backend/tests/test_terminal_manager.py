"""Tests for terminal_manager: PTY session lifecycle, I/O, resize."""

import asyncio
import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bark_backend import terminal_manager
from bark_backend.terminal_manager import TerminalSession


def _mock_proc(returncode=None):
    proc = MagicMock()
    proc.returncode = returncode
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


@pytest.fixture
def real_pipe():
    """Create a real pipe so add_reader/remove_reader work with epoll."""
    r, w = os.pipe()
    yield r, w
    for fd in (r, w):
        try:
            os.close(fd)
        except OSError:
            pass


# Patch all OS-level functions for every test in this module
@pytest.fixture(autouse=True)
def mock_os(real_pipe):
    r, w = real_pipe
    with (
        patch.object(
            terminal_manager, "openpty", return_value=(r, w)
        ) as mopenpty,
        patch.object(terminal_manager, "set_winsize") as m_winsize,
        patch.object(terminal_manager, "fd_read", return_value=b"") as m_read,
        patch.object(terminal_manager, "fd_write", return_value=0) as m_write,
        patch.object(terminal_manager, "fd_close") as m_close,
    ):
        yield {
            "openpty": mopenpty,
            "set_winsize": m_winsize,
            "fd_read": m_read,
            "fd_write": m_write,
            "fd_close": m_close,
            "master_fd": r,
            "slave_fd": w,
        }


class TestInit:
    def test_initial_state(self):
        s = TerminalSession("cid")
        assert s.container_id == "cid"
        assert s._master_fd is None
        assert s._proc is None
        assert s._running is False
        assert not s.is_alive


class TestStart:
    async def test_start_creates_pty_and_process(self, mock_os):
        proc = _mock_proc()
        master_fd = mock_os["master_fd"]
        slave_fd = mock_os["slave_fd"]
        loop = asyncio.get_event_loop()

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ) as m_exec:
            s = TerminalSession("cid")
            await s.start(cols=120, rows=40)

        assert s._master_fd == master_fd
        assert s._running is True
        assert s._proc is proc

        mock_os["openpty"].assert_called_once()
        mock_os["set_winsize"].assert_called_once_with(master_fd, 40, 120)
        mock_os["fd_close"].assert_called_once_with(slave_fd)
        exec_args = m_exec.call_args[0]
        assert exec_args[0] == "docker"
        assert exec_args[1] == "exec"
        assert "cid" in exec_args
        assert "/bin/bash" in exec_args

        loop.remove_reader(master_fd)

    async def test_start_unsets_sensitive_env_vars(self, mock_os, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret2")
        proc = _mock_proc()
        master_fd = mock_os["master_fd"]
        loop = asyncio.get_event_loop()

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ) as m_exec:
            s = TerminalSession("cid")
            await s.start()

        exec_args = m_exec.call_args[0]
        # The command should use `env -u KEY` inside the container
        assert "env" in exec_args
        env_idx = exec_args.index("env")
        env_args = exec_args[env_idx:]
        assert "-u" in env_args
        unset_keys = [
            env_args[i + 1] for i, a in enumerate(env_args) if a == "-u"
        ]
        assert "LLM_API_KEY" in unset_keys
        assert "ANTHROPIC_API_KEY" in unset_keys
        assert "BARK_RESUME_SESSION" in unset_keys

        loop.remove_reader(master_fd)


class TestOnReadable:
    def test_data_queued(self, mock_os):
        mock_os["fd_read"].return_value = b"hello"
        s = TerminalSession("cid")
        s._master_fd = mock_os["master_fd"]

        s.on_readable()

        assert s._output_queue.qsize() == 1
        assert s._output_queue.get_nowait() == "hello"

    def test_empty_read_sends_none(self, mock_os):
        mock_os["fd_read"].return_value = b""
        s = TerminalSession("cid")
        s._master_fd = mock_os["master_fd"]

        s.on_readable()

        assert s._output_queue.get_nowait() is None

    def test_oserror_sends_none(self, mock_os):
        mock_os["fd_read"].side_effect = OSError("fd closed")
        s = TerminalSession("cid")
        s._master_fd = mock_os["master_fd"]

        s.on_readable()

        assert s._output_queue.get_nowait() is None

    def test_binary_decoded_with_replacement(self, mock_os):
        mock_os["fd_read"].return_value = b"\x80\x81\x82"
        s = TerminalSession("cid")
        s._master_fd = mock_os["master_fd"]

        s.on_readable()

        result = s._output_queue.get_nowait()
        assert "\ufffd" in result


class TestRemoveReader:
    def test_valueerror_suppressed(self):
        s = TerminalSession("cid")
        s._master_fd = 99
        loop = asyncio.get_event_loop()
        with patch.object(
            loop, "remove_reader", side_effect=ValueError("bad fd")
        ):
            s.remove_reader()

    def test_oserror_suppressed(self):
        s = TerminalSession("cid")
        s._master_fd = 99
        loop = asyncio.get_event_loop()
        with patch.object(
            loop, "remove_reader", side_effect=OSError("bad fd")
        ):
            s.remove_reader()


class TestIsAlive:
    def test_alive(self):
        s = TerminalSession("cid")
        s._proc = _mock_proc(returncode=None)
        assert s.is_alive is True

    def test_dead_no_proc(self):
        s = TerminalSession("cid")
        assert s.is_alive is False

    def test_dead_with_returncode(self):
        s = TerminalSession("cid")
        s._proc = _mock_proc(returncode=1)
        assert s.is_alive is False


class TestWrite:
    async def test_write(self, mock_os):
        s = TerminalSession("cid")
        s._master_fd = mock_os["master_fd"]

        await s.write("ls\n")

        mock_os["fd_write"].assert_called_once_with(
            mock_os["master_fd"], b"ls\n"
        )

    async def test_write_no_fd(self, mock_os):
        s = TerminalSession("cid")
        await s.write("ls\n")
        mock_os["fd_write"].assert_not_called()


class TestResize:
    async def test_resize(self, mock_os):
        s = TerminalSession("cid")
        s._master_fd = mock_os["master_fd"]

        await s.resize(cols=200, rows=50)

        mock_os["set_winsize"].assert_called_once_with(
            mock_os["master_fd"], 50, 200
        )

    async def test_resize_no_fd(self, mock_os):
        s = TerminalSession("cid")
        await s.resize(cols=200, rows=50)
        mock_os["set_winsize"].assert_not_called()


class TestOutput:
    async def test_yields_data(self):
        s = TerminalSession("cid")
        s._running = True
        s._output_queue.put_nowait("line1")
        s._output_queue.put_nowait("line2")
        s._output_queue.put_nowait(None)

        results = []
        async for data in s.output():
            results.append(data)

        assert results == ["line1", "line2"]

    async def test_empty_stream(self):
        s = TerminalSession("cid")
        s._running = True
        s._output_queue.put_nowait(None)

        results = []
        async for data in s.output():
            results.append(data)

        assert results == []


class TestStop:
    async def test_stop_cleans_up(self, mock_os):
        fd = mock_os["master_fd"]
        s = TerminalSession("cid")
        s._master_fd = fd
        s._proc = _mock_proc(returncode=None)
        s._running = True

        loop = asyncio.get_event_loop()
        loop.add_reader(fd, lambda: None)

        await s.stop()

        assert s._running is False
        assert s._proc is None
        assert s._master_fd is None
        mock_os["fd_close"].assert_called_once_with(fd)

    async def test_stop_no_proc_no_fd(self, mock_os):
        s = TerminalSession("cid")
        s._running = True

        await s.stop()

        assert s._running is False
        mock_os["fd_close"].assert_not_called()

    async def test_stop_terminate_timeout_then_kill(self, mock_os):
        fd = mock_os["master_fd"]
        s = TerminalSession("cid")
        s._master_fd = fd
        proc = _mock_proc(returncode=None)
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        s._proc = proc
        s._running = True

        loop = asyncio.get_event_loop()
        loop.add_reader(fd, lambda: None)

        await s.stop()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert s._proc is None

    async def test_stop_terminate_process_gone(self, mock_os):
        fd = mock_os["master_fd"]
        s = TerminalSession("cid")
        s._master_fd = fd
        proc = _mock_proc(returncode=None)
        proc.terminate = MagicMock(side_effect=ProcessLookupError)
        s._proc = proc
        s._running = True

        loop = asyncio.get_event_loop()
        loop.add_reader(fd, lambda: None)

        await s.stop()

        assert s._proc is None

    async def test_stop_kill_process_gone(self, mock_os):
        fd = mock_os["master_fd"]
        s = TerminalSession("cid")
        s._master_fd = fd
        proc = _mock_proc(returncode=None)
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock(side_effect=ProcessLookupError)
        s._proc = proc
        s._running = True

        loop = asyncio.get_event_loop()
        loop.add_reader(fd, lambda: None)

        await s.stop()

        assert s._proc is None

    async def test_stopfd_close_error(self, mock_os):
        mock_os["fd_close"].side_effect = OSError("already closed")
        s = TerminalSession("cid")
        s._master_fd = mock_os["master_fd"]
        s._running = True

        await s.stop()

        assert s._master_fd is None

    async def test_stopremove_reader_raises_valueerror(self, mock_os):
        """remove_reader raising ValueError is handled gracefully."""
        fd = mock_os["master_fd"]
        s = TerminalSession("cid")
        s._master_fd = fd
        s._running = True

        loop = asyncio.get_event_loop()
        with patch.object(
            loop, "remove_reader", side_effect=ValueError("bad fd")
        ):
            await s.stop()

        assert s._master_fd is None

    async def test_stopremove_reader_raises_oserror(self, mock_os):
        """remove_reader raising OSError is handled gracefully."""
        fd = mock_os["master_fd"]
        s = TerminalSession("cid")
        s._master_fd = fd
        s._running = True

        loop = asyncio.get_event_loop()
        with patch.object(
            loop, "remove_reader", side_effect=OSError("bad fd")
        ):
            await s.stop()

        assert s._master_fd is None
