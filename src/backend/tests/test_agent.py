"""Tests for the Pi RPC agent client."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from klangk_backend.agent import (
    AgentError,
    AgentProcessDied,
    AgentSession,
    any_running,
    get_session,
    is_running,
    stop_session,
    _agents,
)


class _FakeContainerState:
    def __init__(self, container_id="cid"):
        self.container_id = container_id


@pytest.fixture(autouse=True)
def _clear_agents():
    _agents.clear()
    yield
    _agents.clear()


@pytest.fixture(autouse=True)
def _mock_container_registry():
    """Provide a default container registry state for all agent tests."""
    with patch(
        "klangk_backend.agent.container.registry.get_state",
        return_value=_FakeContainerState("cid"),
    ):
        yield


@pytest.fixture(autouse=True)
async def _seed_agent_db(db):
    """Seed the agent user so agent_handle()/agent_email() work."""
    import klangk_backend.model as model

    async with model.transaction() as agent_db:
        await agent_db.execute(
            "INSERT OR REPLACE INTO users"
            " (id, email, password_hash, verified, provider, handle)"
            " VALUES (?, ?, NULL, 1, 'system', ?)",
            (model.AGENT_USER_ID, "MrBoops@example.com", "MrBoops"),
        )
    model.clear_agent_cache()


def _make_session(workspace_id="ws-id"):
    """Create an AgentSession with home setup already done."""
    s = AgentSession(workspace_id)
    s._home_ready = True
    s._last_container_id = "cid"
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

    async def test_llm_error_surfaced(self):
        """LLM errors (e.g. 429) are returned to the user."""
        events = [
            _ACK,
            {"type": "agent_start"},
            {
                "type": "message_start",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "429 rate limited",
                },
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "429 rate limited",
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

        assert "429 rate limited" in result

    async def test_auto_retry_resets_and_reads_final(self):
        """Pi auto-retry cycles are handled; final response is used."""
        events = [
            _ACK,
            {"type": "agent_start"},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "stopReason": "error",
                    "errorMessage": "429 limit",
                },
            },
            {"type": "agent_end", "willRetry": True},
            {
                "type": "auto_retry_start",
                "attempt": 1,
                "maxAttempts": 3,
                "errorMessage": "429 limit",
            },
            {"type": "agent_start"},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "success",
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

        assert result == "success"

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
        session = await get_session("ws-1")
        assert session.workspace_id == "ws-1"
        assert "ws-1" in _agents

    async def test_reuses_existing_session(self):
        s1 = await get_session("ws-1")
        s2 = await get_session("ws-1")
        assert s1 is s2


class TestEnsureHome:
    async def test_ensure_home_creates_dir_and_runs_login_shell(
        self, tmp_path
    ):
        from klangk_backend import model, workspaces

        session = AgentSession("ws1")

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
            result = await session._ensure_home("cid")

        assert result == "/home/MrBoops"
        assert session._home_ready is True
        mock_symlink.assert_called_once()
        mock_skel.assert_awaited_once_with(
            "cid", "00000000-0000-0000-0000-000000000001"
        )

    async def test_ensure_home_cached(self):
        session = AgentSession("ws-id")
        session._home_ready = True
        result = await session._ensure_home("cid")
        assert result == "/home/MrBoops"

    async def test_ensure_home_workspace_not_in_db(self):
        from klangk_backend import model
        from klangk_backend.agent import AgentSetupError

        session = AgentSession("ws-gone")

        with patch.object(model, "get_workspace_by_id", return_value=None):
            with pytest.raises(AgentSetupError, match="not found in database"):
                await session._ensure_home("cid")


class TestStopSession:
    async def test_stop_existing(self):
        session = await get_session("ws-1")
        session._proc = AsyncMock()
        session._proc.returncode = None
        session._proc.kill = MagicMock()
        session._proc.wait = AsyncMock()

        await stop_session("ws-1")
        assert "ws-1" not in _agents

    async def test_stop_nonexistent(self):
        await stop_session("no-such-ws")  # should not raise


class TestIsRunning:
    def test_no_session(self):
        assert not is_running("ws-id")

    def test_no_proc(self):
        _agents["ws-id"] = _make_session()
        assert not is_running("ws-id")

    def test_proc_alive(self):
        s = _make_session()
        s._proc = MagicMock()
        s._proc.returncode = None
        _agents["ws-id"] = s
        assert is_running("ws-id")

    def test_proc_dead(self):
        s = _make_session()
        s._proc = MagicMock()
        s._proc.returncode = 1
        _agents["ws-id"] = s
        assert not is_running("ws-id")


class TestAnyRunning:
    def test_empty(self):
        assert not any_running()

    def test_one_alive(self):
        s = _make_session()
        s._proc = MagicMock()
        s._proc.returncode = None
        _agents["ws-id"] = s
        assert any_running()

    def test_all_dead(self):
        s = _make_session()
        s._proc = MagicMock()
        s._proc.returncode = 0
        _agents["ws-id"] = s
        assert not any_running()


class TestMonitorProcess:
    async def test_monitor_broadcasts_on_death(self):
        from klangk_backend import model
        from klangk_backend.agent import _broadcast_agent_disconnect

        with (
            patch.object(
                model,
                "get_workspace_by_id",
                new_callable=AsyncMock,
                return_value={"id": "ws-mon"},
            ),
            patch.object(
                model,
                "add_chat_message",
                new_callable=AsyncMock,
                return_value={
                    "id": "msg-1",
                    "message": "MrBoops has disconnected",
                },
            ) as mock_chat,
            patch(
                "klangk_backend.agent._get_workspace_session"
            ) as mock_get_session,
        ):
            mock_session = MagicMock()
            mock_get_session.return_value = mock_session

            await _broadcast_agent_disconnect("ws-mon")

            mock_chat.assert_awaited_once()
            assert "disconnected" in mock_chat.call_args[0][3]
            assert mock_session.broadcast.call_count == 3

    async def test_broadcast_no_workspace(self):
        from klangk_backend.agent import _broadcast_agent_disconnect

        # Should not raise when workspace_id is empty
        await _broadcast_agent_disconnect("")

    async def test_broadcast_disconnect_deleted_workspace(self):
        from klangk_backend.agent import _broadcast_agent_disconnect

        # Should not raise when workspace has been deleted
        await _broadcast_agent_disconnect("deleted-ws-id")

    async def test_stop_cancels_monitor(self):
        session = _make_session()
        mock_task = MagicMock()
        session._monitor_task = mock_task
        session._proc = AsyncMock()
        session._proc.returncode = None
        session._proc.kill = MagicMock()
        session._proc.wait = AsyncMock()

        await session.stop()

        mock_task.cancel.assert_called_once()
        assert session._monitor_task is None
        assert session._proc is None

    async def test_monitor_auto_restarts(self):
        from klangk_backend import model

        session = _make_session("ws-restart")
        _agents["ws-restart"] = session

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.wait = AsyncMock()
        mock_proc.stderr = asyncio.StreamReader()
        mock_proc.stderr.feed_eof()
        session._proc = mock_proc

        with (
            patch.object(
                model,
                "add_chat_message",
                new_callable=AsyncMock,
                return_value={"id": "m1", "message": "msg"},
            ),
            patch(
                "klangk_backend.agent._get_workspace_session",
                return_value=MagicMock(),
            ),
            patch.object(
                session,
                "_ensure_started",
                new_callable=AsyncMock,
            ) as mock_start,
            patch(
                "klangk_backend.agent.asyncio.sleep", new_callable=AsyncMock
            ),
        ):
            await session._monitor_process(mock_proc)

            mock_start.assert_awaited_once()
            assert session._restart_attempts == 0

    async def test_monitor_gives_up_after_max_retries(self):
        from klangk_backend import model

        session = _make_session("ws-giveup")
        _agents["ws-giveup"] = session
        session._restart_attempts = 2  # already at limit

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.wait = AsyncMock()
        mock_proc.stderr = asyncio.StreamReader()
        mock_proc.stderr.feed_eof()
        session._proc = mock_proc

        with (
            patch.object(
                model,
                "add_chat_message",
                new_callable=AsyncMock,
                return_value={"id": "m1", "message": "msg"},
            ),
            patch(
                "klangk_backend.agent._get_workspace_session",
                return_value=MagicMock(),
            ),
            patch.object(
                session,
                "_ensure_started",
                new_callable=AsyncMock,
            ) as mock_start,
        ):
            await session._monitor_process(mock_proc)

            mock_start.assert_not_awaited()
            assert session._restart_attempts == 3
            assert session._gave_up is True

    async def test_gave_up_blocks_ensure_started(self):
        session = _make_session("ws-gaveup")
        session._gave_up = True

        with pytest.raises(AgentError, match="gave up"):
            await session._ensure_started()

    async def test_gave_up_reset_on_container_change(self):
        session = _make_session("ws-gaveup2")
        session._gave_up = True
        session._last_container_id = "old-cid"
        # Simulate container change — _resolve_container_id sees new ID.
        with patch(
            "klangk_backend.agent.container.registry.get_state",
            return_value=_FakeContainerState("new-cid"),
        ):
            cid = session._resolve_container_id()
        assert cid == "new-cid"
        assert session._gave_up is False
        assert session._restart_attempts == 0

    async def test_monitor_logs_stderr(self, caplog):
        from klangk_backend import model
        import logging

        session = _make_session("ws-stderr")
        _agents["ws-stderr"] = session
        session._restart_attempts = 2  # will give up

        mock_proc = AsyncMock()
        mock_proc.returncode = 255
        mock_proc.wait = AsyncMock()
        mock_proc.stderr = asyncio.StreamReader()
        mock_proc.stderr.feed_data(b"Error: container not running\n")
        mock_proc.stderr.feed_eof()
        session._proc = mock_proc

        with (
            patch.object(
                model,
                "add_chat_message",
                new_callable=AsyncMock,
                return_value={"id": "m1", "message": "msg"},
            ),
            patch(
                "klangk_backend.agent._get_workspace_session",
                return_value=MagicMock(),
            ),
            caplog.at_level(logging.WARNING, logger="klangk_backend.agent"),
        ):
            await session._monitor_process(mock_proc)

        assert any(
            "container not running" in r.message for r in caplog.records
        )

    async def test_monitor_stderr_read_error(self):
        """Monitor handles stderr read errors gracefully."""
        from klangk_backend import model

        session = _make_session("ws-stderr-err")
        _agents["ws-stderr-err"] = session
        session._restart_attempts = 2

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.wait = AsyncMock()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(side_effect=OSError("broken pipe"))
        session._proc = mock_proc

        with (
            patch.object(
                model,
                "add_chat_message",
                new_callable=AsyncMock,
                return_value={"id": "m1", "message": "msg"},
            ),
            patch(
                "klangk_backend.agent._get_workspace_session",
                return_value=MagicMock(),
            ),
        ):
            await session._monitor_process(mock_proc)
            assert session._gave_up is True

    async def test_monitor_restart_failure_logged(self):
        from klangk_backend import model

        session = _make_session("ws-fail")
        _agents["ws-fail"] = session

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.wait = AsyncMock()
        mock_proc.stderr = asyncio.StreamReader()
        mock_proc.stderr.feed_eof()
        session._proc = mock_proc

        with (
            patch.object(
                model,
                "add_chat_message",
                new_callable=AsyncMock,
                return_value={"id": "m1", "message": "msg"},
            ),
            patch(
                "klangk_backend.agent._get_workspace_session",
                return_value=MagicMock(),
            ),
            patch.object(
                session,
                "_ensure_started",
                new_callable=AsyncMock,
                side_effect=RuntimeError("startup failed"),
            ),
            patch(
                "klangk_backend.agent.asyncio.sleep", new_callable=AsyncMock
            ),
        ):
            await session._monitor_process(mock_proc)
            # Should not raise, just log

    async def test_broadcast_reconnect(self):
        from klangk_backend import model
        from klangk_backend.agent import _broadcast_agent_reconnect

        with (
            patch.object(
                model,
                "get_workspace_by_id",
                new_callable=AsyncMock,
                return_value={"id": "ws-rc"},
            ),
            patch.object(
                model,
                "add_chat_message",
                new_callable=AsyncMock,
                return_value={"id": "m1", "message": "reconnected"},
            ) as mock_chat,
            patch(
                "klangk_backend.agent._get_workspace_session"
            ) as mock_get_session,
        ):
            mock_session = MagicMock()
            mock_get_session.return_value = mock_session

            await _broadcast_agent_reconnect("ws-rc")

            mock_chat.assert_awaited_once()
            assert "reconnected" in mock_chat.call_args[0][3]
            assert mock_session.broadcast.call_count == 2

    async def test_broadcast_reconnect_no_workspace(self):
        from klangk_backend.agent import _broadcast_agent_reconnect

        await _broadcast_agent_reconnect("")

    async def test_broadcast_reconnect_deleted_workspace(self):
        from klangk_backend.agent import _broadcast_agent_reconnect

        # Should not raise when workspace has been deleted
        await _broadcast_agent_reconnect("deleted-ws-id")

    async def test_monitor_skips_restart_if_already_restarted(self):
        from klangk_backend import model

        session = _make_session("ws-skip")
        _agents["ws-skip"] = session

        dead_proc = AsyncMock()
        dead_proc.returncode = 1
        dead_proc.wait = AsyncMock()
        dead_proc.stderr = asyncio.StreamReader()
        dead_proc.stderr.feed_eof()
        session._proc = dead_proc

        async def fake_sleep(_):
            # Simulate something else restarting the proc during sleep
            session._proc = AsyncMock()

        with (
            patch.object(
                model,
                "add_chat_message",
                new_callable=AsyncMock,
                return_value={"id": "m1", "message": "msg"},
            ),
            patch(
                "klangk_backend.agent._get_workspace_session",
                return_value=MagicMock(),
            ),
            patch(
                "klangk_backend.agent.asyncio.sleep",
                side_effect=fake_sleep,
            ),
            patch.object(
                session,
                "_ensure_started",
                new_callable=AsyncMock,
            ) as mock_start,
        ):
            await session._monitor_process(dead_proc)
            mock_start.assert_not_awaited()


class TestSendAbort:
    def test_sends_abort_json_to_stdin(self):
        session = _make_session()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.is_closing.return_value = False
        proc.stdin.write = MagicMock()

        session._send_abort(proc)

        proc.stdin.write.assert_called_once()
        written = proc.stdin.write.call_args[0][0]
        parsed = json.loads(written.decode().strip())
        assert parsed == {"type": "abort"}

    def test_send_abort_no_stdin(self):
        session = _make_session()
        proc = MagicMock()
        proc.stdin = None
        # Should not raise
        session._send_abort(proc)

    def test_send_abort_stdin_closing(self):
        session = _make_session()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.is_closing.return_value = True
        # Should not raise or write
        session._send_abort(proc)
        proc.stdin.write.assert_not_called()

    def test_send_abort_write_oserror(self):
        session = _make_session()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.is_closing.return_value = False
        proc.stdin.write.side_effect = OSError("broken")
        # Should not raise
        session._send_abort(proc)
