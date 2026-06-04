"""Tests for the klangk CLI."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from io import BytesIO

from klangk_backend.cli.config import CLIConfig
from klangk_backend.cli.client import (
    AuthError,
    KlangkClient,
    Workspace,
    WorkspaceNotFoundError,
)


# --- Config tests ---


class TestCLIConfig:
    def test_load_empty(self, monkeypatch):
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH",
            Path("/nonexistent/config.toml"),
        )
        cfg = CLIConfig.load()
        assert cfg.server.url == "http://localhost:8995"
        assert cfg.auth.token is None
        assert cfg.auth.email is None

    def test_load_existing(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        config_path.write_text(
            '[server]\nurl = "http://custom:9999"\n\n'
            '[auth]\ntoken = "abc123"\nemail = "test@example.com"\n'
        )
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig.load()
        assert cfg.server.url == "http://custom:9999"
        assert cfg.auth.token == "abc123"
        assert cfg.auth.email == "test@example.com"

    def test_save_roundtrip(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig()
        cfg.server.url = "http://saved:5678"
        cfg.auth.token = "token456"
        cfg.auth.email = "save@test.com"
        cfg.save()
        loaded = CLIConfig.load()
        assert loaded.server.url == "http://saved:5678"
        assert loaded.auth.token == "token456"
        assert loaded.auth.email == "save@test.com"

    def test_save_creates_parent_dirs(self, tmp_path, monkeypatch):
        config_path = tmp_path / "sub" / "dir" / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig()
        cfg.save()
        assert config_path.exists()

    def test_load_token_only(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        config_path.write_text('[auth]\ntoken = "tok"\n')
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig.load()
        assert cfg.auth.token == "tok"
        assert cfg.auth.email is None


# --- Auth tests ---


class TestAuth:
    def test_login_success(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt123"}
        with patch("httpx.post", return_value=mock_resp):
            with patch(
                "klangk_backend.cli.auth.Prompt.ask",
                side_effect=["u@test.com", "pass123"],
            ):
                from klangk_backend.cli import auth

                auth.login("http://localhost:8995")
        cfg = CLIConfig.load()
        assert cfg.auth.token == "jwt123"
        assert cfg.auth.email == "u@test.com"

    def test_login_with_user_flag(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt456"}
        with patch("httpx.post", return_value=mock_resp):
            # Only one Prompt.ask call (password) since email is provided
            with patch(
                "klangk_backend.cli.auth.Prompt.ask",
                return_value="secret",
            ):
                from klangk_backend.cli import auth

                auth.login("http://localhost:8995", email="cli@test.com")
        cfg = CLIConfig.load()
        assert cfg.auth.token == "jwt456"
        assert cfg.auth.email == "cli@test.com"

    def test_login_with_password_file(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        pw_file = tmp_path / "pw.txt"
        pw_file.write_text("file-secret\n")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt789"}
        with patch("httpx.post", return_value=mock_resp):
            from klangk_backend.cli import auth

            auth.login(
                "http://localhost:8995",
                email="pw@test.com",
                password=pw_file.read_text().strip(),
            )
        cfg = CLIConfig.load()
        assert cfg.auth.token == "jwt789"
        assert cfg.auth.email == "pw@test.com"

    def test_login_reuses_valid_token(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        # Save a config with an existing token
        cfg = CLIConfig()
        cfg.server.url = "http://localhost:8995"
        cfg.auth.token = "existing-token"
        cfg.auth.email = "saved@test.com"
        cfg.save()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.get", return_value=mock_resp):
            from klangk_backend.cli import auth

            auth.login("http://localhost:8995")

        # Token should be unchanged — no prompt was shown
        loaded = CLIConfig.load()
        assert loaded.auth.token == "existing-token"
        assert loaded.auth.email == "saved@test.com"

    def test_login_network_error_falls_through(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig()
        cfg.server.url = "http://localhost:8995"
        cfg.auth.token = "old-token"
        cfg.auth.email = "old@test.com"
        cfg.save()

        # GET raises network error, then POST succeeds
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {"access_token": "fresh"}
        with patch("httpx.get", side_effect=httpx.ConnectError("unreachable")):
            with patch("httpx.post", return_value=post_resp):
                with patch(
                    "klangk_backend.cli.auth.Prompt.ask",
                    side_effect=["new@test.com", "pw"],
                ):
                    from klangk_backend.cli import auth

                    auth.login("http://localhost:8995")

        loaded = CLIConfig.load()
        assert loaded.auth.token == "fresh"

    def test_login_expired_token_prompts(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig()
        cfg.server.url = "http://localhost:8995"
        cfg.auth.token = "expired-token"
        cfg.auth.email = "old@test.com"
        cfg.save()

        # GET returns 401 (expired), then POST succeeds with new token
        get_resp = MagicMock()
        get_resp.status_code = 401
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {"access_token": "new-token"}
        with patch("httpx.get", return_value=get_resp):
            with patch("httpx.post", return_value=post_resp):
                with patch(
                    "klangk_backend.cli.auth.Prompt.ask",
                    side_effect=["new@test.com", "pw"],
                ):
                    from klangk_backend.cli import auth

                    auth.login("http://localhost:8995")

        loaded = CLIConfig.load()
        assert loaded.auth.token == "new-token"
        assert loaded.auth.email == "new@test.com"

    def test_login_failure(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"detail": "Bad credentials"}
        with patch("httpx.post", return_value=mock_resp):
            with patch(
                "klangk_backend.cli.auth.Prompt.ask",
                side_effect=["u@test.com", "wrong"],
            ):
                from klangk_backend.cli import auth

                with pytest.raises(SystemExit):
                    auth.login("http://localhost:8995")

    def test_logout_clears_token(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        config_path.write_text('[auth]\ntoken = "tok"\nemail = "x@y.com"\n')
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.post", return_value=mock_resp):
            from klangk_backend.cli import auth

            auth.logout()
        cfg = CLIConfig.load()
        assert cfg.auth.token is None
        assert cfg.auth.email is None

    def test_logout_swallows_server_error(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig()
        cfg.save()
        with patch("httpx.post", side_effect=Exception("no server")):
            from klangk_backend.cli import auth

            auth.logout()  # Should not raise


# --- KlangkClient tests ---


class TestKlangkClient:
    def test_auth_error_on_401(self, monkeypatch):
        cfg = CLIConfig()
        cfg.auth.token = None
        client = KlangkClient(cfg)
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch.object(client, "get", return_value=mock_resp):
            with pytest.raises(AuthError, match="Session expired"):
                client.list_workspaces()

    def test_list_workspaces_parses_response(self):
        cfg = CLIConfig()
        cfg.auth.token = "valid-token"
        client = KlangkClient(cfg)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "id": "ws1",
                "name": "alpha",
                "created_at": "2025-01-01T00:00:00Z",
            },
            {
                "id": "ws2",
                "name": "beta",
                "created_at": "2025-06-15T12:00:00Z",
            },
        ]
        with patch.object(client, "get", return_value=mock_resp):
            workspaces = client.list_workspaces()
        assert len(workspaces) == 2
        assert workspaces[0].name == "alpha"
        assert workspaces[1].id == "ws2"

    def test_create_workspace_returns_workspace(self):
        cfg = CLIConfig()
        cfg.auth.token = "token"
        client = KlangkClient(cfg)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "new-ws-id",
            "name": "new-ws",
            "created_at": "2025-01-01T00:00:00Z",
        }
        with patch.object(client, "post", return_value=mock_resp):
            ws = client.create_workspace("new-ws")
        assert ws.name == "new-ws"
        assert ws.id == "new-ws-id"

    def test_delete_workspace_not_found(self):
        cfg = CLIConfig()
        cfg.auth.token = "token"
        client = KlangkClient(cfg)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch.object(client, "get", return_value=mock_resp):
            with pytest.raises(WorkspaceNotFoundError):
                client.delete_workspace("nonexistent")

    def test_delete_workspace_success(self):
        cfg = CLIConfig()
        cfg.auth.token = "token"
        client = KlangkClient(cfg)
        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = [
            {
                "id": "ws-to-delete",
                "name": "gone",
                "created_at": "2025-01-01T00:00:00Z",
            }
        ]
        del_resp = MagicMock()
        del_resp.status_code = 204
        with patch.object(client, "get", return_value=list_resp):
            with patch.object(
                client, "delete", return_value=del_resp
            ) as mock_del:
                client.delete_workspace("gone")
                mock_del.assert_called_once_with("/workspaces/ws-to-delete")

    def test_resolve_workspace_by_name(self):
        cfg = CLIConfig()
        cfg.auth.token = "token"
        client = KlangkClient(cfg)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "id": "ws1",
                "name": "alpha",
                "created_at": "2025-01-01T00:00:00Z",
            },
            {
                "id": "ws2",
                "name": "beta",
                "created_at": "2025-01-01T00:00:00Z",
            },
        ]
        with patch.object(client, "get", return_value=mock_resp):
            ws = client.resolve_workspace("beta")
        assert ws.id == "ws2"

    def test_resolve_workspace_not_found_raises(self):
        cfg = CLIConfig()
        cfg.auth.token = "token"
        client = KlangkClient(cfg)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {
                "id": "ws1",
                "name": "alpha",
                "created_at": "2025-01-01T00:00:00Z",
            }
        ]
        with patch.object(client, "get", return_value=mock_resp):
            with pytest.raises(WorkspaceNotFoundError):
                client.resolve_workspace("nonexistent")

    def test_delete_workspace_401_raises_auth_error(self):
        cfg = CLIConfig()
        cfg.auth.token = "token"
        client = KlangkClient(cfg)
        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = [
            {"id": "ws1", "name": "ws1", "created_at": "2025-01-01T00:00:00Z"}
        ]
        del_resp = MagicMock()
        del_resp.status_code = 401
        with patch.object(client, "get", return_value=list_resp):
            with patch.object(client, "delete", return_value=del_resp):
                with pytest.raises(AuthError):
                    client.delete_workspace("ws1")

    def test_delete_workspace_non_200_exits(self):
        cfg = CLIConfig()
        cfg.auth.token = "token"
        client = KlangkClient(cfg)
        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = [
            {"id": "ws1", "name": "ws1", "created_at": "2025-01-01T00:00:00Z"}
        ]
        del_resp = MagicMock()
        del_resp.status_code = 500
        del_resp.text = "Server error"
        del_resp.is_success = False
        with patch.object(client, "get", return_value=list_resp):
            with patch.object(client, "delete", return_value=del_resp):
                with pytest.raises(SystemExit):
                    client.delete_workspace("ws1")

    def test_no_token_uses_empty_string(self):
        cfg = CLIConfig()
        cfg.auth.token = None
        client = KlangkClient(cfg)
        headers = client._headers()
        assert headers["Authorization"] == "Bearer "


# --- Shell protocol ---


class TestShellProtocol:
    def test_ws_url_http_conversion(self):
        url = "http://localhost:8995"
        ws_url = url.replace("http://", "ws://").rstrip("/") + "/ws"
        assert ws_url == "ws://localhost:8995/ws"

    def test_ws_url_https_conversion(self):
        url = "https://klangk.example.com"
        ws_url = url.replace("https://", "wss://").rstrip("/") + "/ws"
        assert ws_url == "wss://klangk.example.com/ws"


# --- Terminal size ---


class TestTerminalSize:
    def test_get_terminal_size_positive_ints(self):
        from klangk_backend.cli.client import _get_terminal_size

        cols, rows = _get_terminal_size()
        assert isinstance(cols, int) and cols > 0
        assert isinstance(rows, int) and rows > 0

    def test_get_terminal_size_returns_default_when_not_tty(self, monkeypatch):
        """When stdin is not a TTY, _get_terminal_size returns (80, 24) without calling os."""
        from klangk_backend.cli import client as cli_client

        called = []

        def _track(*args):
            called.append(args)
            raise OSError("should not be called")

        # sys.stdin is not a TTY in tests — no need to call os.get_terminal_size
        monkeypatch.setattr(os, "get_terminal_size", _track)
        cols, rows = cli_client._get_terminal_size()
        assert cols == 80
        assert rows == 24
        assert len(called) == 0  # os.get_terminal_size was never invoked

    def test_get_terminal_size_calls_os_when_tty(self, monkeypatch):
        from klangk_backend.cli import client as cli_client

        called_with = []

        def _track(*args):
            class FakeSize:
                columns = 102
                lines = 40

            called_with.append(args)
            return FakeSize()

        monkeypatch.setattr(os, "get_terminal_size", _track)
        # sys.stdin is not a TTY in tests — make it look like one
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        cols, rows = cli_client._get_terminal_size()
        assert cols == 102
        assert rows == 40
        assert len(called_with) == 1


# --- _run_shell / _ws_shell ---


class TestRunShell:
    @pytest.mark.asyncio
    async def test_stdin_loop_sends_terminal_input(self):
        from klangk_backend.cli.client import _run_shell

        ws = AsyncMock()
        ws.send = AsyncMock()
        ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "event",
                    "event": {
                        "type": "CUSTOM",
                        "name": "container_stopped",
                        "value": {},
                    },
                }
            )
        )

        fake_buf = BytesIO(b"x")
        fake_buf.fileno = lambda: 99
        call_count = 0

        def fake_select(rlist, wlist, xlist, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return ([99], [], [])
            return ([], [], [])

        def fake_os_read(fd, n):
            if fd == 99:
                return b"x"
            return b""

        with (
            patch(
                "klangk_backend.cli.client.select.select",
                side_effect=fake_select,
            ),
            patch(
                "klangk_backend.cli.client.os.read", side_effect=fake_os_read
            ),
        ):
            task = asyncio.create_task(_run_shell(ws, 80, 24, stdin=fake_buf))
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        sent = [c[0][0] for c in ws.send.call_args_list]
        assert any("terminal_input" in s and '"x"' in s for s in sent)

    @pytest.mark.asyncio
    async def test_stdin_loop_batches_escape_sequences(self):
        from klangk_backend.cli.client import _run_shell

        ws = AsyncMock()
        ws.send = AsyncMock()
        ws.recv = AsyncMock(
            return_value=json.dumps(
                {
                    "type": "event",
                    "event": {
                        "type": "CUSTOM",
                        "name": "container_stopped",
                        "value": {},
                    },
                }
            )
        )

        fake_buf = BytesIO(b"\x1b")
        fake_buf.fileno = lambda: 99
        call_count = 0

        def fake_select(rlist, wlist, xlist, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ([99], [], [])  # initial select: ESC ready
            if call_count == 2:
                return ([99], [], [])  # ESC follow-up: [D ready
            return ([], [], [])

        read_count = 0

        def fake_os_read(fd, n):
            nonlocal read_count
            read_count += 1
            if fd == 99:
                if read_count == 1:
                    return b"\x1b"
                if read_count == 2:
                    return b"[D"
            return b""

        with (
            patch(
                "klangk_backend.cli.client.select.select",
                side_effect=fake_select,
            ),
            patch(
                "klangk_backend.cli.client.os.read", side_effect=fake_os_read
            ),
        ):
            task = asyncio.create_task(_run_shell(ws, 80, 24, stdin=fake_buf))
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        sent = [c[0][0] for c in ws.send.call_args_list]
        # ESC + [D should be sent as one message
        assert any("terminal_input" in s and "\\u001b[D" in s for s in sent)

    @pytest.mark.asyncio
    async def test_stdout_loop_writes_data(self):
        from klangk_backend.cli.client import _run_shell

        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "terminal_output", "data": "hello"}),
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

        fake_buf = BytesIO(b"")
        fake_buf.fileno = lambda: 0
        fake_stdout = CaptureWriter()
        with patch(
            "klangk_backend.cli.client.select.select",
            return_value=([0], [], []),
        ):
            task = asyncio.create_task(
                _run_shell(ws, 80, 24, stdin=fake_buf, stdout=fake_stdout)
            )
            await asyncio.sleep(0.3)
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert "hello" in "".join(captured)

    @pytest.mark.asyncio
    async def test_ws_shell_connection_failure(self):
        from klangk_backend.cli.client import _ws_shell

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
            with pytest.raises(ConnectionError) as exc_info:
                await _ws_shell("ws://localhost/ws", "token", "ws1")
            assert "Connection failed" in str(exc_info.value)


# --- Misc ---


class TestMisc:
    def test_auth_error_message(self):
        err = AuthError("Session expired — run `klangk login`")
        assert "Session expired" in str(err)
        assert "klangk login" in str(err)

    def test_workspace_dataclass_fields(self):
        ws = Workspace(id="x", name="y", created_at="z")
        assert ws.id == "x"
        assert ws.name == "y"
        assert ws.created_at == "z"

    def test_login_success_stores_email(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "klangk_backend.cli.config._CONFIG_PATH", config_path
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt"}
        with patch("httpx.post", return_value=mock_resp):
            with patch(
                "klangk_backend.cli.auth.Prompt.ask",
                side_effect=["admin@example.com", "pw"],
            ):
                from klangk_backend.cli import auth

                auth.login("http://localhost:8995")
        cfg = CLIConfig.load()
        assert cfg.auth.email == "admin@example.com"
        assert cfg.auth.token == "jwt"


class TestExportImportClient:
    def test_export_workspace_streams_to_file(self, tmp_path):
        cfg = CLIConfig()
        cfg.server.url = "http://localhost:8995"
        cfg.auth.token = "token"
        client = KlangkClient(cfg)

        output = tmp_path / "exported.tar.gz"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.headers = {"content-length": "12"}
        mock_resp.iter_bytes.return_value = [b"chunk1", b"chunk2"]

        progress_calls = []

        def _on_progress(downloaded, total):
            progress_calls.append((downloaded, total))

        with patch("httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__ = MagicMock(
                return_value=mock_resp
            )
            mock_stream.return_value.__exit__ = MagicMock(return_value=False)
            client.export_workspace(
                "ws-id-123", output, on_progress=_on_progress
            )

        assert output.read_bytes() == b"chunk1chunk2"
        mock_stream.assert_called_once()
        assert progress_calls == [(6, 12), (12, 12)]

    def test_export_workspace_uses_estimated_size(self, tmp_path):
        cfg = CLIConfig()
        cfg.server.url = "http://localhost:8995"
        cfg.auth.token = "token"
        client = KlangkClient(cfg)

        output = tmp_path / "est.tar.gz"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.headers = {"x-estimated-size": "5000"}
        mock_resp.iter_bytes.return_value = [b"data"]

        progress_calls = []

        with patch("httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__ = MagicMock(
                return_value=mock_resp
            )
            mock_stream.return_value.__exit__ = MagicMock(return_value=False)
            client.export_workspace(
                "ws-id",
                output,
                on_progress=lambda d, t: progress_calls.append((d, t)),
            )

        assert progress_calls == [(4, 5000)]

    def test_export_workspace_no_size_headers(self, tmp_path):
        cfg = CLIConfig()
        cfg.server.url = "http://localhost:8995"
        cfg.auth.token = "token"
        client = KlangkClient(cfg)

        output = tmp_path / "nosize.tar.gz"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.headers = {}
        mock_resp.iter_bytes.return_value = [b"chunk"]

        progress_calls = []

        with patch("httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__ = MagicMock(
                return_value=mock_resp
            )
            mock_stream.return_value.__exit__ = MagicMock(return_value=False)
            client.export_workspace(
                "ws-id",
                output,
                on_progress=lambda d, t: progress_calls.append((d, t)),
            )

        assert progress_calls == [(5, None)]

    def test_export_workspace_http_error(self, tmp_path):
        cfg = CLIConfig()
        cfg.server.url = "http://localhost:8995"
        cfg.auth.token = "token"
        client = KlangkClient(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.is_success = False
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "not found", request=MagicMock(), response=mock_resp
        )

        with patch("httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__ = MagicMock(
                return_value=mock_resp
            )
            mock_stream.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(httpx.HTTPStatusError):
                client.export_workspace("bad-id", tmp_path / "out.tar.gz")

        mock_resp.read.assert_called_once()

    def test_export_workspace_auth_error(self, tmp_path):
        cfg = CLIConfig()
        cfg.auth.token = "bad"
        client = KlangkClient(cfg)

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__ = MagicMock(
                return_value=mock_resp
            )
            mock_stream.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(AuthError):
                client.export_workspace("ws-id", tmp_path / "out.tar.gz")

    def test_import_workspace_returns_workspace(self, tmp_path):
        cfg = CLIConfig()
        cfg.server.url = "http://localhost:8995"
        cfg.auth.token = "token"
        client = KlangkClient(cfg)

        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"fake archive")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "new-id",
            "name": "imported-ws",
            "created_at": "2025-01-01",
        }

        with patch("httpx.post", return_value=mock_resp):
            ws = client.import_workspace(archive, name="imported-ws")

        assert ws.name == "imported-ws"
        assert ws.id == "new-id"

    def test_import_workspace_auth_error(self, tmp_path):
        cfg = CLIConfig()
        cfg.auth.token = "bad"
        client = KlangkClient(cfg)

        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"fake")

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("httpx.post", return_value=mock_resp):
            with pytest.raises(AuthError):
                client.import_workspace(archive)

    def test_import_workspace_no_name(self, tmp_path):
        cfg = CLIConfig()
        cfg.server.url = "http://localhost:8995"
        cfg.auth.token = "token"
        client = KlangkClient(cfg)

        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"fake")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "id2",
            "name": "from-archive",
            "created_at": "2025-01-01",
        }

        with patch("httpx.post", return_value=mock_resp) as mock_post:
            ws = client.import_workspace(archive)

        assert ws.name == "from-archive"
        # Verify no name param was sent
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs.get("params") == {}
