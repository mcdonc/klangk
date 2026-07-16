"""Tests for podman ExecSession: raw podman exec without PTY."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import types


from klangk_backend.podman import ExecSession, Podman
from _helpers import make_settings

_podman = Podman(types.SimpleNamespace(settings=make_settings({})))


def _mock_proc(stdout_data=b"", returncode=None):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdin = AsyncMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.close = MagicMock()

    stdout = AsyncMock()
    _chunks = [stdout_data] if stdout_data else []
    _idx = [0]

    async def _read(n):
        if _idx[0] < len(_chunks):
            chunk = _chunks[_idx[0]]
            _idx[0] += 1
            return chunk
        return b""

    stdout.read = _read
    proc.stdout = stdout
    proc.wait = AsyncMock()
    return proc


class TestExecSession:
    async def test_start_and_output(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"hello world")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["echo", "hello"])
        assert session.is_alive

        chunks = []
        async for data in session.output():
            chunks.append(data)
        assert b"hello world" in b"".join(chunks)

    async def test_start_default_is_raw_argv(self):
        """#1041: with no ``login`` flag the command is spliced into the
        podman exec argv verbatim -- no shell. This is what programmatic
        transports (rsync) need: their argv must round-trip untouched,
        and a ~/.profile that prints must not corrupt the binary stream.
        """
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ) as mock_exec:
            await session.start(["echo", "hello"])
        argv = mock_exec.call_args.args
        # raw: the command words are the tail of the argv, no wrapper.
        assert argv[-2:] == ("echo", "hello")
        assert "bash" not in argv
        assert "-lc" not in argv

    async def test_start_login_uses_login_shell_exec_at_idiom(self):
        """#1041: ``login=True`` runs the command under a login shell that
        sources ``~/.profile`` (so a PATH-only binary like an nvm-installed
        tool resolves) while preserving argv fidelity. It uses the
        standard wrapper idiom ``bash -lc 'exec "$@"'`` with the command
        as the trailing argv -- the same semantics as ``docker exec`` /
        ``kubectl exec``: argv is exec'd, not shell-parsed, so a compound
        command needs an explicit ``bash -c``. Each element survives as
        one word (no shlex/quoting games).
        """
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ) as mock_exec:
            await session.start(["openclaw", "--version"], login=True)
        argv = mock_exec.call_args.args
        # bash -lc 'exec "$@"' bash openclaw --version
        assert argv[-6:] == (
            "bash",
            "-lc",
            'exec "$@"',
            "bash",
            "openclaw",
            "--version",
        )

    async def test_start_login_preserves_argv_with_spaces_and_metachars(
        self,
    ):
        """The login path does NOT shell-join the command: every element is
        passed through ``exec "$@"`` verbatim, so an element containing
        spaces (e.g. a ``bash -c 'script with a redirect'`` invocation)
        survives as one word. This is what ``bash -c``-style commands
        and the e2e sync tests rely on, and what a naive space-join or
        ``shlex.join`` would each break in a different way.
        """
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"")
        cmd = ["bash", "-c", "echo remote-data > /home/work/out.txt"]
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ) as mock_exec:
            await session.start(cmd, login=True)
        argv = mock_exec.call_args.args
        # the command list is the verbatim tail after the idiom prefix
        prefix = ("bash", "-lc", 'exec "$@"', "bash")
        assert argv[-len(cmd) - len(prefix) :] == (*prefix, *cmd)
        # specifically, the script-with-spaces is one element, not split
        assert argv[-1] == "echo remote-data > /home/work/out.txt"

    async def test_write_sends_to_stdin(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["cat"])
        await session.write(b"input data")
        proc.stdin.write.assert_called_with(b"input data")

    async def test_close_stdin(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["cat"])
        await session.close_stdin()
        proc.stdin.close.assert_called_once()

    async def test_stop_terminates_process(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"", returncode=None)
        proc.wait = AsyncMock()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["sleep", "10"])
        await session.stop()
        proc.terminate.assert_called_once()
        assert session._proc is None

    async def test_stop_kills_on_timeout(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"", returncode=None)
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["sleep", "10"])
        await session.stop()
        proc.kill.assert_called_once()

    async def test_returncode(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"", returncode=42)
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["false"])
        assert session.returncode == 42

    async def test_returncode_none_when_no_proc(self):
        session = ExecSession("cid", _podman)
        assert session.returncode is None

    async def test_returncode_survives_stop(self):
        """returncode is still accessible after stop() nulls _proc."""
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"", returncode=7)
        proc.wait = AsyncMock()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["exit", "7"])
        await asyncio.sleep(0.1)
        await session.stop()
        assert session._proc is None
        assert session.returncode == 7

    async def test_is_alive_false_when_no_proc(self):
        session = ExecSession("cid", _podman)
        assert not session.is_alive

    async def test_write_noop_when_no_proc(self):
        session = ExecSession("cid", _podman)
        await session.write(b"data")  # should not raise

    async def test_close_stdin_noop_when_no_proc(self):
        session = ExecSession("cid", _podman)
        await session.close_stdin()  # should not raise

    async def test_stop_noop_when_no_proc(self):
        session = ExecSession("cid", _podman)
        await session.stop()  # should not raise

    async def test_read_stdout_handles_oserror(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"")
        proc.stdout.read = AsyncMock(side_effect=OSError("broken"))
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["cat"])
        # _read_stdout should have queued None
        data = await session._output_queue.get()
        assert data is None

    async def test_read_task_held_after_start(self):
        session = ExecSession("cid", _podman)
        assert session._read_task is None
        proc = _mock_proc(b"hello")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["echo", "hello"])
        assert session._read_task is not None
        assert isinstance(session._read_task, asyncio.Task)

    async def test_stop_cancels_read_task(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"", returncode=None)
        proc.wait = AsyncMock()
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["sleep", "10"])
        assert session._read_task is not None
        await session.stop()
        assert session._read_task is None

    async def test_read_stdout_reraises_cancelled_error(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"")
        blocked = asyncio.Event()

        async def _blocking_read(n):
            blocked.set()
            await asyncio.sleep(999)

        proc.stdout.read = _blocking_read
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["cat"])
        # Wait until the read task is blocked inside stdout.read
        await blocked.wait()
        session._read_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await session._read_task

    async def test_is_alive_false_when_read_task_done(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"output")
        # returncode stays None so is_alive would be True without
        # the read_task.done() check
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["echo", "output"])
        # Wait for the read task to finish (it reads one chunk then EOF)
        await session._read_task
        assert session._read_task.done()
        assert not session.is_alive

    async def test_sentinel_uses_put_nowait(self):
        """_read_stdout uses put_nowait for the sentinel."""
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["true"])
        await asyncio.sleep(0.1)
        data = await session._output_queue.get()
        assert data is None

    async def test_sentinel_dropped_when_queue_full(self):
        """When queue is full, sentinel is silently dropped (no deadlock)."""
        session = ExecSession("cid", _podman)
        session._running = True
        # Pre-fill the queue to capacity
        for _ in range(64):
            session._output_queue.put_nowait(b"data")
        assert session._output_queue.full()

        # Simulate _read_stdout finally block: put_nowait catches QueueFull
        try:
            session._output_queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

        # Queue still full, sentinel dropped, no hang
        assert session._output_queue.full()
        items = []
        while not session._output_queue.empty():
            items.append(session._output_queue.get_nowait())
        assert None not in items

    async def test_output_exits_when_running_cleared_without_sentinel(self):
        """output() exits via _running check when sentinel is dropped."""
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"data")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["echo", "data"])

        # Drain the queue
        await asyncio.sleep(0.1)
        while not session._output_queue.empty():
            session._output_queue.get_nowait()

        session._running = True

        async def _consume():
            collected = []
            async for data in session.output():
                collected.append(data)
            return collected

        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.05)
        session._running = False
        result = await asyncio.wait_for(task, timeout=3.0)
        assert result == []
        await session.stop()

    async def test_output_exits_when_read_task_done_without_sentinel(self):
        """When sentinel is dropped and _running is True, output() exits
        via _read_task.done() check after timeout."""
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"data")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            await session.start(["echo", "data"])

        # Wait for read task to finish
        await asyncio.sleep(0.1)
        assert session._read_task.done()

        # Drain the queue (data + sentinel)
        while not session._output_queue.empty():
            session._output_queue.get_nowait()

        # _running is still True — only _read_task.done() signals exit
        assert session._running

        async def _consume():
            collected = []
            async for data in session.output():
                collected.append(data)
            return collected

        result = await asyncio.wait_for(
            asyncio.create_task(_consume()), timeout=3.0
        )
        assert result == []
        await session.stop()

    async def test_default_work_dir(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ) as mock_exec:
            await session.start(["echo"])
        cmd = mock_exec.call_args[0]
        assert "-w" in cmd
        w_idx = cmd.index("-w")
        assert cmd[w_idx + 1] == "/home/work"

    async def test_user_home_sets_env_and_work_dir(self):
        session = ExecSession(
            "cid",
            _podman,
            env=["HOME=/home/alice"],
            work_dir="/home/alice",
        )
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ) as mock_exec:
            await session.start(["echo"])
        cmd = mock_exec.call_args[0]
        assert "HOME=/home/alice" in cmd
        w_idx = cmd.index("-w")
        assert cmd[w_idx + 1] == "/home/alice"

    async def test_no_home_env_without_user_home(self):
        session = ExecSession("cid", _podman)
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ) as mock_exec:
            await session.start(["echo"])
        cmd = mock_exec.call_args[0]
        assert not any(str(a).startswith("HOME=") for a in cmd)

    async def test_ssh_agent_socket_in_env(self):
        session = ExecSession(
            "cid",
            _podman,
            env=["HOME=/home/alice", "SSH_AUTH_SOCK=/tmp/agent.sock"],
            work_dir="/home/alice",
        )
        proc = _mock_proc(b"")
        with patch(
            "asyncio.create_subprocess_exec",
            return_value=proc,
        ) as mock_exec:
            await session.start(["echo"])
        cmd = mock_exec.call_args[0]
        assert "SSH_AUTH_SOCK=/tmp/agent.sock" in cmd
