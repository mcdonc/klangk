"""Tests for klangk CLI commands (main.py)."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import typer
import websockets

from klangk.cli.client import WorkspaceNotFoundError
from klangk.cli.config import (
    CLIConfig,
    CLIState,
    ServerEntry,
)
from klangk.cli.client import Workspace


@pytest.fixture
def logged_in_cfg(tmp_path, monkeypatch):
    """Config + state with a valid token and email pre-loaded."""
    config_path = tmp_path / "klangk.yaml"
    state_path = tmp_path / "klangk-state.yaml"
    monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
    monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
    # Write a minimal klangk.yaml (no servers needed for most tests)
    config_path.write_text("")
    # Write klangk-state.yaml with active server and credentials
    state = CLIState()
    state.set_credentials(
        "http://localhost:8995", "test@example.com", "test-token"
    )
    state.save()
    yield tmp_path
    # No teardown needed — each test gets a fresh tmp_path


@pytest.fixture(autouse=True)
def reset_main_state():
    """Reset module-level CLI state before and after each test."""
    import klangk.cli.main as _main

    orig_cfg = _main._cfg_cache
    orig_state = _main._state_cache
    orig_server = _main._server_override
    _main._cfg_cache = None
    _main._state_cache = None
    _main._server_override = None
    yield
    _main._cfg_cache = orig_cfg
    _main._state_cache = orig_state
    _main._server_override = orig_server


@pytest.fixture
def reset_env():
    """Save and restore os.environ."""
    orig_env = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(orig_env)


class TestMainCLI:
    @pytest.fixture(autouse=True)
    def no_oidc(self, monkeypatch):
        monkeypatch.setattr("klangk.cli.auth.fetch_config", lambda _: {})

    def test_login_cmd_stores_token(self, tmp_path, monkeypatch):
        from klangk.cli.main import login_cmd

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "new-token"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with patch(
                "klangk.cli.auth.Prompt.ask",
                side_effect=["u@test.com", "pw"],
            ):
                login_cmd(
                    server="http://localhost:8995",
                    user=None,
                    password_file=None,
                )
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") == "new-token"
        assert state.get_email("http://localhost:8995") == "u@test.com"

    def test_login_cmd_with_password_file(self, tmp_path, monkeypatch):
        from klangk.cli.main import login_cmd

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        pw_file = tmp_path / "pw.txt"
        pw_file.write_text("file-pw\n")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "file-token"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            login_cmd(
                server="http://localhost:8995",
                user="file@test.com",
                password_file=str(pw_file),
            )
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") == "file-token"

    def test_login_cmd_with_password_stdin(self, tmp_path, monkeypatch):
        from klangk.cli.main import login_cmd

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "stdin-token"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.readline.return_value = "stdin-pw\n"
                login_cmd(
                    server="http://localhost:8995",
                    user="stdin@test.com",
                    password_file="-",
                )
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") == "stdin-token"

    def test_login_cmd_resolves_alias(self, tmp_path, monkeypatch):
        from klangk.cli.main import login_cmd

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text(
            "servers:\n  prod:\n    url: http://prod:8995\n"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "tok"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with patch(
                "klangk.cli.auth.Prompt.ask",
                side_effect=["u@test.com", "pw"],
            ):
                login_cmd(
                    server="prod",
                    user=None,
                    password_file=None,
                )
        state = CLIState.load()
        assert state.get_token("http://prod:8995") == "tok"
        assert state.active_server == "http://prod:8995"

    def test_login_cmd_uses_config_default_user(self, tmp_path, monkeypatch):
        from klangk.cli.main import login_cmd

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text(
            "servers:\n"
            "  prod:\n"
            "    url: http://prod:8995\n"
            "    user: default@prod.com\n"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "tok"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with patch(
                "klangk.cli.auth.Prompt.ask",
                return_value="pw",
            ):
                login_cmd(
                    server="prod",
                    user=None,
                    password_file=None,
                )
        state = CLIState.load()
        assert state.get_email("http://prod:8995") == "default@prod.com"

    def test_require_auth_raises_when_not_logged_in(
        self, tmp_path, monkeypatch
    ):
        import typer
        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        # State with active server but no token
        state = CLIState(active_server="http://localhost:8995")
        state.save()
        # Patch the live server probe so the test is deterministic
        # regardless of whether a real klangkd is running on localhost:8995
        # in none mode (which would auto-login and skip the Exit).
        monkeypatch.setattr(
            "klangk.cli.main.fetch_config",
            lambda url: {"auth_modes": "password"},
        )

        with pytest.raises(typer.Exit):
            main.require_auth()

    def test_require_auth_passes_when_logged_in(self, logged_in_cfg):
        from klangk.cli import main

        main.require_auth()  # Should not raise

    def test_server_url_no_server_exits(self, tmp_path, monkeypatch):
        """server_url() exits when no active server, no --server, and no
        co-located klangkd UDS at the default path."""
        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        CLIState().save()
        # Point the default-UDS derivation at an empty tmp dir so the
        # existence gate fails deterministically regardless of whether a
        # real klangkd is running on the host.
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("KLANGK_STATE_DIR", raising=False)

        with pytest.raises(typer.Exit):
            main.server_url()

    def test_server_url_uses_active_server(self, logged_in_cfg):
        from klangk.cli import main

        assert main.server_url() == "http://localhost:8995"

    def test_server_url_override_wins(self, logged_in_cfg):
        from klangk.cli import main

        main._server_override = "http://override:9999"
        assert main.server_url() == "http://override:9999"

    def test_server_url_falls_back_to_default_uds(self, tmp_path, monkeypatch):
        """When the default klangkd UDS exists, server_url() uses it (#1676)."""
        import socket as _socket

        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        CLIState().save()  # no active server, no --server
        # Bind a real AF_UNIX socket at the default-derived path.
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("KLANGK_STATE_DIR", raising=False)
        sock_dir = tmp_path / "klangkd"
        sock_dir.mkdir()
        sock_path = sock_dir / "klangk.sock"
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        try:
            assert main.server_url() == str(sock_path)
        finally:
            srv.close()

    def test_server_url_default_uds_respects_klangk_state_dir(
        self, tmp_path, monkeypatch
    ):
        """KLANGK_STATE_DIR relocates the default UDS (#1676)."""
        import socket as _socket

        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        CLIState().save()
        custom_state = tmp_path / "custom-state"
        custom_state.mkdir()
        monkeypatch.setenv("KLANGK_STATE_DIR", str(custom_state))
        sock_path = custom_state / "klangk.sock"
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        try:
            assert main.server_url() == str(sock_path)
        finally:
            srv.close()

    def test_default_server_uds_path_derivation(self, monkeypatch):
        """Unit-test the path derivation independent of existence."""
        from klangk.cli import main

        monkeypatch.delenv("KLANGK_STATE_DIR", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", "/custom/xdg")
        assert (
            main._default_server_uds_path()
            == "/custom/xdg/klangkd/klangk.sock"
        )

        monkeypatch.setenv("XDG_STATE_HOME", "/custom/xdg")
        monkeypatch.setenv("KLANGK_STATE_DIR", "/explicit/state")
        assert main._default_server_uds_path() == "/explicit/state/klangk.sock"

        monkeypatch.delenv("KLANGK_STATE_DIR", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        # Unset fallback → ~/.local/state (expanduser under HOME).
        assert main._default_server_uds_path().endswith(
            ".local/state/klangkd/klangk.sock"
        )

        # An explicit plain absolute KLANGK_SOCKET is honored verbatim —
        # the server binds exactly there and skips the state_dir default.
        monkeypatch.setenv("KLANGK_SOCKET", "/run/klangk.sock")
        assert main._default_server_uds_path() == "/run/klangk.sock"

        # file:/cmd: indirections can't be resolved client-side and must
        # fall through to the state_dir-derived default.
        for indirect in ("file:/etc/klangk/socket", "cmd:cat /etc/socket"):
            monkeypatch.setenv("KLANGK_SOCKET", indirect)
            assert main._default_server_uds_path().endswith(
                ".local/state/klangkd/klangk.sock"
            )
        monkeypatch.delenv("KLANGK_SOCKET", raising=False)

    def test_server_url_falls_back_to_klangk_socket(
        self, tmp_path, monkeypatch
    ):
        """A plain absolute KLANGK_SOCKET is used when it exists (#1676)."""
        import socket as _socket

        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        CLIState().save()
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))  # ensure default
        # path is absent so only KLANGK_SOCKET could resolve
        sock_path = tmp_path / "relocated.sock"
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(str(sock_path))
        monkeypatch.setenv("KLANGK_SOCKET", str(sock_path))
        try:
            assert main.server_url() == str(sock_path)
        finally:
            srv.close()

    def test_server_url_active_server_beats_default_uds(
        self, tmp_path, monkeypatch
    ):
        """active-server wins even when the default UDS exists."""
        import socket as _socket

        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        # Bind a default UDS so the fallback *would* fire …
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        sock_dir = tmp_path / "klangkd"
        sock_dir.mkdir()
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(str(sock_dir / "klangk.sock"))
        # … but an active-server is set and must win.
        CLIState(active_server="http://elsewhere:8995").save()
        try:
            assert main.server_url() == "http://elsewhere:8995"
        finally:
            srv.close()

    def test_server_url_override_beats_default_uds(
        self, tmp_path, monkeypatch
    ):
        """--server override wins even when the default UDS exists."""
        import socket as _socket

        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        CLIState().save()
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        sock_dir = tmp_path / "klangkd"
        sock_dir.mkdir()
        srv = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        srv.bind(str(sock_dir / "klangk.sock"))
        main._server_override = "http://override:9999"
        try:
            assert main.server_url() == "http://override:9999"
        finally:
            srv.close()

    def test_require_auth_stale_uds_says_unreachable(
        self, tmp_path, monkeypatch, capsys
    ):
        """An unreachable UDS reports a connect error, not 'Not logged in'
        (#1676 — stale default socket left by a crashed klangkd)."""
        import typer

        from klangk.cli import main
        from klangk.cli.auth import _UNREACHABLE

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        CLIState().save()  # no token
        # Resolve to a UDS path without relying on disk existence: the
        # --server override bypasses server_url()'s exists() gate.
        stale = str(tmp_path / "stale.sock")
        main._server_override = stale
        monkeypatch.setattr(
            "klangk.cli.main.fetch_config", lambda url: _UNREACHABLE
        )

        with pytest.raises(typer.Exit):
            main.require_auth()
        err = capsys.readouterr().err
        assert "Cannot connect to klangkd" in err
        assert "is it running" in err
        assert "Not logged in" not in err
        # Rich wraps the stderr line at the capture width, so normalize
        # newlines before checking the path is rendered.
        assert stale in err.replace("\n", "")

    def test_app_callback_resolves_server_alias(self, tmp_path, monkeypatch):
        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text(
            "servers:\n  prod:\n    url: http://prod:8995\n"
        )
        CLIState().save()

        main.app_callback(server="prod")
        assert main._server_override == "http://prod:8995"

    def test_list_workspaces_empty(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.list_workspaces.return_value = []
        client.list_shared_workspaces.return_value = []
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo") as mock_echo:
            main.list_workspaces(shared=True)
        assert any("No workspaces" in str(c) for c in mock_echo.call_args_list)

    def test_list_shared_workspaces_plain(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        shared_ws = Workspace(
            id="sw1" + "0" * 52,
            name="shared-ws",
            created_at="2025-01-01T00:00:00Z",
            owner_email="owner@example.com",
        )
        client = MagicMock()
        client.list_workspaces.return_value = []
        client.list_shared_workspaces.return_value = [shared_ws]
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo") as mock_echo:
            main.list_workspaces(plain=True, shared=True)
        calls = [str(c) for c in mock_echo.call_args_list]
        assert any("shared-ws" in c for c in calls)
        assert any("owner@example.com" in c for c in calls)

    def test_list_shared_workspaces_rich(self, logged_in_cfg, monkeypatch):
        from io import StringIO

        from rich.console import Console

        from klangk.cli import main

        shared_ws = Workspace(
            id="sw1" + "0" * 52,
            name="shared-ws",
            created_at="2025-01-01T00:00:00Z",
            owner_email="owner@example.com",
        )
        client = MagicMock()
        client.list_workspaces.return_value = []
        client.list_shared_workspaces.return_value = [shared_ws]
        monkeypatch.setattr(main, "_client", lambda: client)

        buf = StringIO()
        with patch.object(
            main,
            "Console",
            return_value=Console(file=buf, force_terminal=True),
        ):
            main.list_workspaces(plain=False, shared=True)
        output = buf.getvalue()
        assert "shared-ws" in output
        assert "owner@example.com" in output

    def test_list_workspaces_plain(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-workspace",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.list_workspaces.return_value = [ws]
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo") as mock_echo:
            main.list_workspaces(plain=True)
        assert any("my-workspace" in str(c) for c in mock_echo.call_args_list)

    def test_list_workspaces_rich(self, logged_in_cfg, monkeypatch):
        from io import StringIO

        from rich.console import Console

        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-workspace",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.list_workspaces.return_value = [ws]
        monkeypatch.setattr(main, "_client", lambda: client)

        buf = StringIO()
        with patch.object(
            main,
            "Console",
            return_value=Console(file=buf, force_terminal=True),
        ):
            main.list_workspaces(plain=False)
        output = buf.getvalue()
        assert "my-workspace" in output
        assert "2025-01-01" in output

    def test_list_workspaces_plain_shows_status_and_short_id(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        ws = Workspace(
            id="abc" + "0" * 40 + "xyz",
            name="demo",
            created_at="2025-01-01T00:00:00Z",
            running=True,
            health_check="/path/to/check.sh",
            health="healthy",
        )
        client = MagicMock()
        client.list_workspaces.return_value = [ws]
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo") as mock_echo:
            main.list_workspaces(plain=True)
        line = next(
            str(c) for c in mock_echo.call_args_list if "demo" in str(c)
        )
        assert "healthy" in line  # plain status label
        assert "\x1b" not in line  # no ANSI color in --plain
        assert "abc…xyz" in line  # shortened id

    def test_list_workspaces_rich_shows_status_column(
        self, logged_in_cfg, monkeypatch
    ):
        from io import StringIO

        from rich.console import Console

        from klangk.cli import main

        ws = Workspace(
            id="abc" + "0" * 40 + "xyz",
            name="demo",
            created_at="2025-01-01T00:00:00Z",
            running=True,
            health_check="/path/to/check.sh",
            health="unhealthy",
        )
        client = MagicMock()
        client.list_workspaces.return_value = [ws]
        monkeypatch.setattr(main, "_client", lambda: client)

        buf = StringIO()
        with patch.object(
            main,
            "Console",
            return_value=Console(file=buf, force_terminal=True),
        ):
            main.list_workspaces(plain=False)
        output = buf.getvalue()
        assert "Status" in output  # column header
        assert "unhealthy" in output  # status label text renders
        assert "abc…xyz" in output  # shortened id

    def test_list_shared_workspaces_plain_shows_status(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        shared_ws = Workspace(
            id="abc" + "0" * 40 + "xyz",
            name="shared-ws",
            created_at="2025-01-01T00:00:00Z",
            owner_email="owner@example.com",
            running=True,
            health_check="/path/to/check.sh",
            health="healthy",
        )
        client = MagicMock()
        client.list_workspaces.return_value = []
        client.list_shared_workspaces.return_value = [shared_ws]
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo") as mock_echo:
            main.list_workspaces(plain=True, shared=True)
        line = next(
            str(c) for c in mock_echo.call_args_list if "shared-ws" in str(c)
        )
        assert "healthy" in line
        assert "\x1b" not in line

    def test_list_workspaces_all_passes_pagination_flag(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        client = MagicMock()
        client.list_workspaces.return_value = []
        client.list_shared_workspaces.return_value = []
        monkeypatch.setattr(main, "_client", lambda: client)

        main.list_workspaces(
            limit=10,
            all_workspaces=True,
            shared=True,
            sort="created",
            order="desc",
            filter=None,
        )

        client.list_workspaces.assert_called_once_with(
            limit=10, all_pages=True, sort="created", order="desc", q=None
        )
        client.list_shared_workspaces.assert_called_once_with(
            limit=10, all_pages=True, sort="created", order="desc", q=None
        )

    def test_list_workspaces_limit_forwarded(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.list_workspaces.return_value = []
        client.list_shared_workspaces.return_value = []
        monkeypatch.setattr(main, "_client", lambda: client)

        main.list_workspaces(
            limit=50,
            all_workspaces=False,
            shared=True,
            sort="created",
            order="desc",
            filter=None,
        )

        client.list_workspaces.assert_called_once_with(
            limit=50, all_pages=False, sort="created", order="desc", q=None
        )
        client.list_shared_workspaces.assert_called_once_with(
            limit=50, all_pages=False, sort="created", order="desc", q=None
        )

    def test_list_workspaces_sort_filter_forwarded(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        client = MagicMock()
        client.list_workspaces.return_value = []
        client.list_shared_workspaces.return_value = []
        monkeypatch.setattr(main, "_client", lambda: client)

        main.list_workspaces(
            limit=10,
            all_workspaces=False,
            sort="name",
            order="asc",
            filter="gamma",
            shared=True,
        )

        client.list_workspaces.assert_called_once_with(
            limit=10,
            all_pages=False,
            sort="name",
            order="asc",
            q="gamma",
        )
        client.list_shared_workspaces.assert_called_once_with(
            limit=10,
            all_pages=False,
            sort="name",
            order="asc",
            q="gamma",
        )

    def test_create_workspace(self, logged_in_cfg, monkeypatch):
        from io import StringIO

        from rich.console import Console

        from klangk.cli import main

        ws = Workspace(
            id="new-id", name="new-ws", created_at="2025-01-01T00:00:00Z"
        )
        client = MagicMock()
        client.create_workspace.return_value = ws
        monkeypatch.setattr(main, "_client", lambda: client)

        buf = StringIO()
        with patch.object(
            main,
            "Console",
            return_value=Console(file=buf, force_terminal=True),
        ):
            main.create("new-ws")
        assert "new-ws" in buf.getvalue()

    def test_create_workspace_error(self, logged_in_cfg, monkeypatch):
        import typer

        from klangk.cli import main

        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.json.return_value = {"detail": "duplicate name"}
        mock_response.text = "duplicate name"
        client = MagicMock()
        client.create_workspace.side_effect = httpx.HTTPStatusError(
            "bad", request=MagicMock(), response=mock_response
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.create("dup")

    def test_delete_workspace(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo"):
            main.rm("my-ws")
        client.delete_workspace.assert_called_once_with("my-ws")

    def test_delete_workspace_not_found(self, logged_in_cfg, monkeypatch):
        import typer

        from klangk.cli.client import WorkspaceNotFoundError
        from klangk.cli import main

        client = MagicMock()
        client.delete_workspace.side_effect = WorkspaceNotFoundError("nope")
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.rm("nope")

    def test_members_command(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from klangk.cli.client import Workspace

        client = MagicMock()
        client.resolve_workspace.return_value = Workspace(
            id="ws-1", name="my-ws", created_at="2025-01-01"
        )
        roles_resp = MagicMock()
        roles_resp.status_code = 200
        roles_resp.json.return_value = [
            {
                "role": "coders",
                "members": [{"id": "u1", "email": "alice@test.com"}],
            },
            {"role": "spectators", "members": []},
        ]
        client.get.return_value = roles_resp
        monkeypatch.setattr(main, "_client", lambda: client)

        calls = []
        monkeypatch.setattr("typer.echo", lambda s: calls.append(s))
        main.members("my-ws")
        assert any("alice@test.com" in c for c in calls)
        assert any("coder" in c for c in calls)

    def test_members_empty(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from klangk.cli.client import Workspace

        client = MagicMock()
        client.resolve_workspace.return_value = Workspace(
            id="ws-1", name="my-ws", created_at="2025-01-01"
        )
        roles_resp = MagicMock()
        roles_resp.status_code = 200
        roles_resp.json.return_value = [
            {"role": "coders", "members": []},
        ]
        client.get.return_value = roles_resp
        monkeypatch.setattr(main, "_client", lambda: client)

        calls = []
        monkeypatch.setattr("typer.echo", lambda s: calls.append(s))
        main.members("my-ws")
        assert any("No shared" in c for c in calls)

    def test_members_not_found(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.members("nope")

    def test_share_workspace_command(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.add_workspace_member.return_value = {
            "email": "alice@test.com",
            "role": "coders",
        }
        monkeypatch.setattr(main, "_client", lambda: client)

        calls = []
        monkeypatch.setattr("typer.echo", lambda s: calls.append(s))
        main.share_workspace("my-ws", "alice@test.com", role="coder")
        assert any("alice@test.com" in c for c in calls)
        assert any("coder" in c for c in calls)
        client.add_workspace_member.assert_called_once_with(
            "my-ws", "alice@test.com", role="coders"
        )

    def test_share_workspace_with_role(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.add_workspace_member.return_value = {
            "email": "alice@test.com",
            "role": "spectators",
        }
        monkeypatch.setattr(main, "_client", lambda: client)

        calls = []
        monkeypatch.setattr("typer.echo", lambda s: calls.append(s))
        main.share_workspace("my-ws", "alice@test.com", role="spectator")
        client.add_workspace_member.assert_called_once_with(
            "my-ws", "alice@test.com", role="spectators"
        )

    def test_share_workspace_invalid_role(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        monkeypatch.setattr(main, "_client", lambda: MagicMock())

        with pytest.raises(typer.Exit):
            main.share_workspace("my-ws", "a@b.com", role="admin")

    def test_share_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.add_workspace_member.side_effect = WorkspaceNotFoundError(
            "nope"
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.share_workspace("nope", "alice@test.com", role="coder")

    def test_unshare_workspace_command(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        monkeypatch.setattr(main, "_client", lambda: client)

        calls = []
        monkeypatch.setattr("typer.echo", lambda s: calls.append(s))
        main.unshare_workspace("my-ws", "alice@test.com")
        assert any("alice@test.com" in c for c in calls)

    def test_unshare_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.remove_workspace_member.side_effect = WorkspaceNotFoundError(
            "not a member"
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.unshare_workspace("my-ws", "nobody@test.com")

    def test_restart_workspace(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo"):
            main.restart("my-ws")
        client.restart_workspace.assert_called_once_with("my-ws")

    def test_restart_workspace_not_found(self, logged_in_cfg, monkeypatch):
        import typer

        from klangk.cli.client import WorkspaceNotFoundError
        from klangk.cli import main

        client = MagicMock()
        client.restart_workspace.side_effect = WorkspaceNotFoundError("nope")
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.restart("nope")

    def test_shell_requires_auth(self, tmp_path, monkeypatch):
        import typer
        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        # State with active server but no token
        state = CLIState(active_server="http://localhost:8995")
        state.save()

        with pytest.raises(typer.Exit):
            main.shell(None)

    def test_status_not_logged_in(self, tmp_path, monkeypatch, capsys):
        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        state = CLIState(active_server="http://custom:1234")
        state.save()

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "custom:1234" in output
        assert "not_logged_in" in output

    def test_status_logged_in(self, logged_in_cfg, capsys):
        from klangk.cli import main

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "test@example.com" in output
        assert "logged_in" in output

    def test_status_rich_logged_in(self, logged_in_cfg):
        from io import StringIO

        from rich.console import Console

        from klangk.cli import main

        buf = StringIO()
        with patch.object(
            main,
            "Console",
            return_value=Console(file=buf, force_terminal=True),
        ):
            main.status(plain=False)
        output = buf.getvalue()
        assert "test@example.com" in output
        assert "logged in" in output

    def test_status_rich_not_logged_in(self, tmp_path, monkeypatch):
        from io import StringIO

        from rich.console import Console

        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        state = CLIState(active_server="http://localhost:8995")
        state.save()

        buf = StringIO()
        with patch.object(
            main,
            "Console",
            return_value=Console(file=buf, force_terminal=True),
        ):
            main.status(plain=False)
        output = buf.getvalue()
        assert "not logged in" in output

    def test_status_plain_logged_in(self, logged_in_cfg, capsys):
        from klangk.cli import main

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "server=http://localhost:8995" in output
        assert "user=test@example.com" in output
        assert "status=logged_in" in output

    def test_status_plain_not_logged_in(self, tmp_path, monkeypatch, capsys):
        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        state = CLIState(active_server="http://custom:1234")
        state.save()

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "server=http://custom:1234" in output
        assert "status=not_logged_in" in output
        assert "user=" not in output

    def test_logout_command(self, logged_in_cfg):
        from klangk.cli import main

        with patch(
            "klangk.cli.transport.httpx.request",
            return_value=MagicMock(status_code=200),
        ):
            main.logout(server=None)
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") is None

    def test_logout_with_server_arg(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        with patch(
            "klangk.cli.transport.httpx.request",
            return_value=MagicMock(status_code=200),
        ):
            main.logout(server="http://localhost:8995")
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") is None

    def test_logout_no_active_server_exits(self, tmp_path, monkeypatch):
        from klangk.cli import main

        config_path = tmp_path / "klangk.yaml"
        state_path = tmp_path / "klangk-state.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        config_path.write_text("")
        CLIState().save()
        # Ensure no co-located klangkd UDS is picked up by the default
        # fallback (#1676) so the exit path is exercised deterministically.
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("KLANGK_STATE_DIR", raising=False)

        with pytest.raises(typer.Exit):
            main.logout(server=None)

    def test_logout_network_error_does_not_propagate(self, logged_in_cfg):
        from klangk.cli import main

        with patch(
            "klangk.cli.transport.httpx.request",
            side_effect=httpx.ConnectError("no route"),
        ):
            main.logout(server=None)  # must not raise

    def test_shell_with_single_workspace_auto_selects(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="solo-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.list_workspaces.return_value = [ws]
        client.resolve_workspace.return_value = ws

        async def fake_shell(*args, **kwargs):
            pass

        with patch.object(main, "_client", return_value=client):
            with patch.object(main, "ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                with patch("termios.tcgetattr", return_value=None):
                    main.shell(None)

        client.resolve_workspace.assert_not_called()  # was auto-selected

    def test_shell_no_workspaces_exits(self, logged_in_cfg, monkeypatch):
        import typer
        from klangk.cli import main

        client = MagicMock()
        client.list_workspaces.return_value = []
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.shell(None)

    def test_shell_multiple_workspaces_prompts(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        ws1 = Workspace(
            id="id1" + "0" * 52, name="ws-a", created_at="2025-01-01T00:00:00Z"
        )
        ws2 = Workspace(
            id="id2" + "0" * 52, name="ws-b", created_at="2025-01-01T00:00:00Z"
        )
        client = MagicMock()
        client.list_workspaces.return_value = [ws1, ws2]

        async def fake_shell(*args, **kwargs):
            pass

        with patch.object(main, "_client", return_value=client):
            with patch.object(main, "ws_shell", fake_shell):
                with patch("builtins.input", return_value="1"):  # select first
                    with patch("termios.tcgetattr", return_value=None):
                        main.shell(None)

    def test_shell_by_name(self, logged_in_cfg, monkeypatch, reset_env):
        from klangk.cli import main

        ws = Workspace(
            id="target" + "0" * 52,
            name="target-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        async def fake_shell(*args, **kwargs):
            pass

        with patch.object(main, "_client", return_value=client):
            with patch.object(main, "ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                with patch("termios.tcgetattr", return_value=None):
                    main.shell("target-ws")

        client.resolve_workspace.assert_called_once_with("target-ws")

    def test_shell_with_terminal_arg(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangk.cli import main

        ws = Workspace(
            id="target" + "0" * 52,
            name="target-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        captured_kwargs = {}

        async def fake_shell(*args, **kwargs):
            captured_kwargs.update(kwargs)

        with patch.object(main, "_client", return_value=client):
            with patch.object(main, "ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                with patch("termios.tcgetattr", return_value=None):
                    main.shell("target-ws", "build")

        assert captured_kwargs["window"] == "build"

    def test_shell_forward_agent_config_true(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        """forward_agent in config enables forwarding when no CLI flag."""
        from klangk.cli import main

        ws = Workspace(
            id="target" + "0" * 52,
            name="target-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        captured_kwargs = {}

        async def fake_shell(*args, **kwargs):
            captured_kwargs.update(kwargs)

        # Patch _cfg to return a config with forward_agent=True
        cfg = CLIConfig(forward_agent=True)
        with (
            patch.object(main, "_cfg", return_value=cfg),
            patch.object(main, "_client", return_value=client),
            patch.object(main, "ws_shell", fake_shell),
        ):
            os.environ["TERM"] = "xterm-256color"
            os.environ["SSH_AUTH_SOCK"] = "/tmp/agent.sock"
            with patch("termios.tcgetattr", return_value=None):
                main.shell("target-ws")

        assert captured_kwargs["forward_agent"] is True

    def test_shell_forward_agent_flag_overrides_config(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        """--no-forward-agent (False) overrides config forward_agent=True."""
        from klangk.cli import main

        ws = Workspace(
            id="target" + "0" * 52,
            name="target-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        captured_kwargs = {}

        async def fake_shell(*args, **kwargs):
            captured_kwargs.update(kwargs)

        cfg = CLIConfig(forward_agent=True)
        with (
            patch.object(main, "_cfg", return_value=cfg),
            patch.object(main, "_client", return_value=client),
            patch.object(main, "ws_shell", fake_shell),
        ):
            os.environ["TERM"] = "xterm-256color"
            os.environ["SSH_AUTH_SOCK"] = "/tmp/agent.sock"
            with patch("termios.tcgetattr", return_value=None):
                main.shell("target-ws", forward_agent=False)

        assert captured_kwargs["forward_agent"] is False

    def test_shell_forward_agent_per_server(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        """Per-server forward-agent in config is used."""
        from klangk.cli import main

        ws = Workspace(
            id="target" + "0" * 52,
            name="target-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        captured_kwargs = {}

        async def fake_shell(*args, **kwargs):
            captured_kwargs.update(kwargs)

        cfg = CLIConfig(
            forward_agent=False,
            servers={
                "local": ServerEntry(
                    url="http://localhost:8995", forward_agent=True
                ),
            },
        )
        with (
            patch.object(main, "_cfg", return_value=cfg),
            patch.object(main, "_client", return_value=client),
            patch.object(main, "ws_shell", fake_shell),
        ):
            os.environ["TERM"] = "xterm-256color"
            os.environ["SSH_AUTH_SOCK"] = "/tmp/agent.sock"
            with patch("termios.tcgetattr", return_value=None):
                main.shell("target-ws")

        assert captured_kwargs["forward_agent"] is True

    def test_ws_max_size_per_server(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        cfg = CLIConfig(
            servers={
                "local": ServerEntry(
                    url="http://localhost:8995", ws_max_size=999
                ),
            },
        )
        with patch.object(main, "_cfg", return_value=cfg):
            assert main.ws_max_size() == 999

    def test_terminals_command(self, logged_in_cfg, monkeypatch, reset_env):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "container_ready"}),
            json.dumps(
                {
                    "type": "shared_terminals",
                    "terminals": [
                        {
                            "user_id": "u1",
                            "handle": "alice",
                            "window_name": "dev",
                        },
                    ],
                }
            ),
            json.dumps(
                {"type": "event", "event": {"name": "container_ready"}}
            ),
            json.dumps({"type": "terminal_started"}),
            json.dumps(
                {
                    "type": "terminal_windows",
                    "windows": [
                        {"id": "@0", "index": 0, "name": "1", "active": True},
                        {
                            "id": "@1",
                            "index": 1,
                            "name": "build",
                            "active": False,
                        },
                    ],
                }
            ),
        ]

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=messages)
        mock_ws.send = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(main, "_client", return_value=client),
            patch(
                "klangk.cli.transport.websockets.connect", return_value=mock_ws
            ),
        ):
            os.environ["TERM"] = "xterm-256color"
            main.terminals("my-ws")

    def test_share_command(self, logged_in_cfg, monkeypatch, reset_env):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "container_ready"}),
            json.dumps(
                {"type": "event", "event": {"name": "container_ready"}}
            ),
            json.dumps({"type": "terminal_started"}),
            json.dumps(
                {
                    "type": "terminal_windows",
                    "windows": [
                        {"id": "@0", "index": 0, "name": "1", "active": True},
                        {
                            "id": "@1",
                            "index": 1,
                            "name": "build",
                            "active": False,
                        },
                    ],
                }
            ),
            json.dumps(
                {
                    "type": "shared_terminals",
                    "terminals": [
                        {
                            "user_id": "u1",
                            "handle": "me",
                            "window_name": "build",
                        },
                    ],
                }
            ),
        ]

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=messages)
        mock_ws.send = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(main, "_client", return_value=client),
            patch(
                "klangk.cli.transport.websockets.connect", return_value=mock_ws
            ),
        ):
            os.environ["TERM"] = "xterm-256color"
            main.share_terminal("my-ws", "build")

        # Should have sent share_window command
        sent = [
            json.loads(c[0][0])
            for c in mock_ws.send.call_args_list
            if isinstance(c[0][0], str)
        ]
        assert any(s.get("cmd") == "share_window" for s in sent)

    def test_unshare_command(self, logged_in_cfg, monkeypatch, reset_env):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "container_ready"}),
            json.dumps(
                {"type": "event", "event": {"name": "container_ready"}}
            ),
            json.dumps({"type": "terminal_started"}),
            json.dumps(
                {
                    "type": "terminal_windows",
                    "windows": [
                        {"id": "@0", "index": 0, "name": "1", "active": True},
                        {
                            "id": "@1",
                            "index": 1,
                            "name": "build",
                            "active": False,
                        },
                    ],
                }
            ),
            json.dumps({"type": "shared_terminals", "terminals": []}),
        ]

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=messages)
        mock_ws.send = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(main, "_client", return_value=client),
            patch(
                "klangk.cli.transport.websockets.connect", return_value=mock_ws
            ),
        ):
            os.environ["TERM"] = "xterm-256color"
            main.unshare_terminal("my-ws", "build")

        sent = [
            json.loads(c[0][0])
            for c in mock_ws.send.call_args_list
            if isinstance(c[0][0], str)
        ]
        assert any(s.get("cmd") == "unshare_window" for s in sent)

    def test_share_terminal_not_found(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "container_ready"}),
            json.dumps(
                {"type": "event", "event": {"name": "container_ready"}}
            ),
            json.dumps({"type": "terminal_started"}),
            json.dumps(
                {
                    "type": "terminal_windows",
                    "windows": [
                        {"id": "@0", "index": 0, "name": "1", "active": True},
                    ],
                }
            ),
        ]

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=messages)
        mock_ws.send = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(main, "_client", return_value=client),
            patch(
                "klangk.cli.transport.websockets.connect", return_value=mock_ws
            ),
        ):
            os.environ["TERM"] = "xterm-256color"
            with pytest.raises(typer.Exit):
                main.share_terminal("my-ws", "nonexistent")

    def test_terminals_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")

        with patch.object(main, "_client", return_value=client):
            with pytest.raises(typer.Exit):
                main.terminals("nope")

    def test_terminals_connection_failure(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "error", "message": "fail"}),
        ]

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=messages)
        mock_ws.send = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(main, "_client", return_value=client),
            patch(
                "klangk.cli.transport.websockets.connect", return_value=mock_ws
            ),
        ):
            os.environ["TERM"] = "xterm-256color"
            with pytest.raises(ConnectionError):
                main.terminals("my-ws")

    def test_share_connection_failure(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "error", "message": "fail"}),
        ]

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=messages)
        mock_ws.send = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(main, "_client", return_value=client),
            patch(
                "klangk.cli.transport.websockets.connect", return_value=mock_ws
            ),
        ):
            os.environ["TERM"] = "xterm-256color"
            with pytest.raises(ConnectionError):
                main.share_terminal("my-ws", "build")

    def test_unshare_connection_failure(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "error", "message": "fail"}),
        ]

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=messages)
        mock_ws.send = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(main, "_client", return_value=client),
            patch(
                "klangk.cli.transport.websockets.connect", return_value=mock_ws
            ),
        ):
            os.environ["TERM"] = "xterm-256color"
            with pytest.raises(ConnectionError):
                main.unshare_terminal("my-ws", "build")

    def test_unshare_terminal_not_found(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "container_ready"}),
            json.dumps(
                {"type": "event", "event": {"name": "container_ready"}}
            ),
            json.dumps({"type": "terminal_started"}),
            json.dumps(
                {
                    "type": "terminal_windows",
                    "windows": [
                        {"id": "@0", "index": 0, "name": "1", "active": True},
                    ],
                }
            ),
        ]

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=messages)
        mock_ws.send = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=False)

        with (
            patch.object(main, "_client", return_value=client),
            patch(
                "klangk.cli.transport.websockets.connect", return_value=mock_ws
            ),
        ):
            os.environ["TERM"] = "xterm-256color"
            with pytest.raises(typer.Exit):
                main.unshare_terminal("my-ws", "nonexistent")

    def test_edit_with_flags(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            image="klangk",
            service_command="klangk-pi",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app,
                ["edit", "my-ws", "--name", "renamed", "--command", "pi"],
            )
            assert result.exit_code == 0

        call_args = client.put.call_args
        body = call_args[1]["json"]
        assert body["name"] == "renamed"
        assert body["service_command"] == "pi"
        assert "image" not in body  # not provided, not sent

    def test_edit_with_health_check_flag(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            image="klangk",
            service_command="klangk-pi",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app,
                ["edit", "my-ws", "--health-check", "curl -sf http://x/h"],
            )
            assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["health_check"] == "curl -sf http://x/h"

    def test_edit_clear_command(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            service_command="klangk-pi",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["edit", "my-ws", "--command", ""]
            )
            assert result.exit_code == 0

        call_args = client.put.call_args
        assert call_args[1]["json"]["service_command"] is None

    def test_edit_interactive(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            image="klangk",
            service_command="klangk-pi",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep name, keep image, change command, skip add mount, skip add env
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input", side_effect=["", "", "pi", "", "", ""]
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        call_args = client.put.call_args
        body = call_args[1]["json"]
        assert "name" not in body  # kept current
        assert "image" not in body  # kept current
        assert body["service_command"] == "pi"

    def test_edit_interactive_health_check(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            image="klangk",
            service_command="klangk-pi",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep name/image/command, set health check, skip mounts/env
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "curl -sf http://x/h", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["health_check"] == "curl -sf http://x/h"
        assert "service_command" not in body  # kept current

    def test_edit_interactive_change_all(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            image="klangk",
            service_command="klangk-pi",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # change name, image, command; skip add mount, (no mounts to remove)
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["renamed", "klangk-custom", "pi", "", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["name"] == "renamed"
        assert body["image"] == "klangk-custom"
        assert body["service_command"] == "pi"

    def test_edit_interactive_add_mount(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            mounts=None,
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep name/image/command; add a mount, then skip add, (now has mount) skip remove
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "", "/host:/container", "", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["mounts"] == ["/host:/container"]

    def test_edit_interactive_remove_mount(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            mounts=["/a:/b", "/c:/d"],
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep name/image/command; skip add, remove mount 1; skip add, skip remove
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "", "", "1", "", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["mounts"] == ["/c:/d"]

    def test_edit_interactive_add_and_remove_mount(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            mounts=["/old:/old"],
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep all; add /new:/new (loops back), skip add, remove 1 (/old:/old),
        # skip add, skip remove
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "", "/new:/new", "", "1", "", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["mounts"] == ["/new:/new"]

    def test_edit_interactive_invalid_remove_number(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            mounts=["/a:/b"],
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep all; skip add, bad number "99" (loops), skip add, "abc" (loops),
        # skip add, skip remove
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "", "", "99", "", "abc", "", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        # No mount changes (bad input was rejected), so mounts not in body
        client.put.assert_not_called()  # only "no changes" path

    def test_edit_interactive_remove_all_mounts(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            mounts=["/a:/b"],
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep all; skip add, remove 1; skip add (no mounts left, so no remove prompt)
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "", "", "1", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["mounts"] is None

    def test_edit_interactive_invalid_mount_rejected(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            mounts=None,
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep all; try invalid mount "bad", then valid "/a:/b", skip add, (no remove)
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "", "bad", "/a:/b", "", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0
                assert "Invalid mount" in result.stdout

        body = client.put.call_args[1]["json"]
        assert body["mounts"] == ["/a:/b"]

    def test_create_invalid_mount_flag(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        monkeypatch.setattr(main, "_client", lambda: MagicMock())

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            main.app, ["create", "ws", "--mount", "not-valid"]
        )
        assert result.exit_code == 1

    def test_edit_invalid_mount_flag(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["edit", "my-ws", "--mount", "nope"])
        assert result.exit_code == 1

    def test_edit_with_image_flag(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["edit", "my-ws", "--image", "klangk-custom"]
            )
            assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["image"] == "klangk-custom"

    def test_edit_with_mount_flag(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app,
                [
                    "edit",
                    "my-ws",
                    "--mount",
                    "/home/me/src:/work/src",
                    "--mount",
                    "/data:/mnt/data:ro",
                ],
            )
            assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["mounts"] == [
            "/home/me/src:/work/src",
            "/data:/mnt/data:ro",
        ]

    def test_edit_interactive_no_changes(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        # keep name, image, command; skip add mount (no mounts, no remove prompt)
        with patch.object(main, "_client", return_value=client):
            with patch("builtins.input", side_effect=["", "", "", "", "", ""]):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0
                assert "No changes" in result.stdout

        client.put.assert_not_called()

    def test_edit_interactive_add_env(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep all; skip mounts; add env FOO=bar, skip add env
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "", "", "FOO=bar", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["env"] == {"FOO": "bar"}

    def test_edit_interactive_invalid_env_rejected(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep all; skip mounts; try "bad" (no =), then "A=1", skip add, skip remove
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "", "", "bad", "A=1", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0
                assert "KEY=VALUE" in result.stdout

        body = client.put.call_args[1]["json"]
        assert body["env"] == {"A": "1"}

    def test_create_with_env_flag(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="new-id", name="ws", created_at="2025-01-01T00:00:00Z"
        )
        client = MagicMock()
        client.create_workspace.return_value = ws
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            main.app, ["create", "ws", "--env", "FOO=bar", "--env", "X=1"]
        )
        assert result.exit_code == 0
        client.create_workspace.assert_called_once_with(
            "ws",
            image=None,
            service_command=None,
            auto_start=False,
            mounts=None,
            env={"FOO": "bar", "X": "1"},
            health_check=None,
        )

    def test_create_with_command_flag(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="new-id", name="ws", created_at="2025-01-01T00:00:00Z"
        )
        client = MagicMock()
        client.create_workspace.return_value = ws
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["create", "ws", "-c", "npm run dev"])
        assert result.exit_code == 0
        client.create_workspace.assert_called_once_with(
            "ws",
            image=None,
            service_command="npm run dev",
            auto_start=False,
            mounts=None,
            env=None,
            health_check=None,
        )

    def test_create_with_invalid_env_flag(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        monkeypatch.setattr(main, "_client", lambda: MagicMock())

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            main.app, ["create", "ws", "--env", "NOEQUALSSIGN"]
        )
        assert result.exit_code == 1

    def test_edit_interactive_remove_env(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            env={"FOO": "bar", "X": "1"},
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep all; skip mounts; skip add env, remove env 1; skip add, skip remove
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "", "", "", "1", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["env"] == {"X": "1"}

    def test_edit_interactive_invalid_env_remove_number(
        self, logged_in_cfg, monkeypatch
    ):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            env={"A": "1"},
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        # keep all; skip mounts; skip add env, bad number "99" (loops),
        # skip add env, "abc" (loops), skip add, skip remove
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["", "", "", "", "", "", "99", "", "abc", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        client.put.assert_not_called()

    def test_edit_with_env_flag(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["edit", "my-ws", "--env", "A=1"])
            assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["env"] == {"A": "1"}

    def test_edit_with_auto_start_flag(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["edit", "my-ws", "--auto-start"])
            assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["auto_start"] is True

    def test_dup_workspace(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="orig",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.post.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"id": "new-id", "name": "copy"}),
        )

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["dup", "orig", "copy"])
            assert result.exit_code == 0
            assert "copy" in result.stdout

    def test_dup_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["dup", "nope", "copy"])
            assert result.exit_code == 1

    def test_dup_workspace_conflict(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="orig",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.post.return_value = MagicMock(status_code=409)

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["dup", "orig", "taken"])
            assert result.exit_code == 1

    def test_dup_workspace_404(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="orig",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.post.return_value = MagicMock(status_code=404)

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["dup", "orig", "copy"])
            assert result.exit_code == 1

    def test_edit_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["edit", "nope", "--command", "pi"]
            )
            assert result.exit_code == 1

    def test_edit_404_from_server(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=404)

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["edit", "my-ws", "--command", "pi"]
            )
            assert result.exit_code == 1

    def test_exec_runs_command(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from klangk.cli.client import Workspace
        from typer.testing import CliRunner

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        with patch.object(main, "_client", return_value=client):
            with patch.object(
                main, "ws_exec", AsyncMock(return_value=0)
            ) as mock_exec:
                runner = CliRunner()
                result = runner.invoke(
                    main.app, ["exec", "my-ws", "echo", "hi"]
                )
        assert result.exit_code == 0
        # #1041: default exec runs as a login shell so ~/.profile is
        # sourced (login=True); the command list is passed through.
        assert mock_exec.call_args.kwargs["login"] is True
        assert mock_exec.call_args.args[3] == ["echo", "hi"]

    def test_exec_raw_flag_passes_login_false(self, logged_in_cfg):
        """#1041: ``--raw`` opts out of the login shell so the command
        runs as raw argv -- used by programmatic transports (rsync)."""
        from klangk.cli import main
        from klangk.cli.client import Workspace
        from typer.testing import CliRunner

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        with patch.object(main, "_client", return_value=client):
            with patch.object(
                main, "ws_exec", AsyncMock(return_value=0)
            ) as mock_exec:
                runner = CliRunner()
                result = runner.invoke(
                    main.app, ["exec", "--raw", "my-ws", "echo", "hi"]
                )
        assert result.exit_code == 0
        assert mock_exec.call_args.kwargs["login"] is False

    def test_exec_strips_leading_dashdash_separator(self, logged_in_cfg):
        """With allow_extra_args + allow_interspersed_args=False, Click
        does NOT consume the ``--`` end-of-options separator -- it lands
        in ctx.args verbatim. exec_cmd strips a single leading ``--`` so
        the conventional ``klangk exec ws -- echo hi`` works instead of
        trying to run ``--`` as a command.
        """
        from klangk.cli import main
        from klangk.cli.client import Workspace
        from typer.testing import CliRunner

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        with patch.object(main, "_client", return_value=client):
            with patch.object(
                main, "ws_exec", AsyncMock(return_value=0)
            ) as mock_exec:
                runner = CliRunner()
                result = runner.invoke(
                    main.app, ["exec", "my-ws", "--", "echo", "hi"]
                )
        assert result.exit_code == 0
        # the leading ``--`` is stripped; the command is echo hi.
        assert mock_exec.call_args.args[3] == ["echo", "hi"]

    def test_exec_no_command(self, logged_in_cfg):
        from klangk.cli import main

        ctx = MagicMock()
        ctx.args = []
        with pytest.raises(typer.Exit) as exc_info:
            main.exec_cmd(ctx, workspace="my-ws")
        assert exc_info.value.exit_code == 1

    def test_exec_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from klangk.cli.client import WorkspaceNotFoundError

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")
        monkeypatch.setattr(main, "_client", lambda: client)

        ctx = MagicMock()
        ctx.args = ["ls"]
        with pytest.raises(typer.Exit) as exc_info:
            main.exec_cmd(ctx, workspace="nope")
        assert exc_info.value.exit_code == 1

    def test_sync_runs_rsync(self, logged_in_cfg):
        from klangk.cli import main

        ctx = MagicMock()
        ctx.args = []
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"):
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                with pytest.raises(typer.Exit) as exc_info:
                    main.sync(ctx, src="/tmp/foo", dest="ws:/work/foo")
        assert exc_info.value.exit_code == 0
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/rsync"
        assert "-avz" in cmd
        assert "/tmp/foo" in cmd
        assert "ws:/work/foo" in cmd
        # #1041: sync uses ``exec --raw`` as the rsync transport so the
        # remote command runs raw (no login shell) -- a ~/.profile that
        # prints would otherwise corrupt the binary rsync stream.
        assert "klangk exec --raw" in " ".join(cmd)

    def test_sync_no_rsync(self, logged_in_cfg):
        from klangk.cli import main

        def which_no_rsync(name):
            return "/usr/bin/klangk" if name == "klangk" else None

        ctx = MagicMock()
        ctx.args = []
        with patch("shutil.which", side_effect=which_no_rsync):
            with pytest.raises(typer.Exit) as exc_info:
                main.sync(ctx, src="/tmp/foo", dest="ws:/work/foo")
        assert exc_info.value.exit_code == 1

    def test_sync_passes_extra_args(self, logged_in_cfg):
        from klangk.cli import main

        ctx = MagicMock()
        ctx.args = ["--delete", "--exclude=.git"]
        mock_result = MagicMock()
        mock_result.returncode = 0
        with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"):
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                with pytest.raises(typer.Exit):
                    main.sync(ctx, src="/tmp/foo", dest="ws:/work/foo")
        cmd = mock_run.call_args[0][0]
        assert "--delete" in cmd
        assert "--exclude=.git" in cmd

    def test_sync_rsync_failure(self, logged_in_cfg):
        from klangk.cli import main

        ctx = MagicMock()
        ctx.args = []
        mock_result = MagicMock()
        mock_result.returncode = 23
        with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"):
            with patch("subprocess.run", return_value=mock_result):
                with pytest.raises(typer.Exit) as exc_info:
                    main.sync(ctx, src="/tmp/foo", dest="ws:/work/foo")
        assert exc_info.value.exit_code == 23


class TestVolumes:
    def test_volumes_ls(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value=[
                    {"name": "vol-1", "created": "2026-01-01T00:00:00Z"},
                ]
            ),
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "ls"])
        assert result.exit_code == 0
        assert "vol-1" in result.stdout

    def test_volumes_ls_empty(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=[]),
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "ls"])
        assert result.exit_code == 0
        assert "No volumes" in result.stdout

    def test_volumes_ls_plain(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value=[{"name": "vol-1", "created": "2026-01-01"}]
            ),
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "ls", "--plain"])
        assert result.exit_code == 0
        assert "vol-1" in result.stdout

    def test_volumes_create(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.post.return_value = MagicMock(status_code=200)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "create", "new-vol"])
        assert result.exit_code == 0
        assert "Created" in result.stdout

    def test_volumes_create_duplicate(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.post.return_value = MagicMock(status_code=409)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "create", "dup-vol"])
        assert result.exit_code == 1

    def test_volumes_rm(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.delete.return_value = MagicMock(status_code=200)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "rm", "old-vol"])
        assert result.exit_code == 0
        assert "Deleted" in result.stdout

    def test_volumes_rm_not_found(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.delete.return_value = MagicMock(status_code=404)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "rm", "nope"])
        assert result.exit_code == 1

    def test_volumes_rm_in_use(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.delete.return_value = MagicMock(status_code=409)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "rm", "busy"])
        assert result.exit_code == 1

    def test_volumes_rm_permission_denied(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.delete.return_value = MagicMock(status_code=403)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "rm", "not-mine"])
        assert result.exit_code == 1


class TestExportImportCLI:
    def test_export_success(self, logged_in_cfg, monkeypatch, tmp_path):
        from klangk.cli import main

        ws = Workspace(
            id="ws-export-id", name="my-ws", created_at="2025-01-01"
        )

        def _fake_export(ws_id, out, on_progress=None):
            if on_progress:
                on_progress(100, 200)
                on_progress(200, 200)

        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.export_workspace.side_effect = _fake_export
        monkeypatch.setattr(main, "_client", lambda: client)

        out = tmp_path / "out.tar.gz"
        main.export_workspace(name="my-ws", output=out)
        client.export_workspace.assert_called_once()
        args = client.export_workspace.call_args
        assert args[0][0] == "ws-export-id"
        assert args[0][1] == out

    def test_export_default_filename(self, logged_in_cfg, monkeypatch):
        from pathlib import Path
        from klangk.cli import main

        ws = Workspace(id="ws-exp-id", name="test-ws", created_at="2025-01-01")

        def _fake_export(ws_id, out, on_progress=None):
            if on_progress:
                on_progress(50, None)

        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.export_workspace.side_effect = _fake_export
        monkeypatch.setattr(main, "_client", lambda: client)

        main.export_workspace(name="test-ws", output=None)
        client.export_workspace.assert_called_once()
        args = client.export_workspace.call_args
        assert args[0][0] == "ws-exp-id"
        assert args[0][1] == Path("test-ws.tar.gz")

    def test_export_avoids_overwrite(
        self, logged_in_cfg, monkeypatch, tmp_path, monkeypatch_cwd=None
    ):
        from pathlib import Path
        from klangk.cli import main

        ws = Workspace(id="ws-ow-id", name="ow-ws", created_at="2025-01-01")

        def _fake_export(ws_id, out, on_progress=None):
            pass

        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.export_workspace.side_effect = _fake_export
        monkeypatch.setattr(main, "_client", lambda: client)

        # Create existing files in cwd
        monkeypatch.chdir(tmp_path)
        (tmp_path / "ow-ws.tar.gz").write_bytes(b"existing")
        (tmp_path / "ow-ws-1.tar.gz").write_bytes(b"existing2")

        main.export_workspace(name="ow-ws", output=None)
        args = client.export_workspace.call_args
        assert args[0][1] == Path("ow-ws-2.tar.gz")

    def test_export_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.export_workspace(name="nope", output=None)

    def test_export_http_error(self, logged_in_cfg, monkeypatch, tmp_path):
        from klangk.cli import main

        ws = Workspace(id="ws-err-id", name="err-ws", created_at="2025-01-01")
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        resp = MagicMock()
        resp.text = "forbidden"
        resp.status_code = 403
        client.export_workspace.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.export_workspace(name="err-ws", output=tmp_path / "o.tar.gz")

    def test_import_success(self, logged_in_cfg, monkeypatch, tmp_path):
        from klangk.cli import main

        ws = Workspace(
            id="ws-imp-id", name="imported", created_at="2025-01-01"
        )

        def _fake_import(a, name=None, on_progress=None):
            if on_progress:
                on_progress(2, 4)
                on_progress(4, 4)
            return ws

        client = MagicMock()
        client.import_workspace.side_effect = _fake_import
        monkeypatch.setattr(main, "_client", lambda: client)

        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"fake")
        main.import_workspace(archive=archive, name="imported")
        client.import_workspace.assert_called_once()
        args = client.import_workspace.call_args
        assert args[0][0] == archive
        assert args[1]["name"] == "imported"

    def test_import_file_not_found(self, logged_in_cfg, monkeypatch, tmp_path):
        from klangk.cli import main

        with pytest.raises(typer.Exit):
            main.import_workspace(
                archive=tmp_path / "nonexistent.tar.gz", name=None
            )

    def test_import_http_error(self, logged_in_cfg, monkeypatch, tmp_path):
        from klangk.cli import main

        resp = MagicMock()
        resp.text = "conflict"
        resp.status_code = 409
        client = MagicMock()
        client.import_workspace.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"fake")
        with pytest.raises(typer.Exit):
            main.import_workspace(archive=archive, name="dup")


class TestInviteCLI:
    def test_invite_success(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "id": "inv-1",
                "email": "a@b.com",
                "status": "pending",
            },
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            main.app, ["admin", "invitations", "send", "a@b.com"]
        )
        assert result.exit_code == 0
        assert "a@b.com" in result.stdout

    def test_invite_error(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.post.return_value = MagicMock(
            status_code=400,
            json=lambda: {"detail": "already exists"},
            text="already exists",
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            main.app, ["admin", "invitations", "send", "a@b.com"]
        )
        assert result.exit_code == 1

    def test_invite_forbidden(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.post.return_value = MagicMock(
            status_code=403,
            json=lambda: {"detail": "Invitations are disabled"},
            text="Invitations are disabled",
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            main.app, ["admin", "invitations", "send", "a@b.com"]
        )
        assert result.exit_code == 1

    def test_invitations_list(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "invitations": [
                    {
                        "email": "a@b.com",
                        "status": "pending",
                        "invited_by_email": "admin@x.com",
                        "created_at": "2026-01-01 00:00:00",
                    }
                ],
                "page": 1,
                "page_size": 200,
                "total": 1,
                "pending_count": 1,
            },
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["admin", "invitations", "ls"])
        assert result.exit_code == 0
        assert "a@b.com" in result.stdout

    def test_invitations_empty(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "invitations": [],
                "page": 1,
                "page_size": 200,
                "total": 0,
                "pending_count": 0,
            },
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["admin", "invitations", "ls"])
        assert result.exit_code == 0
        assert "No invitations" in result.stdout

    def test_invitations_list_error(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        resp = MagicMock(status_code=403)
        resp.json.return_value = {"detail": "Permission denied"}
        resp.text = "Permission denied"
        resp.headers = {"content-type": "application/json"}
        client.get.return_value = resp
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["admin", "invitations", "ls"])
        assert result.exit_code == 1


class TestWorkspaceStatus:
    def test_stopped_when_not_running(self):
        from klangk.cli.main import workspace_status

        ws = Workspace(id="x", name="n", created_at="2025-01-01T00:00:00Z")
        label, markup = workspace_status(ws)
        assert label == "stopped"
        assert "dim" in markup

    def test_running_when_no_health_check_configured(self):
        from klangk.cli.main import workspace_status

        ws = Workspace(
            id="x", name="n", created_at="2025-01-01T00:00:00Z", running=True
        )
        # No health_check configured -> never probed -> must not show
        # "starting" (which would imply a pending poll that never comes).
        label, markup = workspace_status(ws)
        assert label == "running"
        assert "green" in markup

    def test_healthy(self):
        from klangk.cli.main import workspace_status

        ws = Workspace(
            id="x",
            name="n",
            created_at="2025-01-01T00:00:00Z",
            running=True,
            health_check="/path/to/check.sh",
            health="healthy",
        )
        label, markup = workspace_status(ws)
        assert label == "healthy"
        assert "green" in markup

    def test_unhealthy(self):
        from klangk.cli.main import workspace_status

        ws = Workspace(
            id="x",
            name="n",
            created_at="2025-01-01T00:00:00Z",
            running=True,
            health_check="/path/to/check.sh",
            health="unhealthy",
        )
        label, markup = workspace_status(ws)
        assert label == "unhealthy"
        assert "red" in markup

    def test_starting_when_running_but_no_health(self):
        from klangk.cli.main import workspace_status

        ws = Workspace(
            id="x",
            name="n",
            created_at="2025-01-01T00:00:00Z",
            running=True,
            health_check="/path/to/check.sh",
        )
        label, markup = workspace_status(ws)
        assert label == "starting"
        assert "yellow" in markup


class TestShortId:
    def test_long_id_is_truncated(self):
        from klangk.cli.main import short_id

        assert short_id("abcdefgh") == "abc…fgh"

    def test_seven_char_id_returned_unchanged(self):
        from klangk.cli.main import short_id

        assert short_id("abcdefg") == "abcdefg"

    def test_short_id_returned_unchanged(self):
        from klangk.cli.main import short_id

        assert short_id("abc") == "abc"


class TestResolveForwardAgent:
    def test_flag_true(self, monkeypatch):
        from klangk.cli.main import resolve_forward_agent

        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        assert resolve_forward_agent(True) is True

    def test_flag_false_overrides_config(self):
        from klangk.cli.main import resolve_forward_agent

        assert resolve_forward_agent(False, config_default=True) is False

    def test_none_uses_config_default_true(self, monkeypatch):
        from klangk.cli.main import resolve_forward_agent

        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        assert resolve_forward_agent(None, config_default=True) is True

    def test_none_uses_config_default_false(self):
        from klangk.cli.main import resolve_forward_agent

        assert resolve_forward_agent(None, config_default=False) is False

    def test_none_defaults_to_false(self):
        from klangk.cli.main import resolve_forward_agent

        assert resolve_forward_agent(None) is False

    def test_warns_when_no_ssh_auth_sock(self, monkeypatch):
        from klangk.cli.main import resolve_forward_agent

        monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
        result = resolve_forward_agent(True)
        assert result is True


class TestSandboxCommand:
    def test_missing_config_exits(self, logged_in_cfg, tmp_path):
        from klangk.cli import main

        client = MagicMock()
        client.get_handle.return_value = "admin"

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["sandbox", "myws", str(tmp_path)]
            )
        assert result.exit_code == 1
        assert "No sandbox config" in result.output

    def test_invalid_config_exits(self, logged_in_cfg, tmp_path):
        from klangk.cli import main

        (tmp_path / ".klangk-sandbox.yaml").write_text("not a mapping")

        client = MagicMock()
        client.get_handle.return_value = "admin"

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["sandbox", "myws", str(tmp_path)]
            )
        assert result.exit_code == 1
        assert "Invalid sandbox config" in result.output

    def test_creates_workspace(self, logged_in_cfg, tmp_path):
        from klangk.cli import main

        (tmp_path / ".klangk-sandbox.yaml").write_text(
            "sandbox:\n  mount-at: ~/test\n"
        )

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="myws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.get_handle.return_value = "admin"
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("myws")
        client.create_workspace.return_value = ws

        async def fake_setup(*args, **kwargs):
            pass

        with (
            patch.object(main, "_client", return_value=client),
            patch.object(main, "sandbox_setup_only", fake_setup),
        ):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["sandbox", "myws", str(tmp_path)]
            )
        assert result.exit_code == 0
        client.create_workspace.assert_called_once()
        call_kwargs = client.create_workspace.call_args
        assert call_kwargs[0][0] == "myws"
        assert "Creating workspace" in result.output
        assert "klangk shell" in result.output

    def test_existing_workspace_errors_without_force(
        self, logged_in_cfg, tmp_path
    ):
        from klangk.cli import main

        (tmp_path / ".klangk-sandbox.yaml").write_text(
            "sandbox:\n  mount-at: ~/test\n"
        )

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="myws",
            created_at="2025-01-01T00:00:00Z",
            mounts=[f"{tmp_path.resolve()}:/home/admin/test"],
        )
        client = MagicMock()
        client.get_handle.return_value = "admin"
        client.resolve_workspace.return_value = ws

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["sandbox", "myws", str(tmp_path)]
            )
        assert result.exit_code == 1
        assert "already exists" in result.output
        assert "--force" in result.output

    def test_force_reruns_setup(self, logged_in_cfg, tmp_path):
        from klangk.cli import main

        (tmp_path / ".klangk-sandbox.yaml").write_text(
            "sandbox:\n  mount-at: ~/test\n"
        )

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="myws",
            created_at="2025-01-01T00:00:00Z",
            mounts=[f"{tmp_path.resolve()}:/home/admin/test"],
        )
        client = MagicMock()
        client.get_handle.return_value = "admin"
        client.resolve_workspace.return_value = ws

        setup_called = []

        async def fake_setup(*args, **kwargs):
            setup_called.append(True)

        with (
            patch.object(main, "_client", return_value=client),
            patch.object(main, "sandbox_setup_only", fake_setup),
        ):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app,
                ["sandbox", "myws", str(tmp_path), "--force"],
            )
        assert result.exit_code == 0
        assert setup_called == [True]
        assert "re-applying config" in result.output

    def test_setup_connection_error(self, logged_in_cfg, tmp_path):
        from klangk.cli import main

        (tmp_path / ".klangk-sandbox.yaml").write_text(
            "sandbox:\n  mount-at: ~/test\n"
        )

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="myws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.get_handle.return_value = "admin"
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("myws")
        client.create_workspace.return_value = ws

        async def failing_setup(*args, **kwargs):
            raise ConnectionError("connection refused")

        with (
            patch.object(main, "_client", return_value=client),
            patch.object(main, "sandbox_setup_only", failing_setup),
        ):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app,
                ["sandbox", "myws", str(tmp_path)],
            )
        assert result.exit_code == 1
        assert "connection refused" in result.output


class TestSandboxSetupOnly:
    async def test_connects_and_runs_setup(self):
        from pathlib import Path

        from klangk.cli.main import sandbox_setup_only
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(setup="setup.sh")

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps({"type": "container_ready"})
        )

        with (
            patch("klangk.cli.transport.websockets.connect") as mock_connect,
            patch("klangk.cli.main.sandbox_setup") as mock_setup,
        ):
            mock_connect.return_value.__aenter__ = AsyncMock(
                return_value=mock_ws
            )
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_setup.return_value = 0
            await sandbox_setup_only(
                "http://test",
                "token",
                "ws-id",
                config,
                Path("/tmp"),
                "admin",
                max_size=2**20,
            )
            mock_setup.assert_called_once_with(
                mock_ws, config, Path("/tmp"), "admin"
            )

    async def test_starts_terminal_after_setup_when_service_command(self):
        from pathlib import Path

        from klangk.cli.main import sandbox_setup_only
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(
            setup="setup.sh", service_command="openclaw gateway"
        )

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "container_ready"}),
                json.dumps({"type": "terminal_started"}),
            ]
        )

        with (
            patch("klangk.cli.transport.websockets.connect") as mock_connect,
            patch("klangk.cli.main.sandbox_setup") as mock_setup,
        ):
            mock_connect.return_value.__aenter__ = AsyncMock(
                return_value=mock_ws
            )
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_setup.return_value = 0
            await sandbox_setup_only(
                "http://test",
                "token",
                "ws-id",
                config,
                Path("/tmp"),
                "admin",
            )
            mock_setup.assert_called_once_with(
                mock_ws, config, Path("/tmp"), "admin"
            )

        # terminal_start was sent after setup so the service command runs.
        sent = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
        assert any(m.get("cmd") == "terminal_start" for m in sent)

    async def test_terminal_start_disconnect_is_not_fatal(self):
        """A closed connection while awaiting terminal_started is tolerated."""
        from pathlib import Path

        import websockets

        from klangk.cli.main import sandbox_setup_only
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(
            setup="setup.sh", service_command="openclaw gateway"
        )

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "container_ready"}),
                websockets.ConnectionClosed(None, None),
            ]
        )

        with (
            patch("klangk.cli.transport.websockets.connect") as mock_connect,
            patch("klangk.cli.main.sandbox_setup") as mock_setup,
        ):
            mock_connect.return_value.__aenter__ = AsyncMock(
                return_value=mock_ws
            )
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_setup.return_value = 0
            # Must not raise / hang waiting for terminal_started.
            await sandbox_setup_only(
                "http://test",
                "token",
                "ws-id",
                config,
                Path("/tmp"),
                "admin",
            )

    async def test_marks_setup_state_pending_then_complete(self):
        """With a client, sandbox_setup_only marks pending then complete (#1033)."""
        from pathlib import Path

        from klangk.cli.main import sandbox_setup_only
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(
            setup="setup.sh", service_command="openclaw gateway"
        )

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "container_ready"}),
                json.dumps({"type": "terminal_started"}),
            ]
        )
        mock_client = MagicMock()
        mock_client.set_setup_state = MagicMock()

        with (
            patch("klangk.cli.transport.websockets.connect") as mock_connect,
            patch("klangk.cli.main.sandbox_setup") as mock_setup,
        ):
            mock_connect.return_value.__aenter__ = AsyncMock(
                return_value=mock_ws
            )
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_setup.return_value = 0
            await sandbox_setup_only(
                "http://test",
                "token",
                "ws-id",
                config,
                Path("/tmp"),
                "admin",
                client=mock_client,
            )

        # set_setup_state called twice: pending (before setup),
        # complete (after setup returns 0).
        calls = [c.args for c in mock_client.set_setup_state.call_args_list]
        assert ("ws-id", "pending") in calls
        assert ("ws-id", "complete") in calls

    async def test_marks_setup_state_failed_on_setup_failure(self):
        """A non-zero setup exit marks setup_state as 'failed' (#1033)."""
        from pathlib import Path

        from klangk.cli.main import sandbox_setup_only
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(
            setup="setup.sh", service_command="openclaw gateway"
        )

        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps({"type": "container_ready"})
        )
        mock_client = MagicMock()
        mock_client.set_setup_state = MagicMock()

        with (
            patch("klangk.cli.transport.websockets.connect") as mock_connect,
            patch("klangk.cli.main.sandbox_setup") as mock_setup,
        ):
            mock_connect.return_value.__aenter__ = AsyncMock(
                return_value=mock_ws
            )
            mock_connect.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_setup.return_value = 1  # setup failed
            await sandbox_setup_only(
                "http://test",
                "token",
                "ws-id",
                config,
                Path("/tmp"),
                "admin",
                client=mock_client,
            )

        calls = [c.args for c in mock_client.set_setup_state.call_args_list]
        assert ("ws-id", "failed") in calls
        # terminal_start NOT sent on failure
        sent = [json.loads(c.args[0]) for c in mock_ws.send.call_args_list]
        assert not any(m.get("cmd") == "terminal_start" for m in sent)

    async def test_copies_files(self, tmp_path):
        from klangk.cli.main import sandbox_setup
        from klangk.cli.sandbox import SandboxConfig

        src_file = tmp_path / "myconf"
        src_file.write_text("hello")

        config = SandboxConfig(
            copy=[f"{src_file}:/home/admin/.myconf"],
        )

        ws = AsyncMock()
        exec_calls = []

        async def fake_exec(ws, cmd, stdin=None, stdout=None, timeout=None):
            exec_calls.append(
                {"cmd": cmd, "stdin": stdin.read() if stdin else None}
            )
            return 0

        with patch("klangk.cli.main.exec_on_ws", fake_exec):
            await sandbox_setup(ws, config, tmp_path, "admin")

        assert len(exec_calls) == 1
        assert b"hello" in exec_calls[0]["stdin"]

    async def test_copy_failure_warns(self, tmp_path):
        from klangk.cli.main import sandbox_setup
        from klangk.cli.sandbox import SandboxConfig

        src_file = tmp_path / "myconf"
        src_file.write_text("hello")

        config = SandboxConfig(
            copy=[f"{src_file}:/home/admin/.myconf"],
        )

        ws = AsyncMock()

        async def fake_exec(ws, cmd, stdin=None, stdout=None, timeout=None):
            return 1  # failure

        with patch("klangk.cli.main.exec_on_ws", fake_exec):
            await sandbox_setup(ws, config, tmp_path, "admin")

    async def test_copy_missing_file_warns(self, tmp_path, capsys):
        from klangk.cli.main import sandbox_setup
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(
            copy=["/nonexistent/file:/home/admin/.conf"],
        )

        ws = AsyncMock()

        with patch("klangk.cli.main.exec_on_ws", AsyncMock(return_value=0)):
            await sandbox_setup(ws, config, tmp_path, "admin")

        # exec_on_ws should not have been called (file doesn't exist)

    async def test_runs_setup_script(self, tmp_path):
        from klangk.cli.main import sandbox_setup
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(
            mount_at="~/project",
            setup="setup.sh",
        )

        ws = AsyncMock()
        exec_calls = []

        async def fake_exec(ws, cmd, stdin=None, stdout=None, timeout=None):
            exec_calls.append(cmd)
            return 0

        with patch("klangk.cli.main.exec_on_ws", fake_exec):
            await sandbox_setup(ws, config, tmp_path, "admin")

        assert len(exec_calls) == 1
        assert "/home/admin/project/setup.sh" in exec_calls[0][2]

    async def test_setup_sets_git_ssh_command(self, tmp_path):
        from klangk.cli.main import sandbox_setup
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(
            mount_at="~/project",
            setup="setup.sh",
        )

        ws = AsyncMock()
        exec_calls = []

        async def fake_exec(ws, cmd, stdin=None, stdout=None, timeout=None):
            exec_calls.append(cmd)
            return 0

        with patch("klangk.cli.main.exec_on_ws", fake_exec):
            await sandbox_setup(ws, config, tmp_path, "admin")

        shell_cmd = exec_calls[0][2]
        assert "GIT_SSH_COMMAND=" in shell_cmd
        assert "StrictHostKeyChecking=accept-new" in shell_cmd

    async def test_setup_passes_timeout(self, tmp_path):
        from klangk.cli.main import sandbox_setup
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(
            mount_at="~/project",
            setup="setup.sh",
            setup_timeout=60,
        )

        ws = AsyncMock()
        captured_timeout = []

        async def fake_exec(ws, cmd, stdin=None, stdout=None, timeout=None):
            captured_timeout.append(timeout)
            return 0

        with patch("klangk.cli.main.exec_on_ws", fake_exec):
            await sandbox_setup(ws, config, tmp_path, "admin")

        assert captured_timeout == [60]

    async def test_setup_timeout_warns(self, tmp_path, capsys):
        from klangk.cli.main import sandbox_setup
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(
            mount_at="~/project",
            setup="setup.sh",
            setup_timeout=10,
        )

        ws = AsyncMock()

        async def fake_exec(ws, cmd, stdin=None, stdout=None, timeout=None):
            return 124

        with patch("klangk.cli.main.exec_on_ws", fake_exec):
            await sandbox_setup(ws, config, tmp_path, "admin")

        err = capsys.readouterr().err
        assert "timed out after 10s" in err

    async def test_setup_failure_warns(self, tmp_path):
        from klangk.cli.main import sandbox_setup
        from klangk.cli.sandbox import SandboxConfig

        config = SandboxConfig(
            mount_at="~/project",
            setup="setup.sh",
        )

        ws = AsyncMock()

        async def fake_exec(ws, cmd, stdin=None, stdout=None, timeout=None):
            return 1

        with patch("klangk.cli.main.exec_on_ws", fake_exec):
            await sandbox_setup(ws, config, tmp_path, "admin")

    def test_connection_error_exits_cleanly(self, logged_in_cfg, tmp_path):
        from klangk.cli import main

        (tmp_path / ".klangk-sandbox.yaml").write_text(
            "sandbox:\n  mount-at: ~/test\n"
        )

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="myws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.get_handle.return_value = "admin"
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("myws")
        client.create_workspace.return_value = ws

        with (
            patch.object(main, "_client", return_value=client),
            patch.object(
                main,
                "asyncio",
                MagicMock(
                    run=MagicMock(
                        side_effect=ConnectionError(
                            "Bind mount source does not exist: /tmp/.env"
                        )
                    )
                ),
            ),
            patch.object(main, "reset_terminal"),
            patch.object(main, "drain_stdin"),
        ):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["sandbox", "myws", str(tmp_path)]
            )
        assert result.exit_code == 1
        assert "Bind mount source does not exist" in result.output


class TestMonitorCommand:
    def test_monitor_invokes_run_when_logged_in(self, logged_in_cfg):
        from klangk.cli import main

        mock_run = AsyncMock(return_value=None)
        with patch.object(main, "monitor_run", new=mock_run):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["monitor"])
            assert result.exit_code == 0

        assert mock_run.await_count == 1
        args, kwargs = mock_run.call_args
        assert args[0] == "http://localhost:8995"  # server_spec
        assert args[1] == "test-token"  # token
        assert kwargs["max_reconnects"] != 0  # reconnects by default

    def test_monitor_no_reconnect_passes_zero(self, logged_in_cfg):
        from klangk.cli import main

        mock_run = AsyncMock(return_value=None)
        with patch.object(main, "monitor_run", new=mock_run):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["monitor", "--no-reconnect"])
            assert result.exit_code == 0
        assert mock_run.call_args.kwargs["max_reconnects"] == 0

    def test_monitor_invalid_status_exits(self, logged_in_cfg):
        from klangk.cli import main

        async def _raise(*a, **kw):
            raise websockets.InvalidStatus(MagicMock(status_code=4001))

        with patch.object(main, "monitor_run", new=_raise):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["monitor"])
            assert result.exit_code == 1
            assert "Connection rejected" in result.output

    def test_monitor_keyboard_interrupt_stops_cleanly(self, logged_in_cfg):
        from klangk.cli import main

        async def _kb(*a, **kw):
            raise KeyboardInterrupt

        with patch.object(main, "monitor_run", new=_kb):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["monitor"])
            assert result.exit_code == 0
            assert "Stopped" in result.output


class TestStatusAdminFlag:
    """`status` derives admin from /my-permissions and degrades gracefully."""

    def _perms_client(self, perms, monkeypatch):
        from klangk.cli import main

        client = MagicMock()
        resp = MagicMock(
            status_code=200,
            json=lambda: {"permissions": perms},
        )
        resp.headers = {"content-type": "application/json"}
        client.get.return_value = resp
        monkeypatch.setattr(main, "_client", lambda: client)
        return client

    def test_plain_shows_admin_yes(self, logged_in_cfg, capsys, monkeypatch):
        from klangk.cli import main

        self._perms_client({"/admin": ["*"]}, monkeypatch)
        main.status(plain=True)
        out = capsys.readouterr().out
        assert "admin=yes" in out

    def test_plain_shows_admin_no(self, logged_in_cfg, capsys, monkeypatch):
        from klangk.cli import main

        self._perms_client({"/admin": []}, monkeypatch)
        main.status(plain=True)
        out = capsys.readouterr().out
        assert "admin=no" in out

    def test_rich_shows_admin_yes(self, logged_in_cfg, monkeypatch):
        from io import StringIO

        from rich.console import Console

        from klangk.cli import main

        self._perms_client({"/admin": ["*"]}, monkeypatch)
        buf = StringIO()
        with patch.object(
            main,
            "Console",
            return_value=Console(file=buf, force_terminal=True),
        ):
            main.status(plain=False)
        assert "yes" in buf.getvalue()

    def test_rich_shows_admin_no(self, logged_in_cfg, monkeypatch):
        from io import StringIO

        from rich.console import Console

        from klangk.cli import main

        self._perms_client({"/admin": []}, monkeypatch)
        buf = StringIO()
        with patch.object(
            main,
            "Console",
            return_value=Console(file=buf, force_terminal=True),
        ):
            main.status(plain=False)
        assert "no" in buf.getvalue()

    def test_degrades_when_permissions_unreachable(
        self, logged_in_cfg, capsys, monkeypatch
    ):
        from klangk.cli import main

        client = MagicMock()
        client.get.side_effect = Exception("offline")
        monkeypatch.setattr(main, "_client", lambda: client)
        main.status(plain=True)
        out = capsys.readouterr().out
        # No admin line, but the rest is still reported.
        assert "status=logged_in" in out
        assert "admin=" not in out


class TestAdminUsersCLI:
    """`admin users list` and `admin users set-password` (#1374)."""

    def test_users_list(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "users": [
                    {
                        "id": "u-1",
                        "email": "admin@example.com",
                        "handle": "admin",
                        "verified": True,
                        "provider": None,
                        "created_at": "2026-01-01 00:00:00",
                    }
                ],
                "page": 1,
                "page_size": 50,
                "total": 1,
            },
        )
        monkeypatch.setattr(main, "_client", lambda: client)
        result = CliRunner().invoke(main.app, ["admin", "users", "ls"])
        assert result.exit_code == 0
        assert "admin@example.com" in result.stdout

    def test_users_list_empty(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"users": [], "total": 0},
        )
        monkeypatch.setattr(main, "_client", lambda: client)
        result = CliRunner().invoke(main.app, ["admin", "users", "ls"])
        assert result.exit_code == 0
        assert "No users" in result.stdout

    def test_users_list_error(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        resp = MagicMock(status_code=403)
        resp.json.return_value = {"detail": "Permission denied"}
        resp.text = "Permission denied"
        resp.headers = {"content-type": "application/json"}
        client.get.return_value = resp
        monkeypatch.setattr(main, "_client", lambda: client)
        result = CliRunner().invoke(main.app, ["admin", "users", "ls"])
        assert result.exit_code == 1

    def test_users_list_pagination_note(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "users": [
                    {
                        "id": "u-1",
                        "email": "a@example.com",
                        "handle": "",
                        "verified": True,
                        "provider": None,
                        "created_at": "2026-01-01 00:00:00",
                    }
                ],
                "total": 5,
            },
        )
        monkeypatch.setattr(main, "_client", lambda: client)
        result = CliRunner().invoke(main.app, ["admin", "users", "ls"])
        assert result.exit_code == 0
        assert "Showing 1 of 5" in result.stdout

    def test_set_password_search_error(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        search_resp = MagicMock(status_code=500)
        search_resp.json.return_value = {"detail": "boom"}
        search_resp.text = "boom"
        search_resp.headers = {"content-type": "application/json"}
        client.get.return_value = search_resp
        monkeypatch.setattr(main, "_client", lambda: client)
        result = CliRunner().invoke(
            main.app,
            [
                "admin",
                "users",
                "set-password",
                "hero@example.com",
                "--password",
                "x",
            ],
        )
        assert result.exit_code == 1
        client.patch.assert_not_called()

    def test_set_password_with_option(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        # /users/search resolves email -> id
        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = [
            {"id": "u-1", "email": "hero@example.com", "handle": "hero"}
        ]
        # PATCH /admin/users/{id} succeeds
        patch_resp = MagicMock(status_code=200)
        patch_resp.json.return_value = {"status": "updated"}
        patch_resp.headers = {"content-type": "application/json"}
        client.get.return_value = search_resp
        client.patch.return_value = patch_resp
        monkeypatch.setattr(main, "_client", lambda: client)
        result = CliRunner().invoke(
            main.app,
            [
                "admin",
                "users",
                "set-password",
                "hero@example.com",
                "--password",
                "newpw123",
            ],
        )
        assert result.exit_code == 0
        assert "hero@example.com" in result.stdout
        client.patch.assert_called_once()
        called_path = client.patch.call_args.args[0]
        assert called_path == "/api/v1/admin/users/u-1"

    def test_set_password_by_handle(self, logged_in_cfg, monkeypatch):
        """set-password resolves a *handle* to a user id (#616)."""
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        # /users/search returns the user; the CLI matches on handle.
        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = [
            {"id": "u-1", "email": "hero@example.com", "handle": "hero"}
        ]
        patch_resp = MagicMock(status_code=200)
        patch_resp.json.return_value = {"status": "updated"}
        patch_resp.headers = {"content-type": "application/json"}
        client.get.return_value = search_resp
        client.patch.return_value = patch_resp
        monkeypatch.setattr(main, "_client", lambda: client)
        result = CliRunner().invoke(
            main.app,
            [
                "admin",
                "users",
                "set-password",
                "hero",  # handle, not email
                "--password",
                "newpw123",
            ],
        )
        assert result.exit_code == 0
        client.patch.assert_called_once()
        assert client.patch.call_args.args[0] == "/api/v1/admin/users/u-1"

    def test_set_password_prompt_match(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = [
            {"id": "u-1", "email": "hero@example.com", "handle": "hero"}
        ]
        patch_resp = MagicMock(status_code=200)
        patch_resp.json.return_value = {"status": "updated"}
        patch_resp.headers = {"content-type": "application/json"}
        client.get.return_value = search_resp
        client.patch.return_value = patch_resp
        monkeypatch.setattr(main, "_client", lambda: client)
        # Two identical prompts.
        monkeypatch.setattr(
            "klangk.cli.main.Prompt.ask",
            lambda *a, **k: "newpw123",
        )
        result = CliRunner().invoke(
            main.app, ["admin", "users", "set-password", "hero@example.com"]
        )
        assert result.exit_code == 0

    def test_set_password_prompt_mismatch(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = [
            {"id": "u-1", "email": "hero@example.com", "handle": "hero"}
        ]
        client.get.return_value = search_resp
        monkeypatch.setattr(main, "_client", lambda: client)
        answers = iter(["newpw123", "different"])

        def _ask(*a, **k):
            return next(answers)

        monkeypatch.setattr("klangk.cli.main.Prompt.ask", _ask)
        result = CliRunner().invoke(
            main.app, ["admin", "users", "set-password", "hero@example.com"]
        )
        assert result.exit_code == 1
        client.patch.assert_not_called()

    def test_set_password_user_not_found(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = [
            {"id": "u-2", "email": "other@example.com", "handle": "other"}
        ]
        client.get.return_value = search_resp
        monkeypatch.setattr(main, "_client", lambda: client)
        result = CliRunner().invoke(
            main.app,
            [
                "admin",
                "users",
                "set-password",
                "ghost@example.com",
                "--password",
                "x",
            ],
        )
        assert result.exit_code == 1
        client.patch.assert_not_called()

    def test_set_password_backend_error(self, logged_in_cfg, monkeypatch):
        from klangk.cli import main
        from typer.testing import CliRunner

        client = MagicMock()
        search_resp = MagicMock(status_code=200)
        search_resp.json.return_value = [
            {"id": "u-1", "email": "hero@example.com", "handle": "hero"}
        ]
        patch_resp = MagicMock(status_code=400)
        patch_resp.json.return_value = {"detail": "Password too short"}
        patch_resp.text = "Password too short"
        patch_resp.headers = {"content-type": "application/json"}
        client.get.return_value = search_resp
        client.patch.return_value = patch_resp
        monkeypatch.setattr(main, "_client", lambda: client)
        result = CliRunner().invoke(
            main.app,
            [
                "admin",
                "users",
                "set-password",
                "hero@example.com",
                "--password",
                "x",
            ],
        )
        assert result.exit_code == 1
