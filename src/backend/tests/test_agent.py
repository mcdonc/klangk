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
    ensure_agent_home,
    get_session,
    is_disabled,
    is_running,
    stop_all_sessions,
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
            (model.AGENT_USER_ID, "clanker@example.com", "clanker"),
        )
    model.clear_agent_cache()


def _make_session(workspace_id="ws-id"):
    """Create an AgentSession with home setup already done."""
    s = AgentSession(workspace_id)
    s._home_ready = True
    s._last_container_id = "cid"
    return s


_ACK = {"type": "response", "command": "prompt", "success": True}


class TestAgentDisabled:
    """The agent can be turned off entirely by an admin (#1138)."""

    async def test_is_disabled_defaults_false(self, monkeypatch):
        monkeypatch.delenv("KLANGK_AGENT_DISABLED", raising=False)
        assert is_disabled() is False

    async def test_is_disabled_true_when_set(self, monkeypatch):
        monkeypatch.setenv("KLANGK_AGENT_DISABLED", "1")
        assert is_disabled() is True

    async def test_is_disabled_truthy_variants(self, monkeypatch):
        for val in ("1", "true", "YES", "True"):
            monkeypatch.setenv("KLANGK_AGENT_DISABLED", val)
            assert is_disabled() is True, val

    async def test_is_disabled_falsy_variants(self, monkeypatch):
        for val in ("0", "false", "no", ""):
            monkeypatch.setenv("KLANGK_AGENT_DISABLED", val)
            assert is_disabled() is False, val

    async def test_ensure_started_refuses_when_disabled(self, monkeypatch):
        """The subprocess is never spawned when disabled."""
        monkeypatch.setenv("KLANGK_AGENT_DISABLED", "1")
        session = _make_session("ws-disabled")
        with patch("asyncio.create_subprocess_exec") as mock_spawn:
            with pytest.raises(AgentError, match="disabled"):
                await session._ensure_started()
            mock_spawn.assert_not_called()

    async def test_send_prompt_raises_when_disabled(self, monkeypatch):
        """send_prompt surfaces the disabled state, never spawns."""
        monkeypatch.setenv("KLANGK_AGENT_DISABLED", "1")
        session = _make_session("ws-disabled")
        with patch("asyncio.create_subprocess_exec") as mock_spawn:
            with pytest.raises(AgentError, match="disabled"):
                await session.send_prompt("hello")
            mock_spawn.assert_not_called()


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
        proc.kill = MagicMock()
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
        # Timeout tears down the process so the next prompt starts
        # from a clean stream (#894).
        assert session._proc is None
        proc.kill.assert_called_once()

    async def test_second_prompt_after_timeout_uses_fresh_process(self):
        """Regression for #894: a timeout must tear down the subprocess
        so the next prompt starts from a clean stdout stream, rather
        than resyncing past leftover turn-1 events that could corrupt
        turn 2's response.
        """
        # Turn 1: ack + agent_start, then silence -> response timeout.
        proc1 = AsyncMock()
        proc1.returncode = None
        proc1.stdin = AsyncMock()
        proc1.kill = MagicMock()
        proc1.stdout = asyncio.StreamReader()
        proc1.stdout.feed_data(
            json.dumps(_ACK).encode()
            + b"\n"
            + json.dumps({"type": "agent_start"}).encode()
            + b"\n"
        )
        proc1.stderr = asyncio.StreamReader()

        # Turn 2: a clean stream carrying the real answer.
        proc2 = AsyncMock()
        proc2.returncode = None
        proc2.stdin = AsyncMock()
        proc2.stdout = asyncio.StreamReader()
        turn2_events = [
            _ACK,
            {"type": "agent_start"},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "second",
                },
            },
            {"type": "agent_end"},
        ]
        proc2.stdout.feed_data(
            ("\n".join(json.dumps(e) for e in turn2_events) + "\n").encode()
        )
        proc2.stdout.feed_eof()
        proc2.stderr = asyncio.StreamReader()

        with patch(
            "asyncio.create_subprocess_exec", side_effect=[proc1, proc2]
        ) as mock_exec:
            session = _make_session()
            r1 = await session.send_prompt("turn1", timeout=0.1)
            # If proc1 were (buggily) reused, EOF'ing its pipe makes
            # turn 2 fail fast instead of hanging.  With the fix proc1
            # is already gone, so this is a no-op for turn 2.
            proc1.stdout.feed_eof()
            r2 = await session.send_prompt("turn2")

        assert "timed out" in r1
        assert r2 == "second"
        assert mock_exec.call_count == 2
        proc1.kill.assert_called_once()

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

    async def _run_successful_prompt(self, session):
        """Drive send_prompt to a successful completion."""
        events = [
            _ACK,
            {"type": "agent_start"},
            {
                "type": "message_update",
                "assistantMessageEvent": {
                    "type": "text_delta",
                    "delta": "ok",
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
            result = await session.send_prompt("test")
        assert result == "ok"

    async def test_send_prompt_resets_restart_state(self):
        """A successful prompt clears the restart counter so transient past
        deaths can't accumulate and permanently disable the agent."""
        session = _make_session("ws-reset")
        # Two prior restarts: one more death (without a recovery) would
        # brick the agent. _gave_up stays False — once it's True,
        # _ensure_started raises before a prompt can ever run.
        session._restart_attempts = 2

        await self._run_successful_prompt(session)

        assert session._restart_attempts == 0
        assert session._gave_up is False

    async def test_recovery_then_death_still_restarts(self):
        """3 deaths -> successful recovery -> a 4th death must still restart,
        not permanently give up (the bug from #895)."""
        from klangk_backend import model

        session = _make_session("ws-recover")
        _agents["ws-recover"] = session
        # Simulate 3 prior deaths: counter near the limit (one more death
        # would brick the agent without the reset).
        session._restart_attempts = 2

        # Recovery: a prompt completes successfully, clearing the counter.
        await self._run_successful_prompt(session)
        assert session._restart_attempts == 0

        # A 4th death arrives. The monitor must restart (counter goes 0->1,
        # gave_up stays False) rather than permanently giving up.
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
            assert session._restart_attempts == 1
            assert session._gave_up is False

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


class TestEnsureAgentHome:
    """Direct tests for the eager-provisioning function (#1157).

    Called from eager_start_workspace at container bring-up (every exec
    process inherits $KLANGK_AGENT_HOME) and again from chat-start, which
    caches the result per AgentSession (see TestEnsureHome).
    """

    async def test_provisions_home_and_runs_setup(self, tmp_path):
        from klangk_backend import model, workspaces

        fake_ws = {"user_id": "owner1"}
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))
        mock_proc.returncode = 0

        with (
            patch.object(model, "get_workspace_by_id", return_value=fake_ws),
            patch.object(workspaces, "home_path", return_value=fake_home),
            patch.object(
                workspaces,
                "ensure_home_symlink",
                return_value=("/home/clanker", True),
            ) as mock_symlink,
            patch.object(
                workspaces, "populate_home_skel", new_callable=AsyncMock
            ) as mock_skel,
            patch(
                "asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
        ):
            result = await ensure_agent_home("ws1", "cid")

        assert result == "/home/clanker"
        mock_symlink.assert_called_once()
        # Skeleton files only on first creation (created=True).
        mock_skel.assert_awaited_once_with(
            "cid", "00000000-0000-0000-0000-000000000001"
        )
        # klangk-setup-pi --force re-writes Pi config each time.  It's
        # invoked by its full install path (bare-name resolution isn't
        # reliable across podman/OCI runtimes) -- matching the existing
        # klangk-setup-home pattern.
        argv = mock_exec.call_args.args
        assert "/opt/klangk/bin/klangk-setup-pi" in argv
        assert "--force" in argv

    async def test_setup_failure_does_not_abort_but_logs(self, tmp_path):
        """klangk-setup-pi failure logs a warning but doesn't raise.

        Provisioning is best-effort: the workspace stays usable, and
        the lazy chat-start path retries on first mention (#1162).
        The return value (container home) is unaffected by the rc.
        """
        from klangk_backend import agent, model, workspaces

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(
            return_value=(b"setup out", b"setup err")
        )
        mock_proc.returncode = 127  # script missing / crashed

        with (
            patch.object(
                model, "get_workspace_by_id", return_value={"user_id": "o"}
            ),
            patch.object(workspaces, "home_path", return_value=fake_home),
            patch.object(
                workspaces,
                "ensure_home_symlink",
                return_value=("/home/clanker", True),
            ),
            patch.object(
                workspaces, "populate_home_skel", new_callable=AsyncMock
            ),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            # Must NOT raise -- provisioning is best-effort.
            result = await agent.ensure_agent_home("ws1", "cid")

        assert result == "/home/clanker"

    async def test_skips_skel_when_home_already_exists(self, tmp_path):
        from klangk_backend import model, workspaces

        # created=False -> home dir already existed, no skel needed.
        with (
            patch.object(
                model, "get_workspace_by_id", return_value={"user_id": "o"}
            ),
            patch.object(workspaces, "home_path", return_value=tmp_path),
            patch.object(
                workspaces,
                "ensure_home_symlink",
                return_value=("/home/clanker", False),
            ),
            patch.object(
                workspaces, "populate_home_skel", new_callable=AsyncMock
            ) as mock_skel,
            patch(
                "asyncio.create_subprocess_exec",
                return_value=AsyncMock(
                    communicate=AsyncMock(return_value=(b"", b""))
                ),
            ),
        ):
            result = await ensure_agent_home("ws1", "cid")

        assert result == "/home/clanker"
        mock_skel.assert_not_awaited()

    async def test_raises_when_workspace_missing(self):
        from klangk_backend import model
        from klangk_backend.agent import AgentSetupError

        with patch.object(model, "get_workspace_by_id", return_value=None):
            with pytest.raises(AgentSetupError, match="not found in database"):
                await ensure_agent_home("ws-gone", "cid")


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
        mock_proc.returncode = 0

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
                return_value=("/home/clanker", True),
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

        assert result == "/home/clanker"
        assert session._home_ready is True
        mock_symlink.assert_called_once()
        mock_skel.assert_awaited_once_with(
            "cid", "00000000-0000-0000-0000-000000000001"
        )

    async def test_ensure_home_cached(self):
        session = AgentSession("ws-id")
        session._home_ready = True
        result = await session._ensure_home("cid")
        assert result == "/home/clanker"

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


class TestStopAllSessions:
    async def test_stops_every_session(self):
        for ws_id in ("ws-a", "ws-b"):
            session = await get_session(ws_id)
            session._proc = AsyncMock()
            session._proc.returncode = None
            session._proc.kill = MagicMock()
            session._proc.wait = AsyncMock()

        assert len(_agents) == 2
        await stop_all_sessions()
        assert _agents == {}

    async def test_empty_is_noop(self):
        await stop_all_sessions()
        assert _agents == {}


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


class TestSpawnSerialization:
    """``_ensure_started``'s check-then-spawn must be atomic (#1189).

    ``_monitor_process`` (auto-restart) and ``send_prompt`` (lazy start)
    call ``_ensure_started`` concurrently on the same singleton session
    while ``self._proc`` is None.  ``_ensure_home`` does slow podman work
    between the live-process check and the spawn, so without the spawn
    lock both callers observe None and each spawn a ``pi --mode rpc``
    subprocess — the loser is orphaned.  The lock plus the re-check must
    guarantee exactly one spawn and that both callers share it.
    """

    async def test_concurrent_starts_spawn_once_and_share_proc(self):
        session = _make_session("ws-race")

        # Force the first caller to yield between its fast-path check
        # and the spawn (where ``_ensure_home`` sits in real code),
        # handing control to a second caller that has also observed
        # ``self._proc is None``.  On unfixed code both then spawn.
        async def slow_ensure_home(container_id):
            await asyncio.sleep(0.05)
            return "/home/clanker"

        session._ensure_home = slow_ensure_home  # type: ignore[assignment]

        spawns: list[tuple] = []

        async def fake_exec(*args, **kwargs):
            spawns.append(args)
            proc = AsyncMock()
            proc.returncode = None
            proc.stdin = AsyncMock()
            proc.stdout = asyncio.StreamReader()
            proc.stderr = asyncio.StreamReader()
            return proc

        with (
            patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
            # Avoid starting real monitor tasks: this test only exercises
            # spawn serialization, not the monitor loop.
            patch("asyncio.create_task", new=MagicMock()),
        ):
            proc_a, proc_b = await asyncio.gather(
                session._ensure_started(),
                session._ensure_started(),
            )

        assert len(spawns) == 1, f"expected one spawn, got {len(spawns)}"
        # Both callers share the single spawned process (idempotent).
        assert proc_a is proc_b


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
            ) as mock_chat,
            patch(
                "klangk_backend.agent._get_workspace_session"
            ) as mock_get_session,
        ):
            mock_session = MagicMock()
            mock_get_session.return_value = mock_session

            await _broadcast_agent_disconnect("ws-mon")

            # Presence transitions must NOT be persisted to chat history
            # (they'd linger as stale "has disconnected" on the next visit).
            mock_chat.assert_not_awaited()
            # Still broadcast live to connected subscribers: agent_thinking,
            # the ephemeral chat_message, and presence_leave.
            assert mock_session.broadcast.call_count == 3
            chat_broadcast = mock_session.broadcast.call_args_list[1][0][0]
            assert chat_broadcast["type"] == "chat_message"
            assert "disconnected" in chat_broadcast["message"]
            assert chat_broadcast["message_type"] == model.MSG_SYSTEM

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
            assert session._restart_attempts == 1

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
            ) as mock_chat,
            patch(
                "klangk_backend.agent._get_workspace_session"
            ) as mock_get_session,
        ):
            mock_session = MagicMock()
            mock_get_session.return_value = mock_session

            await _broadcast_agent_reconnect("ws-rc")

            # Reconnect is ephemeral too — never persisted.
            mock_chat.assert_not_awaited()
            assert mock_session.broadcast.call_count == 2
            chat_broadcast = mock_session.broadcast.call_args_list[0][0][0]
            assert chat_broadcast["type"] == "chat_message"
            assert "reconnected" in chat_broadcast["message"]
            assert chat_broadcast["message_type"] == model.MSG_SYSTEM

    async def test_broadcast_reconnect_no_workspace(self):
        from klangk_backend.agent import _broadcast_agent_reconnect

        await _broadcast_agent_reconnect("")

    async def test_broadcast_reconnect_deleted_workspace(self):
        from klangk_backend.agent import _broadcast_agent_reconnect

        # Should not raise when workspace has been deleted
        await _broadcast_agent_reconnect("deleted-ws-id")

    async def test_monitor_skips_restart_if_container_gone(self, caplog):
        """Monitor does not restart when the container has been removed."""
        from klangk_backend import model
        import logging

        session = _make_session("ws-gone")
        _agents["ws-gone"] = session

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
            patch(
                "klangk_backend.agent.container.registry.get_state",
                return_value=None,
            ),
            patch(
                "klangk_backend.agent.asyncio.sleep", new_callable=AsyncMock
            ),
            patch.object(
                session,
                "_ensure_started",
                new_callable=AsyncMock,
            ) as mock_start,
            caplog.at_level(logging.INFO, logger="klangk_backend.agent"),
        ):
            await session._monitor_process(mock_proc)
            mock_start.assert_not_awaited()

        assert any("Container gone" in r.message for r in caplog.records)

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


class TestResetProcess:
    async def test_kills_and_clears_alive_proc(self):
        """_reset_process cancels the monitor, kills, and clears state."""
        session = _make_session()
        proc = MagicMock()
        proc.returncode = None
        monitor = MagicMock()
        session._proc = proc
        session._monitor_task = monitor

        await session._reset_process(proc)

        monitor.cancel.assert_called_once()
        proc.kill.assert_called_once()
        assert session._proc is None
        assert session._monitor_task is None

    async def test_handles_already_dead_proc(self):
        """_reset_process tolerates the process already being gone."""
        session = _make_session()
        proc = MagicMock()
        proc.returncode = None
        proc.kill.side_effect = ProcessLookupError()
        session._proc = proc

        await session._reset_process(proc)  # should not raise

        assert session._proc is None


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
