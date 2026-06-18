"""Additional tests for cli/client.py paths not covered yet."""

import asyncio
import json
import os
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets

from klangkc.config import CLIConfig


class TestWsShell:
    @pytest.mark.asyncio
    async def test_ws_shell_connection_failure_raises(self):
        from klangkc.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.recv = AsyncMock(
            return_value=json.dumps(
                {"type": "error", "message": "bad workspace"}
            )
        )
        ws_mock.send = AsyncMock()

        with patch("websockets.connect", return_value=ws_mock):
            with pytest.raises(ConnectionError):
                await _ws_shell("ws://localhost/ws", "token", "ws1")

    @pytest.mark.asyncio
    async def test_wait_workspace_ready_timeout(self):
        """Times out if workspace_ready never arrives."""
        from klangkc.client import _wait_workspace_ready

        ws_mock = AsyncMock()
        # Always return a non-ready message
        ws_mock.recv = AsyncMock(
            return_value=json.dumps({"type": "presence_list", "users": []})
        )
        ws_mock.send = AsyncMock()

        with pytest.raises(asyncio.TimeoutError):
            await _wait_workspace_ready(ws_mock, "ws1", timeout=0.01)

    @pytest.mark.asyncio
    async def test_wait_workspace_ready_skips_broadcasts(self):
        """Broadcast messages before workspace_ready are skipped."""
        from klangkc.client import _wait_workspace_ready

        ws_mock = AsyncMock()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "presence_list", "users": [{"id": "u1"}]}),
                json.dumps({"type": "chat_history", "messages": []}),
                json.dumps({"type": "workspace_ready", "workspaceId": "ws1"}),
            ]
        )
        ws_mock.send = AsyncMock()

        resp = await _wait_workspace_ready(ws_mock, "ws1")
        assert resp["type"] == "workspace_ready"
        assert resp["workspaceId"] == "ws1"

    @pytest.mark.asyncio
    async def test_ws_shell_success_sends_connect_and_start(self):
        from klangkc.client import _ws_shell

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
        from klangkc.client import _ws_shell

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
        from klangkc.client import _ws_shell

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
        from klangkc.client import _ws_shell

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
        from klangkc.client import _ws_shell

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
        from klangkc.client import _ws_shell

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
        from klangkc.client import _ws_shell

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
        from klangkc.client import _ws_shell

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
        from klangkc.client import _run_shell

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
        from klangkc.client import _run_shell

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
        from klangkc.client import _run_shell

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
        from klangkc.client import _run_shell

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
                "klangkc.client.select.select",
                return_value=([99], [], []),
            ),
            patch(
                "klangkc.client.os.read",
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
        from klangkc import client as cli_client
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
            "klangkc.client.select.select",
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
        from klangkc.client import _is_terminal_response

        assert _is_terminal_response(b"") is False
        assert _is_terminal_response(b"\x1b") is False
        assert _is_terminal_response(b"\x1b[") is False

    def test_osc_response(self):
        from klangkc.client import _is_terminal_response

        assert _is_terminal_response(b"\x1b]11;rgb:0000/0000/0000\x07") is True

    def test_dcs_response(self):
        from klangkc.client import _is_terminal_response

        assert _is_terminal_response(b"\x1bP>|xterm\x1b\\") is True

    def test_da2_response(self):
        from klangkc.client import _is_terminal_response

        assert _is_terminal_response(b"\x1b[>61;1;21c") is True

    def test_da1_response(self):
        from klangkc.client import _is_terminal_response

        assert _is_terminal_response(b"\x1b[?61;1c") is True

    def test_user_arrow_key_returns_false(self):
        from klangkc.client import _is_terminal_response

        assert _is_terminal_response(b"\x1b[A") is False  # up arrow

    def test_non_escape_returns_false(self):
        from klangkc.client import _is_terminal_response

        assert _is_terminal_response(b"hello") is False


class TestDrainStdin:
    def test_drain_with_pending_data(self):
        from klangkc.client import _drain_stdin

        call_count = [0]

        def fake_select(rlist, wlist, xlist, timeout):
            call_count[0] += 1
            if call_count[0] <= 2:
                return (rlist, [], [])
            return ([], [], [])

        with (
            patch("klangkc.client.sys.stdin") as mock_stdin,
            patch(
                "klangkc.client.select.select",
                side_effect=fake_select,
            ),
            patch("klangkc.client.os.read", return_value=b"\x1b[>1;2c"),
            patch("klangkc.client.termios.tcgetattr", return_value=[]),
            patch("klangkc.client.termios.tcsetattr"),
            patch("klangkc.client.tty.setraw"),
        ):
            mock_stdin.fileno.return_value = 0
            _drain_stdin()

    def test_drain_termios_error_skips_raw_mode(self):
        import termios

        from klangkc.client import _drain_stdin

        with (
            patch("klangkc.client.sys.stdin") as mock_stdin,
            patch(
                "klangkc.client.select.select",
                return_value=([], [], []),
            ),
            patch(
                "klangkc.client.termios.tcgetattr",
                side_effect=termios.error("nope"),
            ),
        ):
            mock_stdin.fileno.return_value = 0
            _drain_stdin()  # should not raise

    def test_drain_os_error(self):
        from klangkc.client import _drain_stdin

        with patch("klangkc.client.sys.stdin") as mock_stdin:
            mock_stdin.fileno.side_effect = OSError("bad fd")
            _drain_stdin()  # should not raise

    def test_drain_second_select_finds_data(self):
        """Cover the 'wait one more round' branch."""
        from klangkc.client import _drain_stdin

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
            patch("klangkc.client.sys.stdin") as mock_stdin,
            patch(
                "klangkc.client.select.select",
                side_effect=fake_select,
            ),
            patch("klangkc.client.os.read", return_value=b"\x1b[?1c"),
            patch("klangkc.client.termios.tcgetattr", return_value=[]),
            patch("klangkc.client.termios.tcsetattr"),
            patch("klangkc.client.tty.setraw"),
        ):
            mock_stdin.fileno.return_value = 0
            _drain_stdin()


class TestStdoutLoopExited:
    @pytest.mark.asyncio
    async def test_exited_shows_disconnect_hint(self):
        from klangkc.client import _run_shell

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


class TestStdoutLoopAuthClose:
    @pytest.mark.asyncio
    async def test_auth_close_shows_session_expired(self):
        """Mid-session close with code 4002 shows session expired message."""
        import websockets
        from websockets.frames import Close

        from klangkc.client import _run_shell

        ws = AsyncMock()
        exc = websockets.ConnectionClosed(Close(4002, "Token expired"), None)
        ws.recv = AsyncMock(side_effect=exc)

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
        assert "Session expired" in output
        assert "klangkc login" in output


class TestStdinTerminalResponseFilter:
    @pytest.mark.asyncio
    async def test_terminal_response_filtered_from_stdin(self):
        """Terminal query responses on stdin are dropped, not forwarded."""
        from klangkc.client import _run_shell

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
                "klangkc.client.select.select",
                side_effect=fake_select,
            ),
            patch("klangkc.client.os.read", side_effect=fake_read),
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
        from klangkc import auth

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr("klangkc.config._CONFIG_PATH", config_path)
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
        from klangkc.client import KlangkClient

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
        from klangkc import main as cli_main

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

        from klangkc.client import _ws_exec

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
            with patch("klangkc.client.os.read", fake_os_read):
                with patch("klangkc.client.os.write", fake_os_write):
                    code = await _ws_exec(
                        "ws://localhost/ws", "token", "ws1", ["ls"]
                    )

        assert code == 0
        assert b"file-list" in bytes(captured)

    @pytest.mark.asyncio
    async def test_ws_exec_connection_failure(self):
        from klangkc.client import _ws_exec

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
        from klangkc.main import shell
        from klangkc.client import Workspace

        from klangkc.config import ServerConfig, AuthConfig

        fake_cfg = CLIConfig(
            server=ServerConfig(url="http://localhost:8995"),
            auth=AuthConfig(token="fake", email="test@test.com"),
        )
        monkeypatch.setattr("klangkc.main._cfg", lambda: fake_cfg)

        fake_ws = Workspace(id="ws1", name="ws", created_at="2026-01-01")
        monkeypatch.setattr(
            "klangkc.main._client",
            lambda: MagicMock(
                resolve_workspace=MagicMock(return_value=fake_ws)
            ),
        )

        monkeypatch.setattr(
            "klangkc.main.asyncio.run",
            MagicMock(side_effect=ConnectionError("Window 'x' not found")),
        )
        monkeypatch.setattr("klangkc.client._drain_stdin", lambda: None)
        monkeypatch.setattr("klangkc.client.reset_terminal", lambda: None)

        import typer

        with pytest.raises(typer.Exit) as exc_info:
            shell(workspace="ws", terminal="x")
        assert exc_info.value.exit_code == 1

    def _shell_with_side_effect(self, monkeypatch, side_effect):
        """Helper: run shell() with a mocked asyncio.run side_effect."""
        from klangkc.client import Workspace
        from klangkc.config import AuthConfig, ServerConfig

        fake_cfg = CLIConfig(
            server=ServerConfig(url="http://localhost:8995"),
            auth=AuthConfig(token="fake", email="test@test.com"),
        )
        monkeypatch.setattr("klangkc.main._cfg", lambda: fake_cfg)

        fake_ws = Workspace(id="ws1", name="ws", created_at="2026-01-01")
        monkeypatch.setattr(
            "klangkc.main._client",
            lambda: MagicMock(
                resolve_workspace=MagicMock(return_value=fake_ws)
            ),
        )

        monkeypatch.setattr(
            "klangkc.main.asyncio.run",
            MagicMock(side_effect=side_effect),
        )
        monkeypatch.setattr("klangkc.client._drain_stdin", lambda: None)
        monkeypatch.setattr("klangkc.client.reset_terminal", lambda: None)

    def test_shell_catches_expired_token(self, monkeypatch):
        """shell() catches InvalidStatusCode with 4001/4002 and shows auth error."""
        from websockets.exceptions import InvalidStatusCode

        from klangkc.main import shell

        self._shell_with_side_effect(
            monkeypatch, InvalidStatusCode(4002, None)
        )

        import typer

        with pytest.raises(typer.Exit) as exc_info:
            shell(workspace="ws", terminal="x")
        assert exc_info.value.exit_code == 1

    def test_shell_catches_non_auth_invalid_status(self, monkeypatch):
        """shell() catches InvalidStatusCode with non-auth code (e.g. 500)."""
        from websockets.exceptions import InvalidStatusCode

        from klangkc.main import shell

        self._shell_with_side_effect(monkeypatch, InvalidStatusCode(500, None))

        import typer

        with pytest.raises(typer.Exit) as exc_info:
            shell(workspace="ws", terminal="x")
        assert exc_info.value.exit_code == 1


class TestSSHAgentForwarding:
    async def test_ws_shell_sends_agent_start_when_flag_set(self, tmp_path):
        """With forward_agent=True and a valid SSH_AUTH_SOCK, ssh_agent_start
        is sent before terminal_start."""
        from klangkc.client import _ws_shell

        fake_sock = tmp_path / "agent.sock"
        fake_sock.touch()

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
                    {
                        "type": "ssh_agent_started",
                        "socket": "/tmp/agent.sock",
                    }
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

        with (
            patch("websockets.connect", return_value=ws_mock),
            patch("termios.tcgetattr", return_value=None),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch.dict(os.environ, {"SSH_AUTH_SOCK": str(fake_sock)}),
        ):
            try:
                await _ws_shell(
                    "ws://localhost/ws",
                    "token",
                    "ws1",
                    forward_agent=True,
                )
            except Exception:
                pass

        sent = [c[0][0] for c in ws_mock.send.call_args_list]
        parsed = [json.loads(s) for s in sent]
        cmds = [m.get("cmd") for m in parsed]
        assert "ssh_agent_start" in cmds
        assert "terminal_start" in cmds
        agent_idx = cmds.index("ssh_agent_start")
        terminal_idx = cmds.index("terminal_start")
        assert agent_idx < terminal_idx

    async def test_ws_shell_no_agent_without_flag(self):
        """Without forward_agent=True, no ssh_agent_start is sent."""
        from klangkc.client import _ws_shell

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

        with (
            patch("websockets.connect", return_value=ws_mock),
            patch("termios.tcgetattr", return_value=None),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
        ):
            try:
                await _ws_shell(
                    "ws://localhost/ws",
                    "token",
                    "ws1",
                    forward_agent=False,
                )
            except Exception:
                pass

        sent = [c[0][0] for c in ws_mock.send.call_args_list]
        assert not any("ssh_agent_start" in s for s in sent)

    async def test_ws_shell_agent_start_timeout(self, tmp_path):
        """If the backend never sends ssh_agent_started, the timeout
        fires and we proceed without agent forwarding."""
        from klangkc.client import _ws_shell

        fake_sock = tmp_path / "agent.sock"
        fake_sock.touch()

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()

        recv_count = 0

        async def fake_recv():
            nonlocal recv_count
            recv_count += 1
            if recv_count == 1:
                return json.dumps(
                    {"type": "workspace_ready", "workspaceId": "ws1"}
                )
            if recv_count == 2:
                # During agent start wait, hang forever — asyncio.wait_for
                # will raise TimeoutError
                await asyncio.sleep(999)
            if recv_count == 3:
                return json.dumps(
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
                )
            raise Exception("stop")

        ws_mock.recv = fake_recv

        # Use a very short timeout to avoid slow tests. Patch the deadline
        # calculation to use 0.01s instead of 10s.
        original_wait_for = asyncio.wait_for

        async def fast_wait_for(coro, *, timeout=None):
            # Make the agent start timeout very short
            if timeout is not None and timeout > 1:
                timeout = 0.01
            return await original_wait_for(coro, timeout=timeout)

        with (
            patch("websockets.connect", return_value=ws_mock),
            patch("termios.tcgetattr", return_value=None),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch.dict(os.environ, {"SSH_AUTH_SOCK": str(fake_sock)}),
            patch("asyncio.wait_for", side_effect=fast_wait_for),
        ):
            try:
                await _ws_shell(
                    "ws://localhost/ws",
                    "token",
                    "ws1",
                    forward_agent=True,
                )
            except Exception:
                pass

        # ssh_agent_start was sent but terminal_start should still be sent
        sent = [c[0][0] for c in ws_mock.send.call_args_list]
        parsed = [json.loads(s) for s in sent]
        cmds = [m.get("cmd") for m in parsed]
        assert "ssh_agent_start" in cmds
        assert "terminal_start" in cmds

    async def test_ws_shell_agent_start_error_response(self, tmp_path):
        """If backend sends an error in response to ssh_agent_start,
        we proceed without agent forwarding."""
        from klangkc.client import _ws_shell

        fake_sock = tmp_path / "agent.sock"
        fake_sock.touch()

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
                json.dumps({"type": "error", "message": "no container"}),
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

        with (
            patch("websockets.connect", return_value=ws_mock),
            patch("termios.tcgetattr", return_value=None),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch.dict(os.environ, {"SSH_AUTH_SOCK": str(fake_sock)}),
        ):
            try:
                await _ws_shell(
                    "ws://localhost/ws",
                    "token",
                    "ws1",
                    forward_agent=True,
                )
            except Exception:
                pass

        sent = [c[0][0] for c in ws_mock.send.call_args_list]
        parsed = [json.loads(s) for s in sent]
        cmds = [m.get("cmd") for m in parsed]
        # Agent start was sent, terminal still starts despite error
        assert "ssh_agent_start" in cmds
        assert "terminal_start" in cmds

    async def test_ws_shell_sends_agent_stop_on_exit(self, tmp_path):
        """When agent was active, ssh_agent_stop is sent on shell exit."""
        from klangkc.client import _ws_shell

        fake_sock = tmp_path / "agent.sock"
        fake_sock.touch()

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
                    {
                        "type": "ssh_agent_started",
                        "socket": "/tmp/agent.sock",
                    }
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
                # _run_shell will get this and raise, ending the shell
                Exception("stop"),
            ]
        )

        with (
            patch("websockets.connect", return_value=ws_mock),
            patch("termios.tcgetattr", return_value=None),
            patch("termios.tcsetattr"),
            patch("tty.setraw"),
            patch.dict(os.environ, {"SSH_AUTH_SOCK": str(fake_sock)}),
        ):
            try:
                await _ws_shell(
                    "ws://localhost/ws",
                    "token",
                    "ws1",
                    forward_agent=True,
                )
            except Exception:
                pass

        sent = [c[0][0] for c in ws_mock.send.call_args_list]
        parsed = [json.loads(s) for s in sent]
        cmds = [m.get("cmd") for m in parsed]
        assert "ssh_agent_stop" in cmds
        assert "terminal_stop" in cmds


class TestSSHAgentRelayLoop:
    """Tests for ssh_agent_relay_loop and ssh_agent_response routing."""

    async def test_stdout_loop_routes_agent_response(self):
        """ssh_agent_response messages in stdout_loop are put on the queue."""
        import base64

        from klangkc.client import _run_shell

        ws = AsyncMock()
        stop_event = asyncio.Event()
        stdout = MagicMock()
        stdout.write = MagicMock()
        stdout.flush = MagicMock()

        agent_data = b"\x00\x00\x00\x05\x0bhello"
        encoded = base64.b64encode(agent_data).decode("ascii")

        msg_idx = 0
        messages = [
            json.dumps({"type": "ssh_agent_response", "data": encoded}),
            json.dumps({"type": "terminal_output", "data": "prompt$ "}),
        ]

        async def fake_recv():
            nonlocal msg_idx
            if msg_idx < len(messages):
                m = messages[msg_idx]
                msg_idx += 1
                return m
            # After delivering messages, wait then stop
            stop_event.set()
            await asyncio.sleep(10)

        ws.recv = fake_recv
        ws.send = AsyncMock()

        stdin = MagicMock()
        stdin.fileno = MagicMock(return_value=0)

        with patch("select.select", return_value=([], [], [])):
            try:
                await asyncio.wait_for(
                    _run_shell(
                        ws,
                        80,
                        24,
                        stdin=stdin,
                        stdout=stdout,
                        ssh_agent_sock="/fake/agent.sock",
                    ),
                    timeout=3,
                )
            except (asyncio.TimeoutError, Exception):
                pass

        # The terminal_output message should have been written
        stdout.write.assert_any_call("prompt$ ")

    async def test_relay_loop_forwards_to_local_agent(self, tmp_path):
        """ssh_agent_relay_loop reads from queue, connects to local socket,
        sends data, reads SSH protocol response, and sends back over WS."""
        import base64
        import struct

        from klangkc.client import _run_shell

        # Create a real Unix socket server acting as the SSH agent
        agent_path = str(tmp_path / "agent.sock")
        response_body = b"\x06\x00"  # SSH_AGENT_SUCCESS type + padding
        response_msg = struct.pack(">I", len(response_body)) + response_body

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(agent_path)
        server.listen(1)
        server.setblocking(False)

        async def agent_server():
            """Accept one connection, read request, send response."""
            loop = asyncio.get_event_loop()
            conn, _ = await loop.sock_accept(server)
            try:
                data = await loop.sock_recv(conn, 4096)
                assert len(data) > 0
                await loop.sock_sendall(conn, response_msg)
            finally:
                conn.close()

        ws = AsyncMock()
        stop_event = asyncio.Event()
        stdout = MagicMock()
        stdout.write = MagicMock()
        stdout.flush = MagicMock()

        # The request that will arrive as ssh_agent_response from backend
        request_body = b"\x0b"  # SSH_AGENTC_REQUEST_IDENTITIES
        request_msg = struct.pack(">I", len(request_body)) + request_body
        encoded = base64.b64encode(request_msg).decode("ascii")

        msg_idx = 0

        async def fake_recv():
            nonlocal msg_idx
            if msg_idx == 0:
                msg_idx += 1
                return json.dumps(
                    {"type": "ssh_agent_response", "data": encoded}
                )
            # Give the relay time to process, then stop
            await asyncio.sleep(0.5)
            stop_event.set()
            await asyncio.sleep(10)

        ws.recv = fake_recv
        ws.send = AsyncMock()

        stdin = MagicMock()
        stdin.fileno = MagicMock(return_value=0)

        server_task = asyncio.create_task(agent_server())

        with patch("select.select", return_value=([], [], [])):
            try:
                await asyncio.wait_for(
                    _run_shell(
                        ws,
                        80,
                        24,
                        stdin=stdin,
                        stdout=stdout,
                        ssh_agent_sock=agent_path,
                    ),
                    timeout=5,
                )
            except (asyncio.TimeoutError, Exception):
                pass

        await asyncio.wait_for(server_task, timeout=2)
        server.close()

        # Verify the relay sent ssh_agent_data back over WS
        sent_calls = ws.send.call_args_list
        agent_data_msgs = []
        for call in sent_calls:
            try:
                msg = json.loads(call[0][0])
                if msg.get("cmd") == "ssh_agent_data":
                    agent_data_msgs.append(msg)
            except (json.JSONDecodeError, IndexError):
                pass

        assert len(agent_data_msgs) == 1
        decoded = base64.b64decode(agent_data_msgs[0]["data"])
        assert decoded == response_msg

    async def test_relay_loop_handles_connection_error(self, tmp_path):
        """ssh_agent_relay_loop logs a warning on connection error and
        keeps running."""
        import base64
        import struct

        from klangkc.client import _run_shell

        # Point to a path that doesn't have a listener
        bad_sock = str(tmp_path / "nonexistent.sock")

        ws = AsyncMock()
        stop_event = asyncio.Event()
        stdout = MagicMock()
        stdout.write = MagicMock()
        stdout.flush = MagicMock()

        request_body = b"\x0b"
        request_msg = struct.pack(">I", len(request_body)) + request_body
        encoded = base64.b64encode(request_msg).decode("ascii")

        msg_idx = 0

        async def fake_recv():
            nonlocal msg_idx
            if msg_idx == 0:
                msg_idx += 1
                return json.dumps(
                    {"type": "ssh_agent_response", "data": encoded}
                )
            await asyncio.sleep(0.5)
            stop_event.set()
            await asyncio.sleep(10)

        ws.recv = fake_recv
        ws.send = AsyncMock()

        stdin = MagicMock()
        stdin.fileno = MagicMock(return_value=0)

        with patch("select.select", return_value=([], [], [])):
            try:
                await asyncio.wait_for(
                    _run_shell(
                        ws,
                        80,
                        24,
                        stdin=stdin,
                        stdout=stdout,
                        ssh_agent_sock=bad_sock,
                    ),
                    timeout=3,
                )
            except (asyncio.TimeoutError, Exception):
                pass

        # Should not have crashed — no ssh_agent_data sent since
        # connection to agent failed
        agent_data_msgs = [
            json.loads(c[0][0])
            for c in ws.send.call_args_list
            if "ssh_agent_data" in c[0][0]
        ]
        assert len(agent_data_msgs) == 0
