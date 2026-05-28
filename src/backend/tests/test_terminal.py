"""Tests for terminal: Docker API exec-based terminal sessions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


from bark_backend.terminal import TerminalSession


def _mock_stream():
    stream = MagicMock()
    stream.write_in = AsyncMock()
    stream.close = AsyncMock()
    return stream


def _mock_exec(stream):
    exec_obj = MagicMock()
    exec_obj.start = MagicMock(return_value=stream)
    exec_obj.resize = AsyncMock()
    return exec_obj


def _mock_container(exec_obj):
    container = MagicMock()
    container.exec = AsyncMock(return_value=exec_obj)
    return container


def _mock_docker(container):
    docker = MagicMock()
    docker.containers = MagicMock()
    docker.containers.get = AsyncMock(return_value=container)
    docker.close = AsyncMock()
    return docker


class TestInit:
    def test_initial_state(self):
        s = TerminalSession("cid")
        assert s.container_id == "cid"
        assert s._stream is None
        assert s._exec is None
        assert s._running is False
        assert s.is_alive is False


class TestStart:
    async def test_start_creates_exec_session(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start(120, 40)

        docker.containers.get.assert_awaited_once_with("cid")
        container.exec.assert_awaited_once()
        call_kwargs = container.exec.call_args
        assert call_kwargs[1]["tty"] is True
        assert call_kwargs[1]["stdin"] is True
        assert call_kwargs[1]["user"] == "bark"
        assert call_kwargs[1]["workdir"] == "/work"
        exec_obj.start.assert_called_once()
        exec_obj.resize.assert_awaited_once_with(h=40, w=120)

        assert s._running is True
        await s.stop()

    async def test_start_unsets_sensitive_env_vars(self, monkeypatch):
        monkeypatch.setenv("BARK_LLM_API_KEY", "secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "secret2")

        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        call_args = container.exec.call_args
        cmd = call_args[0][0]
        assert "env" in cmd
        env_idx = cmd.index("env")
        env_args = cmd[env_idx:]
        unset_keys = [
            env_args[i + 1] for i, a in enumerate(env_args) if a == "-u"
        ]
        assert "BARK_LLM_API_KEY" in unset_keys
        assert "ANTHROPIC_API_KEY" in unset_keys

        await s.stop()

    async def test_command_override_sets_env_var(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start(command_override="bash")

        call_kwargs = container.exec.call_args[1]
        assert "BARK_CMD_OVERRIDE=bash" in call_kwargs["environment"]

        await s.stop()

    async def test_no_command_override_by_default(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        call_kwargs = container.exec.call_args[1]
        assert not any(
            "BARK_CMD_OVERRIDE" in e for e in call_kwargs["environment"]
        )

        await s.stop()


class TestReadLoop:
    async def test_output_from_stream(self):
        msg = MagicMock()
        msg.data = b"hello world"
        stream = _mock_stream()
        stream.read_out = AsyncMock(side_effect=[msg, None])
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.1)
        data = s._output_queue.get_nowait()
        assert data == "hello world"

        await s.stop()

    async def test_stream_end_signals_none(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.1)
        data = s._output_queue.get_nowait()
        assert data is None

        await s.stop()


class TestWrite:
    async def test_write_sends_to_stream(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        await s.write("hello")
        stream.write_in.assert_awaited_with(b"hello")

        await s.stop()

    async def test_write_when_stopped(self):
        s = TerminalSession("cid")
        await s.write("hello")


class TestResize:
    async def test_resize_calls_exec_resize(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        await s.resize(200, 50)
        exec_obj.resize.assert_awaited_with(h=50, w=200)

        await s.stop()

    async def test_resize_when_stopped(self):
        s = TerminalSession("cid")
        await s.resize(80, 24)


class TestStop:
    async def test_stop_cleans_up(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        await s.stop()
        assert s._running is False
        assert s._stream is None
        assert s._exec is None
        assert s.is_alive is False
        docker.close.assert_awaited()

    async def test_stop_when_not_started(self):
        s = TerminalSession("cid")
        await s.stop()


class TestIsAlive:
    async def test_not_alive_after_stream_ends(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.1)
        assert s.is_alive is False

        await s.stop()

    async def test_not_alive_after_stop(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        await s.stop()
        assert s.is_alive is False
