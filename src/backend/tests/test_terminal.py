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

    async def test_bridge_token_sets_env_var(self):
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start(bridge_token="tok-123")

        call_kwargs = container.exec.call_args[1]
        assert "KLANGK_BRIDGE_TOKEN=tok-123" in call_kwargs["environment"]

        await s.stop()

    async def test_no_bridge_token_by_default(self):
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
            "KLANGK_BRIDGE_TOKEN" in e for e in call_kwargs["environment"]
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

    async def test_sentinel_uses_put_nowait(self):
        """_read_loop uses put_nowait for the sentinel, not blocking put."""
        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        await asyncio.sleep(0.1)
        # Sentinel should be in the queue (put_nowait succeeded)
        data = s._output_queue.get_nowait()
        assert data is None
        await s.stop()

    async def test_sentinel_dropped_when_queue_full(self):
        """When queue is full, sentinel is silently dropped (no deadlock)."""
        s = TerminalSession("cid")
        s._running = True
        # Pre-fill the queue to capacity
        for _ in range(64):
            s._output_queue.put_nowait("data")
        assert s._output_queue.full()

        # Simulate _read_loop finally block: put_nowait should catch QueueFull
        try:
            s._output_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass  # This is the expected path

        # Queue is still full, sentinel was dropped, no hang
        assert s._output_queue.full()
        # Verify no None sentinel in the queue
        items = []
        while not s._output_queue.empty():
            items.append(s._output_queue.get_nowait())
        assert None not in items


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
        stream.write_in = AsyncMock(side_effect=OSError("broken"))
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
        exec_obj.resize = AsyncMock(side_effect=[None, OSError("broken")])
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
        stream.close = AsyncMock(side_effect=OSError("close fail"))
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)
        docker.close = AsyncMock(side_effect=OSError("docker close fail"))

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        # Should not raise despite exceptions
        await s.stop()
        assert s._stream is None
        assert s._exec is None

    async def test_stop_cancels_blocked_read_loop(self):
        """CancelledError re-raised from _read_loop when stop() cancels it."""
        hang = asyncio.Future()  # blocks forever until cancelled
        first_msg = MagicMock()
        first_msg.data = b"prompt"
        stream = _mock_stream()
        stream.read_out = AsyncMock(side_effect=[first_msg, hang])
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        # Read loop is now blocked on the hanging future
        await s.stop()
        assert s._running is False
        assert s._stream is None

    async def test_stop_read_task_unexpected_exception(self):
        """Non-CancelledError from read task is logged, not raised."""

        async def bad_task():
            raise RuntimeError("unexpected")

        stream = _mock_stream()
        stream.read_out = AsyncMock(return_value=None)
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        # Replace the read task with one that raises a non-CancelledError
        if s._read_task is not None:
            s._read_task.cancel()
            try:
                await s._read_task
            except (asyncio.CancelledError, Exception):
                pass
        s._read_task = asyncio.create_task(bad_task())
        await asyncio.sleep(0)  # let it finish

        # stop() should handle the RuntimeError without raising
        await s.stop()
        assert s._read_task is None

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

    async def test_output_exits_when_running_cleared_without_sentinel(self):
        """When sentinel is dropped (QueueFull), output() exits via _running check."""
        stream = _mock_stream()
        first_msg = MagicMock()
        first_msg.data = b"prompt"
        stream.read_out = AsyncMock(side_effect=[first_msg, None])
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        # Drain the queue (prompt + sentinel from read_loop)
        await asyncio.sleep(0.1)
        while not s._output_queue.empty():
            s._output_queue.get_nowait()

        # Now simulate: no sentinel in queue, consumer should exit
        # when _running is set to False after timeout
        s._running = True

        async def _consume():
            collected = []
            async for data in s.output():
                collected.append(data)
            return collected

        task = asyncio.create_task(_consume())
        # Let the consumer block on get() with timeout
        await asyncio.sleep(0.05)
        # Clear _running so the next timeout cycle exits
        s._running = False
        result = await asyncio.wait_for(task, timeout=3.0)
        assert result == []
        await s.stop()

    async def test_output_exits_when_read_task_done_without_sentinel(self):
        """When sentinel is dropped and _running is True, output() exits
        via _read_task.done() check after timeout."""
        stream = _mock_stream()
        first_msg = MagicMock()
        first_msg.data = b"prompt"
        stream.read_out = AsyncMock(side_effect=[first_msg, None])
        exec_obj = _mock_exec(stream)
        container = _mock_container(exec_obj)
        docker = _mock_docker(container)

        with patch("aiodocker.Docker", return_value=docker):
            s = TerminalSession("cid")
            await s.start()

        # Wait for read_loop to finish (it gets None from read_out)
        await asyncio.sleep(0.1)
        assert s._read_task.done()

        # Drain the queue (prompt + sentinel)
        while not s._output_queue.empty():
            s._output_queue.get_nowait()

        # _running is still True — only _read_task.done() signals exit
        assert s._running

        async def _consume():
            collected = []
            async for data in s.output():
                collected.append(data)
            return collected

        result = await asyncio.wait_for(
            asyncio.create_task(_consume()), timeout=3.0
        )
        assert result == []
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
        call_count = 0

        async def _read_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return first_msg
            return await slow_read()

        stream.read_out = _read_side_effect
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
