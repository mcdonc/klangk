"""Additional tests for cli/client.py paths not covered yet."""

import asyncio
import io
import json
import os
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import websockets

from klangkc.config import CLIConfig, CLIState


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
        assert select_msgs[0]["window_id"] == "@1"

    @pytest.mark.asyncio
    async def test_ws_shell_creates_missing_own_window(self):
        """Missing window is auto-created via terminal_new_window."""
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
                # Response to terminal_new_window
                json.dumps(
                    {
                        "type": "terminal_windows",
                        "windows": [
                            {
                                "id": "@0",
                                "index": 0,
                                "name": "1",
                                "active": False,
                            },
                            {
                                "id": "@1",
                                "index": 1,
                                "name": "nonexistent",
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
                    window="nonexistent",
                )
            except Exception:
                pass

        sent = [json.loads(c[0][0]) for c in ws_mock.send.call_args_list]
        cmds = [m.get("cmd") for m in sent]
        assert "terminal_new_window" in cmds
        new_msg = next(
            m for m in sent if m.get("cmd") == "terminal_new_window"
        )
        assert new_msg["name"] == "nonexistent"
        assert "terminal_select_window" in cmds

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

        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangkc.config._STATE_PATH", state_path)
        state = CLIState()
        state.set_credentials("http://localhost:8995", "x@y.com", "tok")
        state.save()

        with patch("httpx.post", side_effect=OSError("no route")):
            with pytest.raises(OSError):
                auth.logout("http://localhost:8995")

        # Token was cleared and saved before the server call.
        state2 = CLIState.load()
        assert state2.get_token("http://localhost:8995") is None


class TestClientLines:
    def test_delete_workspace_500_raises(self):
        import httpx

        from klangkc.client import KlangkClient

        client = KlangkClient("http://test:8995", "tok")

        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = {
            "items": [
                {
                    "id": "ws1",
                    "name": "ws1",
                    "created_at": "2025-01-01T00:00:00Z",
                }
            ],
            "has_more": False,
            "next_offset": None,
        }
        del_resp = MagicMock()
        del_resp.status_code = 500
        del_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )

        with patch.object(client, "get", return_value=list_resp):
            with patch.object(client, "delete", return_value=del_resp):
                with pytest.raises(httpx.HTTPStatusError):
                    client.delete_workspace("ws1")

    def test_restart_workspace_calls_post(self):

        from klangkc.client import KlangkClient

        client = KlangkClient("http://test:8995", "tok")

        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = {
            "items": [
                {
                    "id": "ws1",
                    "name": "ws1",
                    "created_at": "2025-01-01T00:00:00Z",
                }
            ],
            "has_more": False,
            "next_offset": None,
        }
        restart_resp = MagicMock()
        restart_resp.status_code = 200

        with patch.object(client, "get", return_value=list_resp):
            with patch.object(
                client, "post", return_value=restart_resp
            ) as mock_post:
                client.restart_workspace("ws1")

        mock_post.assert_called_once_with("/api/v1/workspaces/ws1/restart")

    def test_list_workspaces_all_pages_traverses_pagination(self):
        from klangkc.client import KlangkClient

        client = KlangkClient("http://test:8995", "tok")

        page1 = MagicMock()
        page1.status_code = 200
        page1.json.return_value = {
            "items": [
                {
                    "id": "ws1",
                    "name": "ws1",
                    "created_at": "2025-01-01T00:00:00Z",
                }
            ],
            "has_more": True,
            "next_offset": 10,
        }
        page2 = MagicMock()
        page2.status_code = 200
        page2.json.return_value = {
            "items": [
                {
                    "id": "ws2",
                    "name": "ws2",
                    "created_at": "2025-01-02T00:00:00Z",
                }
            ],
            "has_more": False,
            "next_offset": None,
        }

        with patch.object(
            client, "get", side_effect=[page1, page2]
        ) as mock_get:
            workspaces = client.list_workspaces(all_pages=True)

        assert [w.id for w in workspaces] == ["ws1", "ws2"]
        # Second request must carry the next_offset from page 1.
        assert mock_get.call_count == 2
        assert mock_get.call_args_list[1].kwargs["params"]["offset"] == 10

    def test_list_workspaces_sort_order_q_forwarded(self):
        from klangkc.client import KlangkClient

        client = KlangkClient("http://test:8995", "tok")
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "items": [],
            "has_more": False,
            "next_offset": None,
        }
        with patch.object(client, "get", return_value=resp) as mock_get:
            client.list_workspaces(sort="name", order="asc", q="gamma")
        params = mock_get.call_args.kwargs["params"]
        assert params["sort"] == "name"
        assert params["order"] == "asc"
        assert params["q"] == "gamma"


class TestImagesCommand:
    def test_images_lists_allowed(self, monkeypatch):
        from klangkc import main as cli_main

        mock_client = MagicMock()
        mock_client.list_images.return_value = {
            "default": "klangk",
            "allowed": ["klangk", "klangk-custom"],
        }
        monkeypatch.setattr(cli_main, "_client", lambda: mock_client)
        state = CLIState()
        state.set_credentials("http://localhost:8995", "t@t", "tok")
        monkeypatch.setattr(cli_main, "_state", lambda: state)
        monkeypatch.setattr(cli_main, "_server_override", None)

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

        with patch("websockets.connect", return_value=ws_mock):
            from klangkc.client import _ws_exec_piped

            code, output = await _ws_exec_piped(
                "ws://localhost/ws",
                "token",
                "ws1",
                ["ls"],
            )

        assert code == 0
        assert "file-list" in output

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
    def _setup_shell_mocks(self, monkeypatch):
        """Set up common mocks for shell tests."""
        state = CLIState()
        state.set_credentials("http://localhost:8995", "test@test.com", "fake")
        monkeypatch.setattr("klangkc.main._state", lambda: state)
        monkeypatch.setattr("klangkc.main._server_override", None)
        monkeypatch.setattr("klangkc.main._cfg", lambda: CLIConfig())

    def test_shell_catches_connection_error(self, monkeypatch):
        """shell() catches ConnectionError from _ws_shell and exits cleanly."""
        from klangkc.main import shell
        from klangkc.client import Workspace

        self._setup_shell_mocks(monkeypatch)

        fake_ws = Workspace(id="ws1", name="ws", created_at="2026-01-01")
        monkeypatch.setattr(
            "klangkc.main._client",
            lambda: MagicMock(
                resolve_workspace=MagicMock(return_value=fake_ws)
            ),
        )

        monkeypatch.setattr(
            "klangkc.main.asyncio.run",
            MagicMock(side_effect=ConnectionError("Server error")),
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

        self._setup_shell_mocks(monkeypatch)

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
        """shell() catches InvalidStatus with 4001/4002 and shows auth error."""
        from websockets import InvalidStatus, Response

        from klangkc.main import shell

        self._shell_with_side_effect(
            monkeypatch,
            InvalidStatus(Response(4002, "Token expired", {}, b"")),
        )

        import typer

        with pytest.raises(typer.Exit) as exc_info:
            shell(workspace="ws", terminal="x")
        assert exc_info.value.exit_code == 1

    def test_shell_catches_non_auth_invalid_status(self, monkeypatch):
        """shell() catches InvalidStatus with non-auth code (e.g. 500)."""
        from websockets import InvalidStatus, Response

        from klangkc.main import shell

        self._shell_with_side_effect(
            monkeypatch,
            InvalidStatus(Response(500, "Internal Server Error", {}, b"")),
        )

        import typer

        with pytest.raises(typer.Exit) as exc_info:
            shell(workspace="ws", terminal="x")
        assert exc_info.value.exit_code == 1


class TestSSHAgentForwarding:
    @pytest.mark.filterwarnings("ignore::RuntimeWarning")
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

    @pytest.mark.filterwarnings("ignore::RuntimeWarning")
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
            # After delivering messages, close connection
            await asyncio.sleep(0.5)
            raise websockets.ConnectionClosed(None, None)

        ws.recv = fake_recv
        ws.send = AsyncMock()

        # Use a pipe for stdin; close write end so os.read returns EOF.
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        stdin = MagicMock()
        stdin.fileno = MagicMock(return_value=read_fd)

        try:
            await _run_shell(
                ws,
                80,
                24,
                stdin=stdin,
                stdout=stdout,
                ssh_agent_sock="/fake/agent.sock",
            )
        except (websockets.ConnectionClosed, Exception):
            pass
        finally:
            os.close(read_fd)

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

        import threading

        agent_done = threading.Event()

        def agent_server_thread():
            """Accept one connection, read request, send response."""
            conn, _ = server.accept()
            try:
                data = conn.recv(4096)
                assert len(data) > 0
                conn.sendall(response_msg)
            finally:
                conn.close()
                agent_done.set()

        agent_thread = threading.Thread(
            target=agent_server_thread, daemon=True
        )
        agent_thread.start()

        ws = AsyncMock()
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
            # Give the relay time to complete its blocking socket
            # operations, then close the connection.
            await asyncio.sleep(3)
            raise websockets.ConnectionClosed(None, None)

        ws.recv = fake_recv
        ws.send = AsyncMock()

        # Use a pipe for stdin; close write end so os.read returns EOF.
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        stdin = MagicMock()
        stdin.fileno = MagicMock(return_value=read_fd)

        try:
            await _run_shell(
                ws,
                80,
                24,
                stdin=stdin,
                stdout=stdout,
                ssh_agent_sock=agent_path,
            )
        except (websockets.ConnectionClosed, Exception):
            pass
        finally:
            os.close(read_fd)

        agent_done.wait(timeout=2)
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
            raise websockets.ConnectionClosed(None, None)

        ws.recv = fake_recv
        ws.send = AsyncMock()

        # Use a pipe for stdin; close write end so os.read returns EOF.
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        stdin = MagicMock()
        stdin.fileno = MagicMock(return_value=read_fd)

        try:
            await _run_shell(
                ws,
                80,
                24,
                stdin=stdin,
                stdout=stdout,
                ssh_agent_sock=bad_sock,
            )
        except (websockets.ConnectionClosed, Exception):
            pass
        finally:
            os.close(read_fd)

        # Should not have crashed — no ssh_agent_data sent since
        # connection to agent failed
        agent_data_msgs = [
            json.loads(c[0][0])
            for c in ws.send.call_args_list
            if "ssh_agent_data" in c[0][0]
        ]
        assert len(agent_data_msgs) == 0


class TestWsExecPipedWithInput:
    async def test_piped_sends_stdin_data(self):
        import base64

        from klangkc.client import _ws_exec_piped

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()

        output_chunk = base64.b64encode(b"echoed").decode()
        ws_mock.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "workspace_ready", "workspaceId": "ws1"}),
                json.dumps({"type": "exec_output", "data": output_chunk}),
                json.dumps({"type": "exec_exit", "code": 0}),
            ]
        )

        with patch("websockets.connect", return_value=ws_mock):
            code, output = await _ws_exec_piped(
                "ws://localhost/ws",
                "token",
                "ws1",
                ["cat"],
                stdin_data=b"input data",
            )

        assert code == 0
        assert "echoed" in output
        # Verify exec_input was sent with our data
        sent = [c[0][0] for c in ws_mock.send.call_args_list]
        input_msgs = [json.loads(s) for s in sent if "exec_input" in s]
        assert len(input_msgs) == 1
        decoded = base64.b64decode(input_msgs[0]["data"])
        assert decoded == b"input data"


class TestGetHandle:
    def test_returns_handle(self):
        from klangkc.client import KlangkClient

        client = KlangkClient("http://localhost:8995", "test-token")

        with patch.object(
            client,
            "get",
            return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value={"handle": "admin"}),
            ),
        ):
            assert client.get_handle() == "admin"


class TestExecOnWsRealFd:
    """Test _exec_on_ws with a real file descriptor (pipe)."""

    async def test_stdin_fd_sends_data(self):
        import base64

        from klangkc.client import _exec_on_ws

        ws = AsyncMock()
        sent = []
        ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

        recv_idx = 0

        async def fake_recv():
            nonlocal recv_idx
            recv_idx += 1
            if recv_idx == 1:
                return json.dumps({"type": "exec_exit", "code": 0})
            await asyncio.sleep(10)

        ws.recv = fake_recv

        # Create a pipe, write data to it, close write end.
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"hello from pipe")
        os.close(write_fd)

        stdin = MagicMock()
        stdin.fileno = MagicMock(return_value=read_fd)

        stdout_buf = io.BytesIO()
        try:
            code = await _exec_on_ws(
                ws, ["cat"], stdin=stdin, stdout=stdout_buf
            )
        finally:
            os.close(read_fd)

        assert code == 0
        # Check that exec_input was sent with our data
        input_msgs = [json.loads(s) for s in sent if "exec_input" in s]
        assert len(input_msgs) >= 1
        decoded = base64.b64decode(input_msgs[0]["data"])
        assert decoded == b"hello from pipe"

    async def test_stdout_fd_receives_data(self):
        """Test output written to a real fd via os.write."""
        import base64

        from klangkc.client import _exec_on_ws

        ws = AsyncMock()
        ws.send = AsyncMock()

        output_data = base64.b64encode(b"hello output").decode()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "exec_output", "data": output_data}),
                json.dumps({"type": "exec_exit", "code": 0}),
            ]
        )

        # Use a pipe for stdout so we can read what was written.
        read_fd, write_fd = os.pipe()

        stdout = MagicMock()
        stdout.fileno = MagicMock(return_value=write_fd)

        try:
            code = await _exec_on_ws(ws, ["echo"], stdout=stdout)
        finally:
            os.close(write_fd)

        assert code == 0
        # Read what was written to the pipe.
        result = os.read(read_fd, 4096)
        os.close(read_fd)
        assert result == b"hello output"

    async def test_ssh_agent_response_queued(self):
        """ssh_agent_response messages are queued for the relay."""
        import base64

        from klangkc.client import _exec_on_ws

        ws = AsyncMock()
        sent = []
        ws.send = AsyncMock(side_effect=lambda m: sent.append(m))

        agent_data = base64.b64encode(b"\x00\x00\x00\x01\x0b").decode()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "ssh_agent_response", "data": agent_data}),
                json.dumps({"type": "exec_exit", "code": 0}),
            ]
        )

        # No real SSH_AUTH_SOCK — the relay will exit early, but the
        # stdout_forward path still exercises the queuing branch.
        with patch.dict(os.environ, {"SSH_AUTH_SOCK": ""}, clear=False):
            code = await _exec_on_ws(ws, ["true"])

        assert code == 0

    async def test_ssh_agent_relay_forwards_to_local_agent(self):
        """ssh_agent_relay reads from queue and talks to local agent."""
        import base64
        import struct
        import tempfile
        import threading

        from klangkc.client import _exec_on_ws

        # Create a fake agent socket that echoes a fixed response.
        agent_response = struct.pack(">I", 1) + b"\x0b"  # 1-byte body
        agent_request = b"\x00\x00\x00\x01\x01"  # request-identities

        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = os.path.join(tmpdir, "agent.sock")
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(sock_path)
            server.listen(1)
            server.settimeout(5)

            # Run the fake agent in a thread so blocking socket calls
            # in the relay don't deadlock the event loop.
            def serve_agent():
                conn, _ = server.accept()
                try:
                    conn.recv(4096)
                    conn.sendall(agent_response)
                finally:
                    conn.close()

            agent_thread = threading.Thread(target=serve_agent, daemon=True)
            agent_thread.start()

            ws = AsyncMock()
            sent = []

            # Track when agent relay has sent its response.
            relay_done = threading.Event()

            async def track_send(m):
                sent.append(m)
                if "ssh_agent_data" in str(m):
                    relay_done.set()

            ws.send = track_send

            recv_idx = 0

            async def fake_recv():
                nonlocal recv_idx
                recv_idx += 1
                if recv_idx == 1:
                    return json.dumps(
                        {
                            "type": "ssh_agent_response",
                            "data": base64.b64encode(agent_request).decode(),
                        }
                    )
                # Wait until the relay has processed before ending.
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, relay_done.wait, 5)
                return json.dumps({"type": "exec_exit", "code": 0})

            ws.recv = fake_recv

            with patch.dict(
                os.environ, {"SSH_AUTH_SOCK": sock_path}, clear=False
            ):
                code = await _exec_on_ws(ws, ["true"])

            agent_thread.join(timeout=2)
            server.close()

        assert code == 0
        # The relay should have sent ssh_agent_data back.
        agent_msgs = [
            json.loads(s) for s in sent if "ssh_agent_data" in str(s)
        ]
        assert len(agent_msgs) >= 1

    async def test_ssh_agent_relay_no_socket(self):
        """Relay exits early when SSH_AUTH_SOCK is unset."""
        from klangkc.client import _exec_on_ws

        ws = AsyncMock()
        ws.send = AsyncMock()
        ws.recv = AsyncMock(
            return_value=json.dumps({"type": "exec_exit", "code": 0})
        )

        with patch.dict(os.environ, {"SSH_AUTH_SOCK": ""}, clear=False):
            code = await _exec_on_ws(ws, ["true"])

        assert code == 0

    async def test_ssh_agent_relay_idle_loop(self):
        """Relay loops with no data then exits when stop is set."""
        import tempfile

        from klangkc.client import _exec_on_ws

        # Create a real socket file so the relay enters the while loop,
        # but never send ssh_agent_response — it should hit the
        # TimeoutError/continue branch, then exit when exec finishes.
        with tempfile.TemporaryDirectory() as tmpdir:
            sock_path = os.path.join(tmpdir, "agent.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(sock_path)
            srv.listen(1)

            ws = AsyncMock()
            ws.send = AsyncMock()

            async def delayed_exit():
                # Give the relay time to hit at least one timeout cycle.
                await asyncio.sleep(1.5)
                return json.dumps({"type": "exec_exit", "code": 0})

            ws.recv = delayed_exit

            with patch.dict(
                os.environ, {"SSH_AUTH_SOCK": sock_path}, clear=False
            ):
                code = await _exec_on_ws(ws, ["true"])

            srv.close()

        assert code == 0

    async def test_ssh_agent_relay_bad_socket(self):
        """Relay handles OSError when local agent socket is a regular file."""
        import base64
        import tempfile

        from klangkc.client import _exec_on_ws

        # Create a regular file (not a socket) so os.path.exists()
        # passes but socket.connect() raises OSError.
        with tempfile.NamedTemporaryFile() as f:
            bad_path = f.name

            ws = AsyncMock()
            sent = []

            async def track_send(m):
                sent.append(m)

            ws.send = track_send

            recv_idx = 0

            async def fake_recv():
                nonlocal recv_idx
                recv_idx += 1
                if recv_idx == 1:
                    return json.dumps(
                        {
                            "type": "ssh_agent_response",
                            "data": base64.b64encode(
                                b"\x00\x00\x00\x01\x01"
                            ).decode(),
                        }
                    )
                # Give the relay time to hit the error path.
                await asyncio.sleep(0.3)
                return json.dumps({"type": "exec_exit", "code": 0})

            ws.recv = fake_recv

            with patch.dict(
                os.environ,
                {"SSH_AUTH_SOCK": bad_path},
                clear=False,
            ):
                code = await _exec_on_ws(ws, ["true"])

        assert code == 0

    async def test_timeout_returns_124(self):
        """When timeout expires, exit code 124 is returned."""
        from klangkc.client import _exec_on_ws

        ws = AsyncMock()
        ws.send = AsyncMock()

        async def hang_forever():
            await asyncio.sleep(3600)

        ws.recv = hang_forever

        code = await _exec_on_ws(ws, ["sleep", "999"], timeout=1)
        assert code == 124


class TestWsExecWrapper:
    """Test _ws_exec wrapper connects and delegates to _exec_on_ws."""

    async def test_ws_exec_delegates(self):

        from klangkc.client import _ws_exec

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
                json.dumps({"type": "exec_exit", "code": 42}),
            ]
        )

        # Use pipes to avoid blocking on real stdin/stdout
        read_fd, write_fd = os.pipe()
        os.close(write_fd)

        with (
            patch("websockets.connect", return_value=ws_mock),
            patch(
                "sys.stdin",
                MagicMock(
                    buffer=MagicMock(fileno=MagicMock(return_value=read_fd))
                ),
            ),
            patch(
                "sys.stdout",
                MagicMock(buffer=MagicMock(fileno=MagicMock(return_value=1))),
            ),
        ):
            code = await _ws_exec("ws://localhost/ws", "token", "ws1", ["ls"])

        os.close(read_fd)
        assert code == 42


class TestSandboxSetupHook:
    """Test that _ws_shell calls sandbox_setup before terminal_start."""

    async def test_sandbox_setup_called(self, tmp_path):
        from klangkc.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()

        sandbox_setup_called = []

        async def fake_sandbox_setup(ws):
            sandbox_setup_called.append(True)

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
                    sandbox_setup=fake_sandbox_setup,
                )
            except Exception:
                pass

        assert sandbox_setup_called == [True]
        # Verify sandbox_setup ran before terminal_start
        sent = [c[0][0] for c in ws_mock.send.call_args_list]
        parsed = [json.loads(s) for s in sent]
        cmds = [m.get("cmd") for m in parsed]
        assert "terminal_start" in cmds


class TestWindowAutoCreate:
    """Test that _ws_shell creates a window when it doesn't exist."""

    async def test_creates_missing_window(self):
        from klangkc.client import _ws_shell

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit
        ws_mock.send = AsyncMock()

        # First recv: workspace_ready
        # Second recv: terminal_windows with only "bash" (no "debug")
        # Third recv: terminal_windows after creation (includes "debug")
        # Fourth recv: stop
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
                            {
                                "id": "@1",
                                "index": 1,
                                "name": "debug",
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
                    window="debug",
                )
            except Exception:
                pass

        sent = [json.loads(c[0][0]) for c in ws_mock.send.call_args_list]
        cmds = [m.get("cmd") for m in sent]
        assert "terminal_new_window" in cmds
        new_win_msg = next(
            m for m in sent if m.get("cmd") == "terminal_new_window"
        )
        assert new_win_msg["name"] == "debug"
        assert "terminal_select_window" in cmds
        select_msg = next(
            m for m in sent if m.get("cmd") == "terminal_select_window"
        )
        assert select_msg["window_id"] == "@1"

    async def test_selects_existing_window(self):
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
                            {
                                "id": "@1",
                                "index": 1,
                                "name": "debug",
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
                    window="debug",
                )
            except Exception:
                pass

        sent = [json.loads(c[0][0]) for c in ws_mock.send.call_args_list]
        cmds = [m.get("cmd") for m in sent]
        # Should select directly, not create
        assert "terminal_new_window" not in cmds
        assert "terminal_select_window" in cmds

    async def test_create_window_buffers_output(self):
        """terminal_output during window creation is buffered."""
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
                # Output arrives during window creation wait
                json.dumps({"type": "terminal_output", "data": "$ "}),
                json.dumps(
                    {
                        "type": "terminal_windows",
                        "windows": [
                            {
                                "id": "@0",
                                "index": 0,
                                "name": "bash",
                                "active": False,
                            },
                            {
                                "id": "@1",
                                "index": 1,
                                "name": "build",
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
                    window="build",
                )
            except Exception:
                pass

        sent = [json.loads(c[0][0]) for c in ws_mock.send.call_args_list]
        cmds = [m.get("cmd") for m in sent]
        assert "terminal_new_window" in cmds

    async def test_create_window_error_response(self):
        """Error from server during window creation raises ConnectionError."""
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
                json.dumps(
                    {
                        "type": "error",
                        "message": "Failed to create window: tmux error",
                    }
                ),
            ]
        )

        with patch("websockets.connect", return_value=ws_mock):
            with pytest.raises(ConnectionError, match="Failed to create"):
                await _ws_shell(
                    "ws://localhost/ws",
                    "token",
                    "ws1",
                    window="bad",
                )
