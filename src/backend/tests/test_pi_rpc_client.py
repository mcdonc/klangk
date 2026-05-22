"""Tests for pi_rpc_client: subprocess communication, read loop, commands."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bark_backend.pi_rpc_client import PiRpcClient


def _mock_proc(stdout_data=b"", returncode=None, stderr_data=b""):
    """Create a mock asyncio subprocess."""
    proc = MagicMock()
    proc.returncode = returncode

    stdout = AsyncMock()
    _chunks = [stdout_data] if stdout_data else []
    _call_count = 0

    async def _read(n):
        nonlocal _call_count
        if _call_count < len(_chunks):
            chunk = _chunks[_call_count]
            _call_count += 1
            return chunk
        return b""

    stdout.read = _read
    stdout.at_eof = MagicMock(return_value=False)
    proc.stdout = stdout

    stderr = MagicMock()
    stderr.read = MagicMock(return_value=stderr_data)
    proc.stderr = stderr

    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    proc.stdin = stdin

    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    proc.wait = AsyncMock()

    return proc


class TestInit:
    def test_initial_state(self):
        client = PiRpcClient("cid-1")
        assert client.container_id == "cid-1"
        assert client._proc is None
        assert client._running is False
        assert not client.is_alive


class TestConnect:
    async def test_connect(self):
        proc = _mock_proc()
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            client = PiRpcClient("cid-1")
            await client.connect()
        assert client._running is True
        assert client._proc is proc
        assert client._read_task is not None
        # Clean up
        client._running = False
        client._read_task.cancel()
        try:
            await client._read_task
        except asyncio.CancelledError:
            pass


class TestIsAlive:
    def test_alive_when_running(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc(returncode=None)
        assert client.is_alive is True

    def test_dead_when_no_proc(self):
        client = PiRpcClient("cid")
        assert client.is_alive is False

    def test_dead_when_returncode_set(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc(returncode=1)
        assert client.is_alive is False


class TestSendCommand:
    async def test_send_command(self):
        client = PiRpcClient("cid")
        proc = _mock_proc(returncode=None)
        client._proc = proc
        client._running = True

        await client.send_command({"type": "test"})
        written = proc.stdin.write.call_args[0][0]
        parsed = json.loads(written.decode())
        assert parsed == {"type": "test"}
        proc.stdin.drain.assert_awaited_once()

    async def test_send_command_dead_proc(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc(returncode=1)
        with pytest.raises(RuntimeError, match="dead"):
            await client.send_command({"type": "test"})

    async def test_send_command_no_proc(self):
        client = PiRpcClient("cid")
        with pytest.raises(RuntimeError, match="dead"):
            await client.send_command({"type": "test"})


class TestPrompt:
    async def test_prompt(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc(returncode=None)
        client._running = True

        await client.prompt("hello")
        written = json.loads(client._proc.stdin.write.call_args[0][0].decode())
        assert written["type"] == "prompt"
        assert written["message"] == "hello"
        assert "images" not in written

    async def test_prompt_with_images(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc(returncode=None)
        client._running = True

        images = [{"url": "http://example.com/img.png"}]
        await client.prompt("look at this", images=images)
        written = json.loads(client._proc.stdin.write.call_args[0][0].decode())
        assert written["type"] == "prompt"
        assert written["images"] == images


class TestSteer:
    async def test_steer(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc(returncode=None)
        client._running = True

        await client.steer("go left")
        written = json.loads(client._proc.stdin.write.call_args[0][0].decode())
        assert written == {"type": "steer", "message": "go left"}


class TestFollowUp:
    async def test_follow_up(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc(returncode=None)
        client._running = True

        await client.follow_up("and then?")
        written = json.loads(client._proc.stdin.write.call_args[0][0].decode())
        assert written == {"type": "follow_up", "message": "and then?"}


class TestAbort:
    async def test_abort(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc(returncode=None)
        client._running = True

        await client.abort()
        written = json.loads(client._proc.stdin.write.call_args[0][0].decode())
        assert written == {"type": "abort"}


class TestReadLoop:
    async def test_reads_json_events(self):
        event = {"type": "agent_start"}
        data = json.dumps(event).encode() + b"\n"

        client = PiRpcClient("cid")
        client._proc = _mock_proc(stdout_data=data)
        client._running = True

        await client.read_loop()

        result = await client._event_queue.get()
        assert result == event
        # Sentinel None at end
        sentinel = await client._event_queue.get()
        assert sentinel is None

    async def test_reads_multiple_events(self):
        e1 = {"type": "agent_start"}
        e2 = {"type": "agent_end"}
        data = (
            json.dumps(e1).encode() + b"\n" + json.dumps(e2).encode() + b"\n"
        )

        client = PiRpcClient("cid")
        client._proc = _mock_proc(stdout_data=data)
        client._running = True

        await client.read_loop()

        assert await client._event_queue.get() == e1
        assert await client._event_queue.get() == e2
        assert await client._event_queue.get() is None

    async def test_skips_empty_lines(self):
        event = {"type": "test"}
        data = b"\n\n" + json.dumps(event).encode() + b"\n\n"

        client = PiRpcClient("cid")
        client._proc = _mock_proc(stdout_data=data)
        client._running = True

        await client.read_loop()

        assert await client._event_queue.get() == event
        assert await client._event_queue.get() is None

    async def test_skips_non_json(self):
        data = b"not json at all\n"

        client = PiRpcClient("cid")
        client._proc = _mock_proc(stdout_data=data)
        client._running = True

        await client.read_loop()

        # Only sentinel
        assert await client._event_queue.get() is None

    async def test_stops_on_eof(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc()
        client._proc.stdout.at_eof = MagicMock(return_value=True)
        client._running = True

        await client.read_loop()

        assert await client._event_queue.get() is None

    async def test_stops_on_returncode(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc(returncode=0)
        client._running = True

        await client.read_loop()

        assert await client._event_queue.get() is None

    async def test_stops_when_not_running(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc()
        client._running = False

        await client.read_loop()

        assert await client._event_queue.get() is None

    async def test_handles_read_exception(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc()
        client._running = True

        async def _explode(n):
            raise IOError("broken pipe")

        client._proc.stdout.read = _explode

        await client.read_loop()

        # Should still get sentinel
        assert await client._event_queue.get() is None

    async def test_logs_stderr_on_exit(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc(stderr_data=b"some error output")
        client._running = True

        await client.read_loop()
        assert await client._event_queue.get() is None

    async def test_logs_stderr_awaitable(self):
        """When stderr.read() returns a coroutine, it should be awaited."""
        client = PiRpcClient("cid")
        client._proc = _mock_proc()
        client._running = True

        async def async_stderr_read():
            return b"async error"

        client._proc.stderr.read = MagicMock(return_value=async_stderr_read())

        await client.read_loop()
        assert await client._event_queue.get() is None

    async def test_stderr_read_exception_handled(self):
        client = PiRpcClient("cid")
        client._proc = _mock_proc()
        client._running = True
        client._proc.stderr.read = MagicMock(
            side_effect=OSError("stderr broken")
        )

        await client.read_loop()
        assert await client._event_queue.get() is None


class TestEvents:
    async def test_yields_events(self):
        client = PiRpcClient("cid")
        await client._event_queue.put({"type": "a"})
        await client._event_queue.put({"type": "b"})
        await client._event_queue.put(None)

        results = []
        async for event in client.events():
            results.append(event)

        assert len(results) == 2
        assert results[0]["type"] == "a"
        assert results[1]["type"] == "b"

    async def test_empty_stream(self):
        client = PiRpcClient("cid")
        await client._event_queue.put(None)

        results = []
        async for event in client.events():
            results.append(event)

        assert results == []


class TestDetach:
    def test_detach_stops_reading_without_killing(self):
        client = PiRpcClient("cid")
        proc = _mock_proc(returncode=None)
        client._proc = proc
        client._running = True
        task = asyncio.get_event_loop().create_future()
        client._read_task = task

        client.detach()

        assert client._running is False
        assert client._proc is None
        assert client._read_task is None
        proc.terminate.assert_not_called()

    def test_detach_no_proc(self):
        client = PiRpcClient("cid")
        client._running = True
        client.detach()
        assert client._running is False
        assert client._proc is None


class TestDisconnect:
    async def test_disconnect_running_proc(self):
        client = PiRpcClient("cid")
        proc = _mock_proc(returncode=None)
        client._proc = proc
        client._running = True
        client._read_task = asyncio.create_task(asyncio.sleep(10))

        await client.disconnect()

        assert client._running is False
        assert client._proc is None
        proc.terminate.assert_called_once()

    async def test_disconnect_no_proc(self):
        client = PiRpcClient("cid")
        client._running = True
        await client.disconnect()
        assert client._running is False

    async def test_disconnect_terminate_timeout(self):
        client = PiRpcClient("cid")
        proc = _mock_proc(returncode=None)
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        client._proc = proc
        client._running = True
        client._read_task = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0)  # let read_task start

        await client.disconnect()

        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        assert client._proc is None

    async def test_disconnect_terminate_process_gone(self):
        client = PiRpcClient("cid")
        proc = _mock_proc(returncode=None)
        proc.terminate = MagicMock(side_effect=ProcessLookupError)
        client._proc = proc
        client._running = True
        client._read_task = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0)

        await client.disconnect()

        assert client._proc is None

    async def test_disconnect_kill_process_gone(self):
        client = PiRpcClient("cid")
        proc = _mock_proc(returncode=None)
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        proc.kill = MagicMock(side_effect=ProcessLookupError)
        client._proc = proc
        client._running = True
        client._read_task = asyncio.create_task(asyncio.sleep(0))
        await asyncio.sleep(0)

        await client.disconnect()

        assert client._proc is None
