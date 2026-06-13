"""Tests for the Pi RPC agent client."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from klangk_backend.agent import (
    AgentProcessDied,
    AgentSession,
    any_running,
    get_session,
    is_running,
    stop_session,
    _agents,
)


@pytest.fixture(autouse=True)
def _clear_agents():
    _agents.clear()
    yield
    _agents.clear()


class TestAgentSession:
    async def test_send_prompt_collects_text_deltas(self):
        events = [
            {"type": "agent_start"},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "Hello ",
                },
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "world",
                },
            },
            {"type": "agent_end"},
        ]
        stdout_data = "\n".join(json.dumps(e) for e in events) + "\n"

        proc = AsyncMock()
        proc.returncode = None
        proc.stdin = AsyncMock()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(stdout_data.encode())
        proc.stdout.feed_eof()
        proc.stderr = asyncio.StreamReader()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            session = AgentSession("cid")
            result = await session.send_prompt("test")

        assert result == "Hello world"

    async def test_send_prompt_timeout(self):
        proc = AsyncMock()
        proc.returncode = None
        proc.stdin = AsyncMock()
        # stdout that never produces data
        proc.stdout = asyncio.StreamReader()
        proc.stderr = asyncio.StreamReader()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            session = AgentSession("cid")
            result = await session.send_prompt("test", timeout=0.1)

        assert "timed out" in result

    async def test_send_prompt_empty_response(self):
        events = [
            {"type": "agent_start"},
            {"type": "agent_end"},
        ]
        stdout_data = "\n".join(json.dumps(e) for e in events) + "\n"

        proc = AsyncMock()
        proc.returncode = None
        proc.stdin = AsyncMock()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(stdout_data.encode())
        proc.stdout.feed_eof()
        proc.stderr = asyncio.StreamReader()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            session = AgentSession("cid")
            result = await session.send_prompt("test")

        assert result == "I had nothing to say."

    async def test_process_dies_raises(self):
        """AgentProcessDied raised when process exits mid-prompt."""
        events = [
            {"type": "agent_start"},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "partial",
                },
            },
        ]
        stdout_data = "\n".join(json.dumps(e) for e in events) + "\n"

        proc = AsyncMock()
        proc.returncode = None
        proc.stdin = AsyncMock()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(stdout_data.encode())
        proc.stdout.feed_eof()
        proc.stderr = asyncio.StreamReader()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            session = AgentSession("cid")
            # Simulate process dying after reading
            proc.returncode = 1
            with pytest.raises(AgentProcessDied):
                await session.send_prompt("test")

    async def test_stop(self):
        proc = AsyncMock()
        proc.returncode = None
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        session = AgentSession("cid")
        session._proc = proc
        await session.stop()

        proc.kill.assert_called_once()
        assert session._proc is None

    async def test_reuses_running_proc(self):
        """Second prompt reuses the existing subprocess."""
        events = [
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "one",
                },
            },
            {"type": "agent_end"},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "two",
                },
            },
            {"type": "agent_end"},
        ]
        stdout_data = "\n".join(json.dumps(e) for e in events) + "\n"

        proc = AsyncMock()
        proc.returncode = None
        proc.stdin = AsyncMock()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(stdout_data.encode())
        proc.stdout.feed_eof()
        proc.stderr = asyncio.StreamReader()

        with patch(
            "asyncio.create_subprocess_exec", return_value=proc
        ) as mock_exec:
            session = AgentSession("cid")
            r1 = await session.send_prompt("first")
            r2 = await session.send_prompt("second")

        assert r1 == "one"
        assert r2 == "two"
        # Only one subprocess created
        assert mock_exec.call_count == 1

    async def test_process_exit_mid_read(self):
        """Handles process exiting before agent_end."""
        proc = AsyncMock()
        proc.returncode = None
        proc.stdin = AsyncMock()
        proc.stdout = asyncio.StreamReader()
        # Feed partial data then EOF
        proc.stdout.feed_data(b'{"type":"agent_start"}\n')
        proc.stdout.feed_eof()
        proc.stderr = asyncio.StreamReader()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            session = AgentSession("cid")
            result = await session.send_prompt("test")

        assert result == "I had nothing to say."

    async def test_malformed_jsonl_skipped(self):
        """Non-JSON lines are silently skipped."""
        lines = [
            "not json at all",
            json.dumps(
                {
                    "type": "message_update",
                    "assistantMessageEvent": {
                        "type": "text_delta",
                        "delta": "ok",
                    },
                }
            ),
            json.dumps({"type": "agent_end"}),
        ]
        stdout_data = "\n".join(lines) + "\n"

        proc = AsyncMock()
        proc.returncode = None
        proc.stdin = AsyncMock()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(stdout_data.encode())
        proc.stdout.feed_eof()
        proc.stderr = asyncio.StreamReader()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            session = AgentSession("cid")
            result = await session.send_prompt("test")

        assert result == "ok"


class TestGetSession:
    async def test_creates_new_session(self):
        session = await get_session("cid-1")
        assert session.container_id == "cid-1"
        assert "cid-1" in _agents

    async def test_reuses_existing_session(self):
        s1 = await get_session("cid-1")
        s2 = await get_session("cid-1")
        assert s1 is s2


class TestStopSession:
    async def test_stop_existing(self):
        session = await get_session("cid-1")
        session._proc = AsyncMock()
        session._proc.returncode = None
        session._proc.kill = MagicMock()
        session._proc.wait = AsyncMock()

        await stop_session("cid-1")
        assert "cid-1" not in _agents

    async def test_stop_nonexistent(self):
        await stop_session("no-such-container")  # should not raise


class TestIsRunning:
    def test_no_session(self):
        assert not is_running("cid")

    def test_no_proc(self):
        _agents["cid"] = AgentSession("cid")
        assert not is_running("cid")

    def test_proc_alive(self):
        s = AgentSession("cid")
        s._proc = MagicMock()
        s._proc.returncode = None
        _agents["cid"] = s
        assert is_running("cid")

    def test_proc_dead(self):
        s = AgentSession("cid")
        s._proc = MagicMock()
        s._proc.returncode = 1
        _agents["cid"] = s
        assert not is_running("cid")


class TestAnyRunning:
    def test_empty(self):
        assert not any_running()

    def test_one_alive(self):
        s = AgentSession("cid")
        s._proc = MagicMock()
        s._proc.returncode = None
        _agents["cid"] = s
        assert any_running()

    def test_all_dead(self):
        s = AgentSession("cid")
        s._proc = MagicMock()
        s._proc.returncode = 0
        _agents["cid"] = s
        assert not any_running()
