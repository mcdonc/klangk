"""Additional tests for cli/client.py paths not covered yet."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets

from klangk_backend.cli.config import CLIConfig


class TestWsShell:
    @pytest.mark.asyncio
    async def test_ws_shell_connection_failure_raises(self):
        from klangk_backend.cli.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "not_workspace_ready", "data": "oops"}
            )
        )
        ws_mock.send = AsyncMock()

        with patch("websockets.connect", return_value=ws_mock):
            with pytest.raises(ConnectionError):
                await _ws_shell("ws://localhost/ws", "token", "ws1")

    @pytest.mark.asyncio
    async def test_ws_shell_success_sends_connect_and_start(self):
        from klangk_backend.cli.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "workspace_ready", "workspaceId": "ws1"}),
                json.dumps(
                    {"type": "terminal_output", "data": "\x1b[2J\x1b[H"}
                ),
                json.dumps(
                    {
                        "type": "terminal_windows",
                        "windows": [
                            {
                                "id": "@0",
                                "index": 0,
                                "name": "bash",
                                "active": True,
                            },
                        ],
                    }
                ),
                Exception("stop"),
            ]
        )

        with patch("websockets.connect", return_value=ws_mock):
            with patch("termios.tcgetattr", return_value=None):
                with patch("termios.tcsetattr"):
                    with patch("tty.setraw"):
                        try:
                            await _ws_shell(
                                "ws://localhost/ws", "token", "ws1"
                            )
                        except Exception:
                            pass

        sent = [c[0][0] for c in ws_mock.send.call_args_list]
        assert any("workspace_connect" in s for s in sent)
        assert any("terminal_start" in s for s in sent)
        # No commandOverride by default
        start_msgs = [json.loads(s) for s in sent if "terminal_start" in s]
        assert "commandOverride" not in start_msgs[0]

    @pytest.mark.asyncio
    async def test_ws_shell_sends_command_override(self):
        from klangk_backend.cli.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "workspace_ready", "workspaceId": "ws1"}),
                json.dumps(
                    {"type": "terminal_output", "data": "\x1b[2J\x1b[H"}
                ),
                json.dumps(
                    {
                        "type": "terminal_windows",
                        "windows": [
                            {
                                "id": "@0",
                                "index": 0,
                                "name": "bash",
                                "active": True,
                            },
                        ],
                    }
                ),
                Exception("stop"),
            ]
        )

        with patch("websockets.connect", return_value=ws_mock):
            with patch("termios.tcgetattr", return_value=None):
                with patch("termios.tcsetattr"):
                    with patch("tty.setraw"):
                        try:
                            await _ws_shell(
                                "ws://localhost/ws",
                                "token",
                                "ws1",
                                command_override="bash",
                            )
                        except Exception:
                            pass

        sent = [c[0][0] for c in ws_mock.send.call_args_list]
        start_msgs = [json.loads(s) for s in sent if "terminal_start" in s]
        assert start_msgs[0]["commandOverride"] == "bash"

    @pytest.mark.asyncio
    async def test_ws_shell_collects_windows_and_shared(self):
        """Drain loop collects terminal_windows and shared_terminals."""
        from klangk_backend.cli.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "workspace_ready"}),
                json.dumps(
                    {
                        "type": "shared_terminals",
                        "terminals": [
                            {
                                "user_id": "u1",
                                "handle": "alice",
                                "window_name": "dev",
                                "window_id": "@1",
                            },
                        ],
                    }
                ),
                json.dumps({"type": "terminal_output", "data": "$ "}),
                json.dumps(
                    {
                        "type": "terminal_windows",
                        "windows": [
                            {
                                "id": "@0",
                                "index": 0,
                                "name": "1",
                                "active": True,
                            },
                        ],
                    }
                ),
                Exception("stop"),
            ]
        )

        with patch("websockets.connect", return_value=ws_mock):
            with patch("termios.tcgetattr", return_value=None):
                with patch("termios.tcsetattr"):
                    with patch("tty.setraw"):
                        try:
                            await _ws_shell(
                                "ws://localhost/ws", "token", "ws1"
                            )
                        except Exception:
                            pass

    @pytest.mark.asyncio
    async def test_ws_shell_select_own_window(self):
        """window= selects an own window by name."""
        from klangk_backend.cli.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "workspace_ready"}),
                json.dumps(
                    {
                        "type": "terminal_windows",
                        "windows": [
                            {
                                "id": "@0",
                                "index": 0,
                                "name": "1",
                                "active": True,
                            },
                            {
                                "id": "@1",
                                "index": 1,
                                "name": "build",
                                "active": False,
                            },
                        ],
                    }
                ),
                json.dumps({"type": "terminal_output", "data": "$ "}),
                Exception("stop"),
            ]
        )

        with patch("websockets.connect", return_value=ws_mock):
            with patch("termios.tcgetattr", return_value=None):
                with patch("termios.tcsetattr"):
                    with patch("tty.setraw"):
                        try:
                            await _ws_shell(
                                "ws://localhost/ws",
                                "token",
                                "ws1",
                                window="build",
                            )
                        except Exception:
                            pass

        sent = [json.loads(c[0][0]) for c in ws_mock.send.call_args_list]
        select_msgs = [
            s for s in sent if s.get("cmd") == "terminal_select_window"
        ]
        assert len(select_msgs) == 1
        assert select_msgs[0]["index"] == 1

    @pytest.mark.asyncio
    async def test_ws_shell_select_own_window_not_found(self):
        from klangk_backend.cli.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "workspace_ready"}),
                json.dumps(
                    {
                        "type": "terminal_windows",
                        "windows": [
                            {
                                "id": "@0",
                                "index": 0,
                                "name": "1",
                                "active": True,
                            },
                        ],
                    }
                ),
                json.dumps({"type": "terminal_output", "data": "$ "}),
            ]
        )

        with patch("websockets.connect", return_value=ws_mock):
            with pytest.raises(ConnectionError, match="not found"):
                await _ws_shell(
                    "ws://localhost/ws",
                    "token",
                    "ws1",
                    window="nonexistent",
                )

    @pytest.mark.asyncio
    async def test_ws_shell_join_shared_terminal(self):
        """window=handle:name joins a shared terminal."""
        from klangk_backend.cli.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "workspace_ready"}),
                json.dumps(
                    {
                        "type": "shared_terminals",
                        "terminals": [
                            {
                                "user_id": "u1",
                                "handle": "alice",
                                "window_name": "dev",
                                "window_id": "@1",
                            },
                        ],
                    }
                ),
                json.dumps({"type": "terminal_output", "data": "$ "}),
                json.dumps(
                    {
                        "type": "terminal_windows",
                        "windows": [
                            {
                                "id": "@0",
                                "index": 0,
                                "name": "bash",
                                "active": True,
                            },
                        ],
                    }
                ),
                # After join_shared_terminal is sent:
                json.dumps({"type": "terminal_output", "data": "joining..."}),
                json.dumps(
                    {"type": "terminal_started", "shared_window": "dev"}
                ),
                Exception("stop"),
            ]
        )

        with patch("websockets.connect", return_value=ws_mock):
            with patch("termios.tcgetattr", return_value=None):
                with patch("termios.tcsetattr"):
                    with patch("tty.setraw"):
                        try:
                            await _ws_shell(
                                "ws://localhost/ws",
                                "token",
                                "ws1",
                                window="alice:dev",
                            )
                        except Exception:
                            pass

        sent = [json.loads(c[0][0]) for c in ws_mock.send.call_args_list]
        join_msgs = [s for s in sent if s.get("cmd") == "join_shared_terminal"]
        assert len(join_msgs) == 1
        assert join_msgs[0]["user_id"] == "u1"
        assert join_msgs[0]["window_id"] == "@1"

    @pytest.mark.asyncio
    async def test_ws_shell_join_shared_not_found(self):
        from klangk_backend.cli.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "workspace_ready"}),
                json.dumps({"type": "shared_terminals", "terminals": []}),
                json.dumps({"type": "terminal_output", "data": "$ "}),
                json.dumps(
                    {
                        "type": "terminal_windows",
                        "windows": [
                            {
                                "id": "@0",
                                "index": 0,
                                "name": "bash",
                                "active": True,
                            },
                        ],
                    }
                ),
            ]
        )

        with patch("websockets.connect", return_value=ws_mock):
            with pytest.raises(ConnectionError, match="not found"):
                await _ws_shell(
                    "ws://localhost/ws",
                    "token",
                    "ws1",
                    window="alice:dev",
                )

    @pytest.mark.asyncio
    async def test_ws_shell_join_shared_error(self):
        from klangk_backend.cli.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "workspace_ready"}),
                json.dumps(
                    {
                        "type": "shared_terminals",
                        "terminals": [
                            {
                                "user_id": "u1",
                                "handle": "alice",
                                "window_name": "dev",
                                "window_id": "@1",
                            },
                        ],
                    }
                ),
                json.dumps({"type": "terminal_output", "data": "$ "}),
                json.dumps(
                    {
                        "type": "terminal_windows",
                        "windows": [
                            {
                                "id": "@0",
                                "index": 0,
                                "name": "bash",
                                "active": True,
                            },
                        ],
                    }
                ),
                json.dumps({"type": "error", "message": "Permission denied"}),
            ]
        )

        with patch("websockets.connect", return_value=ws_mock):
            with pytest.raises(ConnectionError, match="Permission denied"):
                await _ws_shell(
                    "ws://localhost/ws",
                    "token",
                    "ws1",
                    window="alice:dev",
                )


class TestRunShell:
    @pytest.mark.asyncio
    async def test_stdout_loop_bytes_message(self):
        from klangk_backend.cli.client import _run_shell

        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                b'{"type": "terminal_output", "data": "raw-bytes"}',
                json.dumps(
                    {
                        "type": "event",
                        "event": {
                            "type": "CUSTOM",
                            "name": "container_stopped",
                            "value": {},
                        },
                    }
                ),
            ]
        )

        captured = []

        class CaptureWriter:
            def write(self, data):
                captured.append(data)

            def flush(self):
                pass

        fake_stdout = CaptureWriter()
        task = asyncio.create_task(_run_shell(ws, 80, 24, stdout=fake_stdout))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert "raw-bytes" in "".join(captured)

    @pytest.mark.asyncio
    async def test_stdout_loop_ignores_unknown_event(self):
        from klangk_backend.cli.client import _run_shell

        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "type": "event",
                        "event": {"type": "RUN_STARTED", "value": {}},
                    }
                ),
                json.dumps(
                    {
                        "type": "event",
                        "event": {
                            "type": "CUSTOM",
                            "name": "container_stopped",
                            "value": {},
                        },
                    }
                ),
            ]
        )
        task = asyncio.create_task(_run_shell(ws, 80, 24))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_stdout_loop_connection_closed(self):
        from klangk_backend.cli.client import _run_shell

        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=websockets.ConnectionClosed(None, None)
        )
        task = asyncio.create_task(_run_shell(ws, 80, 24))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Should not raise — ConnectionClosed is caught cleanly

    @pytest.mark.asyncio
    async def test_stdin_loop_broken_pipe(self):
        from klangk_backend.cli.client import _run_shell

        ws = AsyncMock()
        ws.send = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "type": "event",
                        "event": {
                            "type": "CUSTOM",
                            "name": "container_stopped",
                            "value": {},
                        },
                    }
                )
            ]
        )

        fake_stdin = MagicMock()
        fake_stdin.fileno = MagicMock(return_value=99)
        # stdin_loop reads via os.read(fd, ...), not stdin.read(); patch it
        # to raise BrokenPipeError so the OSError handler runs instead of
        # blocking on the real stdin fd.
        with (
            patch(
                "klangk_backend.cli.client.select.select",
                return_value=([99], [], []),
            ),
            patch(
                "klangk_backend.cli.client.os.read",
                side_effect=BrokenPipeError,
            ),
        ):
            task = asyncio.create_task(
                _run_shell(ws, 80, 24, stdin=fake_stdin)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_resize_loop_sends_on_size_change(self, monkeypatch):
        """resize_loop detects size change and sends terminal_resize via _send_resize."""
        from klangk_backend.cli import client as cli_client
        from io import BytesIO

        fake_buf = BytesIO(b"")
        fake_buf.fileno = lambda: 0

        ws = AsyncMock()
        ws.send = AsyncMock()

        # stdout_loop recv blocks long enough for resize_loop to fire.
        async def slow_recv():
            await asyncio.sleep(5.0)
            return json.dumps(
                {
                    "type": "event",
                    "event": {
                        "type": "CUSTOM",
                        "name": "container_stopped",
                        "value": {},
                    },
                }
            )

        ws.recv = slow_recv

        call_idx = [0]

        def cycling_size():
            call_idx[0] += 1
            return (120, 40) if call_idx[0] > 1 else (80, 24)

        monkeypatch.setattr(cli_client, "_get_terminal_size", cycling_size)

        # select returns empty so stdin_loop keeps looping without reading EOF
        with patch(
            "klangk_backend.cli.client.select.select",
            return_value=([], [], []),
        ):
            task = asyncio.create_task(
                cli_client._run_shell(ws, 80, 24, stdin=fake_buf)
            )
            await asyncio.sleep(2.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        resize_msgs = [
            c[0][0]
            for c in ws.send.call_args_list
            if "terminal_resize" in c[0][0]
        ]
        assert len(resize_msgs) >= 1, (
            f"Expected at least 1 resize send, got {ws.send.call_count} total "
            f"sends: {[c[0][0] for c in ws.send.call_args_list]}"
        )


class TestIsTerminalResponse:
    def test_short_data_returns_false(self):
        from klangk_backend.cli.client import _is_terminal_response

        assert _is_terminal_response(b"") is False
        assert _is_terminal_response(b"\x1b") is False
        assert _is_terminal_response(b"\x1b[") is False

    def test_osc_response(self):
        from klangk_backend.cli.client import _is_terminal_response

        assert _is_terminal_response(b"\x1b]11;rgb:0000/0000/0000\x07") is True

    def test_dcs_response(self):
        from klangk_backend.cli.client import _is_terminal_response

        assert _is_terminal_response(b"\x1bP>|xterm\x1b\\") is True

    def test_da2_response(self):
        from klangk_backend.cli.client import _is_terminal_response

        assert _is_terminal_response(b"\x1b[>61;1;21c") is True

    def test_da1_response(self):
        from klangk_backend.cli.client import _is_terminal_response

        assert _is_terminal_response(b"\x1b[?61;1c") is True

    def test_user_arrow_key_returns_false(self):
        from klangk_backend.cli.client import _is_terminal_response

        assert _is_terminal_response(b"\x1b[A") is False  # up arrow

    def test_non_escape_returns_false(self):
        from klangk_backend.cli.client import _is_terminal_response

        assert _is_terminal_response(b"hello") is False


class TestDrainStdin:
    def test_drain_with_pending_data(self):
        from klangk_backend.cli.client import _drain_stdin

        call_count = [0]

        def fake_select(rlist, wlist, xlist, timeout):
            call_count[0] += 1
            if call_count[0] <= 2:
                return (rlist, [], [])
            return ([], [], [])

        with (
            patch("klangk_backend.cli.client.sys.stdin") as mock_stdin,
            patch(
                "klangk_backend.cli.client.select.select",
                side_effect=fake_select,
            ),
            patch(
                "klangk_backend.cli.client.os.read", return_value=b"\x1b[>1;2c"
            ),
            patch(
                "klangk_backend.cli.client.termios.tcgetattr", return_value=[]
            ),
            patch("klangk_backend.cli.client.termios.tcsetattr"),
            patch("klangk_backend.cli.client.tty.setraw"),
        ):
            mock_stdin.fileno.return_value = 0
            _drain_stdin()

    def test_drain_termios_error_skips_raw_mode(self):
        import termios

        from klangk_backend.cli.client import _drain_stdin

        with (
            patch("klangk_backend.cli.client.sys.stdin") as mock_stdin,
            patch(
                "klangk_backend.cli.client.select.select",
                return_value=([], [], []),
            ),
            patch(
                "klangk_backend.cli.client.termios.tcgetattr",
                side_effect=termios.error("nope"),
            ),
        ):
            mock_stdin.fileno.return_value = 0
            _drain_stdin()  # should not raise

    def test_drain_os_error(self):
        from klangk_backend.cli.client import _drain_stdin

        with patch("klangk_backend.cli.client.sys.stdin") as mock_stdin:
            mock_stdin.fileno.side_effect = OSError("bad fd")
            _drain_stdin()  # should not raise

    def test_drain_second_select_finds_data(self):
        """Cover the 'wait one more round' branch."""
        from klangk_backend.cli.client import _drain_stdin

        call_count = [0]

        def fake_select(rlist, wlist, xlist, timeout):
            call_count[0] += 1
            # First select: no data (50ms)
            if call_count[0] == 1:
                return ([], [], [])
            # Second select (100ms fallback): data arrives
            if call_count[0] == 2:
                return (rlist, [], [])
            # Third: no more
            return ([], [], [])

        with (
            patch("klangk_backend.cli.client.sys.stdin") as mock_stdin,
            patch(
                "klangk_backend.cli.client.select.select",
                side_effect=fake_select,
            ),
            patch(
                "klangk_backend.cli.client.os.read", return_value=b"\x1b[?1c"
            ),
            patch(
                "klangk_backend.cli.client.termios.tcgetattr", return_value=[]
            ),
            patch("klangk_backend.cli.client.termios.tcsetattr"),
            patch("klangk_backend.cli.client.tty.setraw"),
        ):
            mock_stdin.fileno.return_value = 0
            _drain_stdin()


class TestStdoutLoopExited:
    @pytest.mark.asyncio
    async def test_exited_shows_disconnect_hint(self):
        from klangk_backend.cli.client import _run_shell

        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps(
                    {"type": "terminal_output", "data": "bash [exited]\r\n"}
                ),
                json.dumps(
                    {
                        "type": "event",
                        "event": {
                            "type": "CUSTOM",
                            "name": "container_stopped",
                            "value": {},
                        },
                    }
                ),
            ]
        )

        captured = []

        class CaptureWriter:
            def write(self, data):
                captured.append(data)

            def flush(self):
                pass

        task = asyncio.create_task(
            _run_shell(ws, 80, 24, stdout=CaptureWriter())
        )
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        output = "".join(captured)
        assert "[exited]" in output
        assert "Enter, then ~." in output


class TestStdinTerminalResponseFilter:
    @pytest.mark.asyncio
    async def test_terminal_response_filtered_from_stdin(self):
        """Terminal query responses on stdin are dropped, not forwarded."""
        from klangk_backend.cli.client import _run_shell

        ws = AsyncMock()
        ws.send = AsyncMock()

        read_count = [0]
        # DA2 response on first read, then EOF
        responses = [b"\x1b", b"[>61;1;21c", b""]

        def fake_read(fd, n):
            val = responses[min(read_count[0], len(responses) - 1)]
            read_count[0] += 1
            return val

        select_count = [0]

        def fake_select(rlist, wlist, xlist, timeout):
            select_count[0] += 1
            if select_count[0] <= 4:
                return (rlist, [], [])
            return ([], [], [])

        ws.recv = AsyncMock(
            side_effect=[
                json.dumps(
                    {
                        "type": "event",
                        "event": {
                            "type": "CUSTOM",
                            "name": "container_stopped",
                            "value": {},
                        },
                    }
                )
            ]
        )

        fake_stdin = MagicMock()
        fake_stdin.fileno.return_value = 99

        with (
            patch(
                "klangk_backend.cli.client.select.select",
                side_effect=fake_select,
            ),
            patch("klangk_backend.cli.client.os.read", side_effect=fake_read),
        ):
            task = asyncio.create_task(
                _run_shell(ws, 80, 24, stdin=fake_stdin)
            )
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # No terminal_input should have been sent
        sent = [
            json.loads(c[0][0])
            for c in ws.send.call_args_list
            if isinstance(c[0][0], str) and "terminal_input" in c[0][0]
        ]
        assert len(sent) == 0


class TestAuthLines:
    def test_logout_network_error_propagates(self, tmp_path, monkeypatch):
        from klangk_backend.cli import auth

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig()
        cfg.server.url = "http://localhost:8995"
        cfg.auth.token = "tok"
        cfg.auth.email = "x@y.com"
        cfg.save()

        with patch("httpx.post", side_effect=OSError("no route")):
            with pytest.raises(OSError):
                auth.logout()

        # Token was cleared and saved before the server call.
        cfg2 = CLIConfig.load()
        assert cfg2.auth.token is None


class TestClientLines:
    def test_delete_workspace_500_exit(self):
        from klangk_backend.cli.client import KlangkClient

        cfg = CLIConfig()
        cfg.auth.token = "tok"
        client = KlangkClient(cfg)

        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = [
            {"id": "ws1", "name": "ws1", "created_at": "2025-01-01T00:00:00Z"}
        ]
        del_resp = MagicMock()
        del_resp.status_code = 500
        del_resp.text = "server error"
        del_resp.is_success = False

        with patch.object(client, "get", return_value=list_resp):
            with patch.object(client, "delete", return_value=del_resp):
                with pytest.raises(SystemExit):
                    client.delete_workspace("ws1")


class TestImagesCommand:
    def test_images_lists_allowed(self, monkeypatch):
        from klangk_backend.cli import main as cli_main

        mock_client = MagicMock()
        mock_client.list_images.return_value = {
            "default": "klangk",
            "allowed": ["klangk", "klangk-custom"],
        }
        monkeypatch.setattr(cli_main, "_client", lambda: mock_client)
        monkeypatch.setattr(cli_main, "_cfg", lambda: CLIConfig())
        cfg = CLIConfig()
        cfg.auth.token = "tok"
        monkeypatch.setattr(cli_main, "_cfg", lambda: cfg)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(cli_main.app, ["images"])
        assert result.exit_code == 0
        assert "klangk" in result.output
        assert "klangk-custom" in result.output


class TestWsExec:
    @pytest.mark.asyncio
    async def test_ws_exec_success(self):
        import base64

        from klangk_backend.cli.client import _ws_exec

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()

        output_chunk = base64.b64encode(b"file-list").decode()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "workspace_ready", "workspaceId": "ws1"}),
                json.dumps({"type": "exec_output", "data": output_chunk}),
                json.dumps({"type": "exec_exit", "code": 0}),
            ]
        )

        captured = bytearray()

        def fake_os_read(fd, n):
            return b""  # EOF immediately

        def fake_os_write(fd, data):
            captured.extend(data)
            return len(data)

        with patch("websockets.connect", return_value=ws_mock):
            with patch("klangk_backend.cli.client.os.read", fake_os_read):
                with patch(
                    "klangk_backend.cli.client.os.write", fake_os_write
                ):
                    code = await _ws_exec(
                        "ws://localhost/ws", "token", "ws1", ["ls"]
                    )

        assert code == 0
        assert b"file-list" in bytes(captured)

    @pytest.mark.asyncio
    async def test_ws_exec_connection_failure(self):
        from klangk_backend.cli.client import _ws_exec

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.recv = AsyncMock(
            return_value=json.dumps({"type": "error", "message": "bad"})
        )
        ws_mock.send = AsyncMock()

        with patch("websockets.connect", return_value=ws_mock):
            with pytest.raises(ConnectionError):
                await _ws_exec("ws://localhost/ws", "token", "ws1", ["ls"])


class TestShellConnectionError:
    def test_shell_catches_connection_error(self, monkeypatch):
        """shell() catches ConnectionError from _ws_shell and exits cleanly."""
        from klangk_backend.cli.main import shell
        from klangk_backend.cli.client import Workspace

        from klangk_backend.cli.config import ServerConfig, AuthConfig

        fake_cfg = CLIConfig(
            server=ServerConfig(url="http://localhost:8995"),
            auth=AuthConfig(token="fake", email="test@test.com"),
        )
        monkeypatch.setattr("klangk_backend.cli.main._cfg", lambda: fake_cfg)

        fake_ws = Workspace(id="ws1", name="ws", created_at="2026-01-01")
        monkeypatch.setattr(
            "klangk_backend.cli.main._client",
            lambda: MagicMock(
                resolve_workspace=MagicMock(return_value=fake_ws)
            ),
        )

        monkeypatch.setattr(
            "klangk_backend.cli.main.asyncio.run",
            MagicMock(side_effect=ConnectionError("Window 'x' not found")),
        )
        monkeypatch.setattr(
            "klangk_backend.cli.client._drain_stdin", lambda: None
        )
        monkeypatch.setattr(
            "klangk_backend.cli.client.reset_terminal", lambda: None
        )

        import typer

        with pytest.raises(typer.Exit) as exc_info:
            shell(workspace="ws", terminal="x")
        assert exc_info.value.exit_code == 1
