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


def _make_session(container_id="cid"):
    """Create an AgentSession with home setup already done."""
    s = AgentSession(container_id)
    s._home_ready = True
    return s


_ACK = {"type": "response", "command": "prompt", "success": True}


class TestAgentSession:
    async def test_send_prompt_collects_text_deltas(self):
        events = [
            _ACK,
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
            session = _make_session()
            result = await session.send_prompt("test")

        assert result == "Hello world"

    async def test_send_prompt_timeout(self):
        proc = AsyncMock()
        proc.returncode = None
        proc.stdin = AsyncMock()
        # Feed ack + agent_start so we get past the preamble, then
        # nothing — simulating a model that never responds.
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(
            json.dumps(_ACK).encode()
            + b"\n"
            + json.dumps({"type": "agent_start"}).encode()
            + b"\n"
        )
        proc.stderr = asyncio.StreamReader()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            session = _make_session()
            result = await session.send_prompt("test", timeout=0.1)

        assert "timed out" in result

    async def test_send_prompt_empty_response(self):
        events = [
            _ACK,
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
            session = _make_session()
            result = await session.send_prompt("test")

        assert result == "I had nothing to say."

    async def test_thinking_fallback_when_no_text_delta(self):
        """When model only emits thinking_delta, use thinking as response."""
        events = [
            _ACK,
            {"type": "agent_start"},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_start",
                },
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_delta",
                    "delta": "The answer is 42.",
                },
            },
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "thinking_end",
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
            session = _make_session()
            result = await session.send_prompt("test")

        assert result == "The answer is 42."

    async def test_unhandled_delta_type_logged(self, caplog):
        """Unknown delta types are logged at debug level."""
        events = [
            _ACK,
            {"type": "agent_start"},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "mystery_delta",
                    "delta": "?",
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

        import logging

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            caplog.at_level(logging.DEBUG, logger="klangk_backend.agent"),
        ):
            session = _make_session()
            await session.send_prompt("test")

        assert any("mystery_delta" in r.message for r in caplog.records)

    async def test_process_dies_raises(self):
        """AgentProcessDied raised when process exits mid-prompt."""
        events = [
            _ACK,
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
            session = _make_session()
            # Simulate process dying after reading
            proc.returncode = 1
            with pytest.raises(AgentProcessDied):
                await session.send_prompt("test")

    async def test_stop(self):
        proc = AsyncMock()
        proc.returncode = None
        proc.kill = MagicMock()
        proc.wait = AsyncMock()

        session = _make_session()
        session._proc = proc
        await session.stop()

        proc.kill.assert_called_once()
        assert session._proc is None

    async def test_reuses_running_proc(self):
        """Second prompt reuses the existing subprocess."""
        events = [
            _ACK,
            {"type": "agent_start"},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "one",
                },
            },
            {"type": "agent_end"},
            _ACK,
            {"type": "agent_start"},
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
            session = _make_session()
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
        proc.stdout.feed_data(
            json.dumps(_ACK).encode() + b"\n" + b'{"type":"agent_start"}\n'
        )
        proc.stdout.feed_eof()
        proc.stderr = asyncio.StreamReader()

        with patch("asyncio.create_subprocess_exec", return_value=proc):
            session = _make_session()
            result = await session.send_prompt("test")

        assert result == "I had nothing to say."

    async def test_malformed_jsonl_skipped(self):
        """Non-JSON lines are silently skipped."""
        lines = [
            json.dumps(_ACK),
            json.dumps({"type": "agent_start"}),
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
            session = _make_session()
            result = await session.send_prompt("test")

        assert result == "ok"

    async def test_skip_to_agent_start_discards_stale(self):
        """_skip_to_agent_start skips stale events."""
        session = _make_session()
        reader = asyncio.StreamReader()
        # Stale events from a prior turn, then agent_start
        reader.feed_data(
            json.dumps({"type": "message_update"}).encode()
            + b"\n"
            + json.dumps({"type": "agent_end"}).encode()
            + b"\n"
            + json.dumps({"type": "agent_start"}).encode()
            + b"\n"
        )
        reader.feed_eof()
        await session._skip_to_agent_start(reader)

    async def test_skip_to_agent_start_eof(self):
        """_skip_to_agent_start returns on EOF."""
        session = _make_session()
        reader = asyncio.StreamReader()
        reader.feed_eof()
        await session._skip_to_agent_start(reader)

    async def test_skip_to_agent_start_skips_non_json(self):
        """_skip_to_agent_start skips malformed lines."""
        session = _make_session()
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"garbage\n" + json.dumps({"type": "agent_start"}).encode() + b"\n"
        )
        reader.feed_eof()
        await session._skip_to_agent_start(reader)

    async def test_ack_timeout_still_proceeds(self):
        """If ack times out, send_prompt still tries to read response."""
        events = [
            # No _ACK — simulates ack timeout
            {"type": "agent_start"},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "late",
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

        with (
            patch("asyncio.create_subprocess_exec", return_value=proc),
            patch(
                "klangk_backend.agent.AgentSession._wait_for_ack",
                side_effect=asyncio.TimeoutError,
            ),
        ):
            session = _make_session()
            result = await session.send_prompt("test")

        # Should still get a result (skip_to_agent_start finds it)
        assert result == "late"

    async def test_wait_for_ack_eof(self):
        """_wait_for_ack returns on EOF without error."""
        session = _make_session()
        reader = asyncio.StreamReader()
        reader.feed_eof()
        await session._wait_for_ack(reader)  # should not raise

    async def test_wait_for_ack_skips_non_json(self):
        """_wait_for_ack skips malformed lines before ack."""
        session = _make_session()
        reader = asyncio.StreamReader()
        reader.feed_data(b"garbage\n")
        reader.feed_data(json.dumps(_ACK).encode() + b"\n")
        reader.feed_eof()
        await session._wait_for_ack(reader)  # should not raise


class TestGetSession:
    async def test_creates_new_session(self):
        session = await get_session("cid-1")
        assert session.container_id == "cid-1"
        assert "cid-1" in _agents

    async def test_reuses_existing_session(self):
        s1 = await get_session("cid-1")
        s2 = await get_session("cid-1")
        assert s1 is s2


class TestEnsureHome:
    async def test_ensure_home_creates_dir_and_runs_login_shell(
        self, tmp_path
    ):
        from klangk_backend import container, model, workspaces

        session = AgentSession("cid")
        # Simulate registry mapping so workspace_id_for("cid") returns "ws1"
        container.registry.track_activity("cid", "ws1")

        fake_ws = {"user_id": "owner1"}
        fake_home = tmp_path / "home"
        fake_home.mkdir()

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with (
            patch.object(
                model,
                "get_workspace_by_id",
                return_value=fake_ws,
            ),
            patch.object(
                workspaces,
                "home_path",
                return_value=fake_home,
            ),
            patch.object(
                workspaces,
                "ensure_home_symlink",
                return_value=("/home/MrBoops", True),
            ) as mock_symlink,
            patch.object(
                workspaces,
                "populate_home_skel",
                new_callable=AsyncMock,
            ) as mock_skel,
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
        ):
            result = await session._ensure_home()

        assert result == "/home/MrBoops"
        assert session._home_ready is True
        mock_symlink.assert_called_once()
        mock_skel.assert_awaited_once_with(
            "cid", "00000000-0000-0000-0000-000000000001"
        )
        container.registry.states.pop("ws1", None)

    async def test_ensure_home_cached(self):
        session = AgentSession("cid")
        session._home_ready = True
        result = await session._ensure_home()
        assert result == "/home/MrBoops"

    async def test_ensure_home_workspace_not_in_db(self):
        from klangk_backend import container, model
        from klangk_backend.agent import AgentSetupError

        session = AgentSession("cid")
        container.registry.track_activity("cid", "ws-gone")

        with patch.object(model, "get_workspace_by_id", return_value=None):
            with pytest.raises(AgentSetupError, match="not found in database"):
                await session._ensure_home()

        container.registry.states.pop("ws-gone", None)


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
        _agents["cid"] = _make_session()
        assert not is_running("cid")

    def test_proc_alive(self):
        s = _make_session()
        s._proc = MagicMock()
        s._proc.returncode = None
        _agents["cid"] = s
        assert is_running("cid")

    def test_proc_dead(self):
        s = _make_session()
        s._proc = MagicMock()
        s._proc.returncode = 1
        _agents["cid"] = s
        assert not is_running("cid")


class TestAnyRunning:
    def test_empty(self):
        assert not any_running()

    def test_one_alive(self):
        s = _make_session()
        s._proc = MagicMock()
        s._proc.returncode = None
        _agents["cid"] = s
        assert any_running()

    def test_all_dead(self):
        s = _make_session()
        s._proc = MagicMock()
        s._proc.returncode = 0
        _agents["cid"] = s
        assert not any_running()
