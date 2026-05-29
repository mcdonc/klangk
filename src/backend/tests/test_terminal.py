"""Tests for terminal: Docker API exec-based terminal sessions."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


from klangk_backend.terminal import TerminalSession


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
        assert call_kwargs[1]["user"] == "klangk"
        assert call_kwargs[1]["workdir"] == "/home/klangk/work"
        exec_obj.start.assert_called_once()
        exec_obj.resize.assert_awaited_once_with(h=40, w=120)

        assert s._running is True
        await s.stop()

    async def test_start_unsets_sensitive_env_vars(self, monkeypatch):
        monkeypatch.setenv("KLANGK_LLM_API_KEY", "secret")
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
        assert "KLANGK_LLM_API_KEY" in unset_keys
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
        assert "KLANGK_CMD_OVERRIDE=bash" in call_kwargs["environment"]

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
            "KLANGK_CMD_OVERRIDE" in e for e in call_kwargs["environment"]
        )

        await s.stop()

    async def test_start_exception_closes_docker(self):
        stream = _mock_stream()
        exec_obj = _mock_exec(stream)
        exec_obj.resize = AsyncMock(side_effect=RuntimeError("resize fail"))
        stream.read_out = AsyncMock(return_value=None)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            try:
                await s.start()
            except RuntimeError:
                pass

        docker.close.assert_awaited()


class TestReadLoop:
    async def test_output_from_stream(self):
        first_msg = MagicMock()
        first_msg.data = b"prompt"
        second_msg = MagicMock()
        second_msg.data = b"hello world"
        stream = _mock_stream()
        # First read consumed by start(), second by read_loop
        stream.read_out = AsyncMock(side_effect=[first_msg, second_msg, None])
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.1)
        # First msg queued by start(), second by read_loop
        data1 = s._output_queue.get_nowait()
        assert data1 == "prompt"
        data2 = s._output_queue.get_nowait()
        assert data2 == "hello world"

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

    async def test_read_loop_handles_exception(self):
        first_msg = MagicMock()
        first_msg.data = b"prompt"
        stream = _mock_stream()
        stream.read_out = AsyncMock(
            side_effect=[first_msg, RuntimeError("connection lost")]
        )
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.1)
        # Should get prompt then None (from exception cleanup)
        s._output_queue.get_nowait()  # prompt
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

    async def test_write_exception_suppressed(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        stream.write_in = AsyncMock(side_effect=RuntimeError("broken"))
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        # Should not raise
        await s.write("hello")
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

    async def test_resize_exception_suppressed(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        exec_obj.resize = AsyncMock(side_effect=[None, RuntimeError("broken")])
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        # Should not raise (second call raises)
        await s.resize(200, 50)
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

    async def test_stop_handles_close_exceptions(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        stream.close = AsyncMock(side_effect=RuntimeError("close fail"))
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)
        docker.close = AsyncMock(side_effect=RuntimeError("docker close fail"))

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        # Should not raise despite exceptions
        await s.stop()
        assert s._stream is None
        assert s._exec is None

    async def test_stop_when_not_started(self):
        s = TerminalSession("cid")
        await s.stop()


class TestOutput:
    async def test_output_yields_data(self):
        first_msg = MagicMock()
        first_msg.data = b"prompt"
        second_msg = MagicMock()
        second_msg.data = b"output"
        stream = _mock_stream()
        stream.read_out = AsyncMock(side_effect=[first_msg, second_msg, None])
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        collected = []
        async for data in s.output():
            collected.append(data)

        assert "prompt" in collected
        assert "output" in collected
        await s.stop()


class TestIsAlive:
    async def test_alive_while_running(self):
        stream = _mock_stream()
        # Never-ending stream — read_out blocks forever
        event = asyncio.Event()

        async def slow_read():
            await event.wait()
            return None

        first_msg = MagicMock()
        first_msg.data = b"prompt"
        stream.read_out = AsyncMock(side_effect=[first_msg, slow_read()])
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        assert s.is_alive is True
        event.set()
        await s.stop()

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
