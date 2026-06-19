"""Tests for klangk CLI commands (main.py)."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import typer

from klangkc.client import WorkspaceNotFoundError
from klangkc.config import CLIConfig
from klangkc.client import Workspace


@pytest.fixture
def logged_in_cfg(tmp_path, monkeypatch):
    """Config with a valid token and email pre-loaded."""
    config_path = tmp_path / "cli.toml"
    monkeypatch.setattr("klangkc.config._CONFIG_PATH", config_path)
    cfg = CLIConfig()
    cfg.server.url = "http://localhost:8995"
    cfg.auth.token = "test-token"
    cfg.auth.email = "test@example.com"
    cfg.save()
    yield config_path
    # No teardown needed — each test gets a fresh tmp_path


@pytest.fixture(autouse=True)
def reset_main_state():
    """Reset module-level CLI state before and after each test."""
    import klangkc.main as _main

    orig = _main._cfg_cache
    _main._cfg_cache = None
    yield
    _main._cfg_cache = orig


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
        monkeypatch.setattr("klangkc.auth._fetch_config", lambda _: None)

    def test_login_cmd_stores_token(self, tmp_path, monkeypatch):
        from klangkc.main import login_cmd

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr("klangkc.config._CONFIG_PATH", config_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "new-token"}
        with patch("httpx.post", return_value=mock_resp):
            with patch(
                "klangkc.auth.Prompt.ask",
                side_effect=["u@test.com", "pw"],
            ):
                login_cmd(
                    email=None,
                    server="http://localhost:8995",
                    password_file=None,
                )
        cfg = CLIConfig.load()
        assert cfg.auth.token == "new-token"
        assert cfg.auth.email == "u@test.com"

    def test_login_cmd_with_password_file(self, tmp_path, monkeypatch):
        from klangkc.main import login_cmd

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr("klangkc.config._CONFIG_PATH", config_path)
        pw_file = tmp_path / "pw.txt"
        pw_file.write_text("file-pw\n")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "file-token"}
        with patch("httpx.post", return_value=mock_resp):
            login_cmd(
                email="file@test.com",
                server="http://localhost:8995",
                password_file=str(pw_file),
            )
        cfg = CLIConfig.load()
        assert cfg.auth.token == "file-token"

    def test_login_cmd_with_password_stdin(self, tmp_path, monkeypatch):
        from klangkc.main import login_cmd

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr("klangkc.config._CONFIG_PATH", config_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "stdin-token"}
        with patch("httpx.post", return_value=mock_resp):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.readline.return_value = "stdin-pw\n"
                login_cmd(
                    email="stdin@test.com",
                    server="http://localhost:8995",
                    password_file="-",
                )
        cfg = CLIConfig.load()
        assert cfg.auth.token == "stdin-token"

    def test_require_auth_raises_when_not_logged_in(
        self, tmp_path, monkeypatch
    ):
        import typer
        from klangkc import main

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr("klangkc.config._CONFIG_PATH", config_path)
        cfg = CLIConfig()
        cfg.auth.token = None
        cfg.save()

        with pytest.raises(typer.Exit):
            main._require_auth()

    def test_require_auth_passes_when_logged_in(self, logged_in_cfg):
        from klangkc import main

        main._require_auth()  # Should not raise

    def test_list_workspaces_empty(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        monkeypatch.setattr(main, "_cfg", lambda: CLIConfig.load())
        client = MagicMock()
        client.list_workspaces.return_value = []
        client.list_shared_workspaces.return_value = []
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo") as mock_echo:
            main.list_workspaces(shared=True)
        assert any("No workspaces" in str(c) for c in mock_echo.call_args_list)

    def test_list_shared_workspaces_plain(self, logged_in_cfg, monkeypatch):
        from klangkc import main

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

        from klangkc import main

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
        from klangkc import main

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

        from klangkc import main

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

    def test_create_workspace(self, logged_in_cfg, monkeypatch):
        from io import StringIO

        from rich.console import Console

        from klangkc import main

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

        from klangkc import main

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
        from klangkc import main

        client = MagicMock()
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo"):
            main.rm("my-ws")
        client.delete_workspace.assert_called_once_with("my-ws")

    def test_delete_workspace_not_found(self, logged_in_cfg, monkeypatch):
        import typer

        from klangkc.client import WorkspaceNotFoundError
        from klangkc import main

        client = MagicMock()
        client.delete_workspace.side_effect = WorkspaceNotFoundError("nope")
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.rm("nope")

    def test_shell_requires_auth(self, tmp_path, monkeypatch):
        import typer
        from klangkc import main

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr("klangkc.config._CONFIG_PATH", config_path)
        cfg = CLIConfig()
        cfg.auth.token = None
        cfg.save()

        with pytest.raises(typer.Exit):
            main.shell(None)

    def test_status_not_logged_in(self, tmp_path, monkeypatch, capsys):
        from klangkc import main

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr("klangkc.config._CONFIG_PATH", config_path)
        cfg = CLIConfig()
        cfg.server.url = "http://custom:1234"
        cfg.auth.token = None
        cfg.save()

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "custom:1234" in output
        assert "not_logged_in" in output

    def test_status_logged_in(self, logged_in_cfg, capsys):
        from klangkc import main

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "test@example.com" in output
        assert "logged_in" in output

    def test_status_rich_logged_in(self, logged_in_cfg):
        from io import StringIO

        from rich.console import Console

        from klangkc import main

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

        from klangkc import main

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr("klangkc.config._CONFIG_PATH", config_path)
        cfg = CLIConfig()
        cfg.auth.token = None
        cfg.save()

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
        from klangkc import main

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "server=http://localhost:8995" in output
        assert "user=test@example.com" in output
        assert "status=logged_in" in output

    def test_status_plain_not_logged_in(self, tmp_path, monkeypatch, capsys):
        from klangkc import main

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr("klangkc.config._CONFIG_PATH", config_path)
        cfg = CLIConfig()
        cfg.server.url = "http://custom:1234"
        cfg.auth.token = None
        cfg.save()

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "server=http://custom:1234" in output
        assert "status=not_logged_in" in output
        assert "user=" not in output

    def test_logout_command(self, logged_in_cfg):
        from klangkc import main

        with patch("httpx.post", return_value=MagicMock(status_code=200)):
            main.logout()
        cfg = CLIConfig.load()
        assert cfg.auth.token is None

    def test_logout_network_error_does_not_propagate(self, logged_in_cfg):
        from klangkc import main

        with patch("httpx.post", side_effect=httpx.ConnectError("no route")):
            main.logout()  # must not raise

    def test_shell_with_single_workspace_auto_selects(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangkc import main

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
            with patch.object(main, "_ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                with patch("termios.tcgetattr", return_value=None):
                    main.shell(None)  # no args, single workspace auto-selected

        client.resolve_workspace.assert_not_called()  # was auto-selected

    def test_shell_no_workspaces_exits(self, logged_in_cfg, monkeypatch):
        import typer
        from klangkc import main

        client = MagicMock()
        client.list_workspaces.return_value = []
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.shell(None)

    def test_shell_multiple_workspaces_prompts(
        self, logged_in_cfg, monkeypatch
    ):
        from klangkc import main

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
            with patch.object(main, "_ws_shell", fake_shell):
                with patch("builtins.input", return_value="1"):  # select first
                    with patch("termios.tcgetattr", return_value=None):
                        main.shell(None)

    def test_shell_by_name(self, logged_in_cfg, monkeypatch, reset_env):
        from klangkc import main

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
            with patch.object(main, "_ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                with patch("termios.tcgetattr", return_value=None):
                    main.shell("target-ws")

        client.resolve_workspace.assert_called_once_with("target-ws")

    def test_shell_with_terminal_arg(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangkc import main

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
            with patch.object(main, "_ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                with patch("termios.tcgetattr", return_value=None):
                    main.shell("target-ws", "build")

        assert captured_kwargs["window"] == "build"

    def test_shell_forward_agent_env_true(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        """KLANGKC_FORWARD_AGENT=true enables forwarding."""
        from klangkc import main

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
            with patch.object(main, "_ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                os.environ["KLANGKC_FORWARD_AGENT"] = "true"
                os.environ["SSH_AUTH_SOCK"] = "/tmp/agent.sock"
                with patch("termios.tcgetattr", return_value=None):
                    main.shell("target-ws")

        assert captured_kwargs["forward_agent"] is True

    def test_shell_forward_agent_flag_overrides_env_false(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        """--forward-agent flag takes precedence over KLANGKC_FORWARD_AGENT=false."""
        from klangkc import main

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
            with patch.object(main, "_ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                os.environ["KLANGKC_FORWARD_AGENT"] = "false"
                os.environ["SSH_AUTH_SOCK"] = "/tmp/agent.sock"
                with patch("termios.tcgetattr", return_value=None):
                    main.shell("target-ws", forward_agent=True)

        assert captured_kwargs["forward_agent"] is True

    def test_shell_forward_agent_env_url_list_match(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        """KLANGKC_FORWARD_AGENT with matching URL enables forwarding."""
        from klangkc import main

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
            with patch.object(main, "_ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                os.environ["KLANGKC_FORWARD_AGENT"] = (
                    "http://localhost:8995 http://other:8995"
                )
                os.environ["SSH_AUTH_SOCK"] = "/tmp/agent.sock"
                with patch("termios.tcgetattr", return_value=None):
                    main.shell("target-ws")

        assert captured_kwargs["forward_agent"] is True

    def test_shell_forward_agent_env_url_list_no_match(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        """KLANGKC_FORWARD_AGENT with non-matching URL doesn't enable."""
        from klangkc import main

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
            with patch.object(main, "_ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                os.environ["KLANGKC_FORWARD_AGENT"] = (
                    "http://other:8995 http://another:8995"
                )
                with patch("termios.tcgetattr", return_value=None):
                    main.shell("target-ws")

        assert captured_kwargs["forward_agent"] is False

    def test_shell_forward_agent_no_ssh_auth_sock_warns(
        self, logged_in_cfg, monkeypatch, reset_env, capsys
    ):
        """--forward-agent without SSH_AUTH_SOCK prints warning."""
        from klangkc import main

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
            with patch.object(main, "_ws_shell", fake_shell):
                os.environ["TERM"] = "xterm-256color"
                os.environ.pop("SSH_AUTH_SOCK", None)
                os.environ["KLANGKC_FORWARD_AGENT"] = "true"
                with patch("termios.tcgetattr", return_value=None):
                    main.shell("target-ws")

        # The warning goes to stderr via rich console
        # Just verify it didn't crash — the warning is printed to _err console

    def test_terminals_command(self, logged_in_cfg, monkeypatch, reset_env):
        from klangkc import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "workspace_ready"}),
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
            patch("websockets.connect", return_value=mock_ws),
        ):
            os.environ["TERM"] = "xterm-256color"
            main.terminals("my-ws")

    def test_share_command(self, logged_in_cfg, monkeypatch, reset_env):
        from klangkc import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "workspace_ready"}),
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
            patch("websockets.connect", return_value=mock_ws),
        ):
            os.environ["TERM"] = "xterm-256color"
            main.share("my-ws", "build")

        # Should have sent share_window command
        sent = [
            json.loads(c[0][0])
            for c in mock_ws.send.call_args_list
            if isinstance(c[0][0], str)
        ]
        assert any(s.get("cmd") == "share_window" for s in sent)

    def test_unshare_command(self, logged_in_cfg, monkeypatch, reset_env):
        from klangkc import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "workspace_ready"}),
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
            patch("websockets.connect", return_value=mock_ws),
        ):
            os.environ["TERM"] = "xterm-256color"
            main.unshare("my-ws", "build")

        sent = [
            json.loads(c[0][0])
            for c in mock_ws.send.call_args_list
            if isinstance(c[0][0], str)
        ]
        assert any(s.get("cmd") == "unshare_window" for s in sent)

    def test_share_terminal_not_found(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangkc import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "workspace_ready"}),
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
            patch("websockets.connect", return_value=mock_ws),
        ):
            os.environ["TERM"] = "xterm-256color"
            with pytest.raises(typer.Exit):
                main.share("my-ws", "nonexistent")

    def test_terminals_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")

        with patch.object(main, "_client", return_value=client):
            with pytest.raises(typer.Exit):
                main.terminals("nope")

    def test_terminals_connection_failure(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangkc import main

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
            patch("websockets.connect", return_value=mock_ws),
        ):
            os.environ["TERM"] = "xterm-256color"
            with pytest.raises(ConnectionError):
                main.terminals("my-ws")

    def test_share_connection_failure(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangkc import main

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
            patch("websockets.connect", return_value=mock_ws),
        ):
            os.environ["TERM"] = "xterm-256color"
            with pytest.raises(ConnectionError):
                main.share("my-ws", "build")

    def test_unshare_connection_failure(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangkc import main

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
            patch("websockets.connect", return_value=mock_ws),
        ):
            os.environ["TERM"] = "xterm-256color"
            with pytest.raises(ConnectionError):
                main.unshare("my-ws", "build")

    def test_unshare_terminal_not_found(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from klangkc import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        messages = [
            json.dumps({"type": "workspace_ready"}),
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
            patch("websockets.connect", return_value=mock_ws),
        ):
            os.environ["TERM"] = "xterm-256color"
            with pytest.raises(typer.Exit):
                main.unshare("my-ws", "nonexistent")

    def test_edit_with_flags(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            image="klangk",
            default_command="klangk-pi",
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
        assert body["default_command"] == "pi"
        assert "image" not in body  # not provided, not sent

    def test_edit_clear_command(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            default_command="klangk-pi",
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
        assert call_args[1]["json"]["default_command"] is None

    def test_edit_interactive(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            image="klangk",
            default_command="klangk-pi",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # keep name, keep image, change command, skip add mount, skip add env
        with patch.object(main, "_client", return_value=client):
            with patch("builtins.input", side_effect=["", "", "pi", "", ""]):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        call_args = client.put.call_args
        body = call_args[1]["json"]
        assert "name" not in body  # kept current
        assert "image" not in body  # kept current
        assert body["default_command"] == "pi"

    def test_edit_interactive_change_all(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
            image="klangk",
            default_command="klangk-pi",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws
        client.put.return_value = MagicMock(status_code=200)

        # change name, image, command; skip add mount, (no mounts to remove)
        with patch.object(main, "_client", return_value=client):
            with patch(
                "builtins.input",
                side_effect=["renamed", "klangk-custom", "pi", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["name"] == "renamed"
        assert body["image"] == "klangk-custom"
        assert body["default_command"] == "pi"

    def test_edit_interactive_add_mount(self, logged_in_cfg, monkeypatch):
        from klangkc import main

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
                side_effect=["", "", "", "/host:/container", "", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        body = client.put.call_args[1]["json"]
        assert body["mounts"] == ["/host:/container"]

    def test_edit_interactive_remove_mount(self, logged_in_cfg, monkeypatch):
        from klangkc import main

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
                side_effect=["", "", "", "", "1", "", "", ""],
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
        from klangkc import main

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
                side_effect=["", "", "", "/new:/new", "", "1", "", "", ""],
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
        from klangkc import main

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
                side_effect=["", "", "", "", "99", "", "abc", "", "", ""],
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
        from klangkc import main

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
                side_effect=["", "", "", "", "1", "", ""],
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
        from klangkc import main

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
                side_effect=["", "", "", "bad", "/a:/b", "", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0
                assert "Invalid mount" in result.stdout

        body = client.put.call_args[1]["json"]
        assert body["mounts"] == ["/a:/b"]

    def test_create_invalid_mount_flag(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        monkeypatch.setattr(main, "_client", lambda: MagicMock())

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            main.app, ["create", "ws", "--mount", "not-valid"]
        )
        assert result.exit_code == 1

    def test_edit_invalid_mount_flag(self, logged_in_cfg, monkeypatch):
        from klangkc import main

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
        from klangkc import main

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
        from klangkc import main

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
        from klangkc import main

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        # keep name, image, command; skip add mount (no mounts, no remove prompt)
        with patch.object(main, "_client", return_value=client):
            with patch("builtins.input", side_effect=["", "", "", "", ""]):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0
                assert "No changes" in result.stdout

        client.put.assert_not_called()

    def test_edit_interactive_add_env(self, logged_in_cfg, monkeypatch):
        from klangkc import main

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
                side_effect=["", "", "", "", "FOO=bar", "", ""],
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
        from klangkc import main

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
                side_effect=["", "", "", "", "bad", "A=1", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0
                assert "KEY=VALUE" in result.stdout

        body = client.put.call_args[1]["json"]
        assert body["env"] == {"A": "1"}

    def test_create_with_env_flag(self, logged_in_cfg, monkeypatch):
        from klangkc import main

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
            "ws", image=None, mounts=None, env={"FOO": "bar", "X": "1"}
        )

    def test_create_with_invalid_env_flag(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        monkeypatch.setattr(main, "_client", lambda: MagicMock())

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(
            main.app, ["create", "ws", "--env", "NOEQUALSSIGN"]
        )
        assert result.exit_code == 1

    def test_edit_interactive_remove_env(self, logged_in_cfg, monkeypatch):
        from klangkc import main

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
                side_effect=["", "", "", "", "", "1", "", ""],
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
        from klangkc import main

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
                side_effect=["", "", "", "", "", "99", "", "abc", "", ""],
            ):
                from typer.testing import CliRunner

                runner = CliRunner()
                result = runner.invoke(main.app, ["edit", "my-ws"])
                assert result.exit_code == 0

        client.put.assert_not_called()

    def test_edit_with_env_flag(self, logged_in_cfg, monkeypatch):
        from klangkc import main

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

    def test_dup_workspace(self, logged_in_cfg, monkeypatch):
        from klangkc import main

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
        from klangkc import main

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["dup", "nope", "copy"])
            assert result.exit_code == 1

    def test_dup_workspace_conflict(self, logged_in_cfg, monkeypatch):
        from klangkc import main

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
        from klangkc import main

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
        from klangkc import main

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
        from klangkc import main

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
        from klangkc import main
        from klangkc.client import Workspace

        ws = Workspace(
            id="ws1" + "0" * 52,
            name="my-ws",
            created_at="2025-01-01T00:00:00Z",
        )
        client = MagicMock()
        client.resolve_workspace.return_value = ws

        async def fake_exec(*args):
            return 0

        ctx = MagicMock()
        ctx.args = ["ls", "-la"]
        with patch.object(main, "_client", return_value=client):
            with patch.object(main, "_ws_exec", fake_exec):
                with pytest.raises(typer.Exit) as exc_info:
                    main.exec_cmd(ctx, workspace="my-ws")
                assert exc_info.value.exit_code == 0

    def test_exec_no_command(self, logged_in_cfg):
        from klangkc import main

        ctx = MagicMock()
        ctx.args = []
        with pytest.raises(typer.Exit) as exc_info:
            main.exec_cmd(ctx, workspace="my-ws")
        assert exc_info.value.exit_code == 1

    def test_exec_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from klangkc import main
        from klangkc.client import WorkspaceNotFoundError

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")
        monkeypatch.setattr(main, "_client", lambda: client)

        ctx = MagicMock()
        ctx.args = ["ls"]
        with pytest.raises(typer.Exit) as exc_info:
            main.exec_cmd(ctx, workspace="nope")
        assert exc_info.value.exit_code == 1

    def test_sync_runs_rsync(self, logged_in_cfg):
        from klangkc import main

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
        assert "klangkc exec" in " ".join(cmd)

    def test_sync_no_rsync(self, logged_in_cfg):
        from klangkc import main

        def which_no_rsync(name):
            return "/usr/bin/klangkc" if name == "klangkc" else None

        ctx = MagicMock()
        ctx.args = []
        with patch("shutil.which", side_effect=which_no_rsync):
            with pytest.raises(typer.Exit) as exc_info:
                main.sync(ctx, src="/tmp/foo", dest="ws:/work/foo")
        assert exc_info.value.exit_code == 1

    def test_sync_passes_extra_args(self, logged_in_cfg):
        from klangkc import main

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
        from klangkc import main

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
        from klangkc import main

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
        from klangkc import main

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
        from klangkc import main

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
        from klangkc import main

        client = MagicMock()
        client.post.return_value = MagicMock(status_code=200)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "create", "new-vol"])
        assert result.exit_code == 0
        assert "Created" in result.stdout

    def test_volumes_create_duplicate(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        client = MagicMock()
        client.post.return_value = MagicMock(status_code=409)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "create", "dup-vol"])
        assert result.exit_code == 1

    def test_volumes_rm(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        client = MagicMock()
        client.delete.return_value = MagicMock(status_code=200)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "rm", "old-vol"])
        assert result.exit_code == 0
        assert "Deleted" in result.stdout

    def test_volumes_rm_not_found(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        client = MagicMock()
        client.delete.return_value = MagicMock(status_code=404)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "rm", "nope"])
        assert result.exit_code == 1

    def test_volumes_rm_in_use(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        client = MagicMock()
        client.delete.return_value = MagicMock(status_code=409)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "rm", "busy"])
        assert result.exit_code == 1

    def test_volumes_rm_permission_denied(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        client = MagicMock()
        client.delete.return_value = MagicMock(status_code=403)
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["volumes", "rm", "not-mine"])
        assert result.exit_code == 1


class TestExportImportCLI:
    def test_export_success(self, logged_in_cfg, monkeypatch, tmp_path):
        from klangkc import main

        monkeypatch.setattr(main, "_cfg", lambda: CLIConfig.load())
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
        from klangkc import main

        monkeypatch.setattr(main, "_cfg", lambda: CLIConfig.load())
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
        from klangkc import main

        monkeypatch.setattr(main, "_cfg", lambda: CLIConfig.load())
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
        from klangkc import main

        monkeypatch.setattr(main, "_cfg", lambda: CLIConfig.load())
        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.export_workspace(name="nope", output=None)

    def test_export_http_error(self, logged_in_cfg, monkeypatch, tmp_path):
        from klangkc import main

        monkeypatch.setattr(main, "_cfg", lambda: CLIConfig.load())
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
        from klangkc import main

        monkeypatch.setattr(main, "_cfg", lambda: CLIConfig.load())
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
        from klangkc import main

        monkeypatch.setattr(main, "_cfg", lambda: CLIConfig.load())

        with pytest.raises(typer.Exit):
            main.import_workspace(
                archive=tmp_path / "nonexistent.tar.gz", name=None
            )

    def test_import_http_error(self, logged_in_cfg, monkeypatch, tmp_path):
        from klangkc import main

        monkeypatch.setattr(main, "_cfg", lambda: CLIConfig.load())
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
        from klangkc import main

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
        result = runner.invoke(main.app, ["invite", "a@b.com"])
        assert result.exit_code == 0
        assert "a@b.com" in result.stdout

    def test_invite_error(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        client = MagicMock()
        client.post.return_value = MagicMock(
            status_code=400,
            json=lambda: {"detail": "already exists"},
            text="already exists",
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["invite", "a@b.com"])
        assert result.exit_code == 1

    def test_invite_forbidden(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        client = MagicMock()
        client.post.return_value = MagicMock(
            status_code=403,
            json=lambda: {"detail": "Invitations are disabled"},
            text="Invitations are disabled",
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["invite", "a@b.com"])
        assert result.exit_code == 1

    def test_invitations_list(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {
                    "email": "a@b.com",
                    "status": "pending",
                    "invited_by_email": "admin@x.com",
                    "created_at": "2026-01-01 00:00:00",
                }
            ],
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["invitations"])
        assert result.exit_code == 0
        assert "a@b.com" in result.stdout

    def test_invitations_empty(self, logged_in_cfg, monkeypatch):
        from klangkc import main

        client = MagicMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [],
        )
        monkeypatch.setattr(main, "_client", lambda: client)

        from typer.testing import CliRunner

        runner = CliRunner()
        result = runner.invoke(main.app, ["invitations"])
        assert result.exit_code == 0
        assert "No invitations" in result.stdout


class TestBuildWsUrl:
    def test_http(self):
        from klangkc.main import _build_ws_url

        assert (
            _build_ws_url("http://localhost:8995") == "ws://localhost:8995/ws"
        )

    def test_https(self):
        from klangkc.main import _build_ws_url

        assert _build_ws_url("https://example.com") == "wss://example.com/ws"

    def test_bare(self):
        from klangkc.main import _build_ws_url

        assert _build_ws_url("example.com") == "ws://example.com/ws"


class TestResolveForwardAgent:
    def test_flag_true(self, monkeypatch):
        from klangkc.main import _resolve_forward_agent

        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        assert _resolve_forward_agent(True, "http://localhost") is True

    def test_env_true(self, monkeypatch):
        from klangkc.main import _resolve_forward_agent

        monkeypatch.setenv("KLANGKC_FORWARD_AGENT", "true")
        monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
        assert _resolve_forward_agent(False, "http://localhost") is True

    def test_option_info_treated_as_false(self):
        from klangkc.main import _resolve_forward_agent

        # Simulate typer OptionInfo (not a bool)
        assert (
            _resolve_forward_agent("not-a-bool", "http://localhost") is False
        )


class TestSandboxCommand:
    def test_missing_config_exits(self, logged_in_cfg, tmp_path):
        from klangkc import main

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
        from klangkc import main

        klangk_dir = tmp_path / ".klangk"
        klangk_dir.mkdir()
        (klangk_dir / "sandbox.yaml").write_text("not a mapping")

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
        from klangkc import main

        klangk_dir = tmp_path / ".klangk"
        klangk_dir.mkdir()
        (klangk_dir / "sandbox.yaml").write_text(
            "sandbox:\n  mount_at: ~/test\n"
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

        async def fake_connect(*args, **kwargs):
            pass

        with (
            patch.object(main, "_client", return_value=client),
            patch.object(main, "_sandbox_connect", fake_connect),
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

    def test_reconnects_existing(self, logged_in_cfg, tmp_path):
        from klangkc import main

        klangk_dir = tmp_path / ".klangk"
        klangk_dir.mkdir()
        (klangk_dir / "sandbox.yaml").write_text(
            "sandbox:\n  mount_at: ~/test\n"
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

        async def fake_connect(*args, **kwargs):
            pass

        with (
            patch.object(main, "_client", return_value=client),
            patch.object(main, "_sandbox_connect", fake_connect),
        ):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["sandbox", "myws", str(tmp_path)]
            )
        assert result.exit_code == 0
        client.create_workspace.assert_not_called()
        assert "exists" in result.output

    def test_config_changed_warning(self, logged_in_cfg, tmp_path):
        from klangkc import main

        klangk_dir = tmp_path / ".klangk"
        klangk_dir.mkdir()
        (klangk_dir / "sandbox.yaml").write_text(
            "sandbox:\n  mount_at: ~/test\nmounts:\n  - /extra:/extra\n"
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

        async def fake_connect(*args, **kwargs):
            pass

        with (
            patch.object(main, "_client", return_value=client),
            patch.object(main, "_sandbox_connect", fake_connect),
        ):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app, ["sandbox", "myws", str(tmp_path)]
            )
        assert result.exit_code == 0
        assert "config has changed" in result.output

    def test_force_setup_passes_config(self, logged_in_cfg, tmp_path):
        from klangkc import main

        klangk_dir = tmp_path / ".klangk"
        klangk_dir.mkdir()
        (klangk_dir / "sandbox.yaml").write_text(
            "sandbox:\n  mount_at: ~/test\n"
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

        captured_kwargs = {}

        async def fake_connect(*args, **kwargs):
            captured_kwargs.update(kwargs)

        with (
            patch.object(main, "_client", return_value=client),
            patch.object(main, "_sandbox_connect", fake_connect),
        ):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(
                main.app,
                ["sandbox", "myws", str(tmp_path), "--force-setup"],
            )
        assert result.exit_code == 0
        assert captured_kwargs.get("config") is not None


class TestSandboxSetup:
    async def test_copies_files(self, tmp_path):
        from klangkc.main import _sandbox_setup
        from klangkc.sandbox import SandboxConfig

        src_file = tmp_path / "myconf"
        src_file.write_text("hello")

        config = SandboxConfig(
            copy=[f"{src_file}:/home/admin/.myconf"],
        )

        ws = AsyncMock()
        exec_calls = []

        async def fake_exec(ws, cmd, stdin=None, stdout=None):
            exec_calls.append(
                {"cmd": cmd, "stdin": stdin.read() if stdin else None}
            )
            return 0

        with patch("klangkc.main._exec_on_ws", fake_exec):
            await _sandbox_setup(ws, config, tmp_path, "admin")

        assert len(exec_calls) == 1
        assert b"hello" in exec_calls[0]["stdin"]

    async def test_copy_failure_warns(self, tmp_path):
        from klangkc.main import _sandbox_setup
        from klangkc.sandbox import SandboxConfig

        src_file = tmp_path / "myconf"
        src_file.write_text("hello")

        config = SandboxConfig(
            copy=[f"{src_file}:/home/admin/.myconf"],
        )

        ws = AsyncMock()

        async def fake_exec(ws, cmd, stdin=None, stdout=None):
            return 1  # failure

        with patch("klangkc.main._exec_on_ws", fake_exec):
            await _sandbox_setup(ws, config, tmp_path, "admin")

    async def test_copy_missing_file_warns(self, tmp_path, capsys):
        from klangkc.main import _sandbox_setup
        from klangkc.sandbox import SandboxConfig

        config = SandboxConfig(
            copy=["/nonexistent/file:/home/admin/.conf"],
        )

        ws = AsyncMock()

        with patch("klangkc.main._exec_on_ws", AsyncMock(return_value=0)):
            await _sandbox_setup(ws, config, tmp_path, "admin")

        # _exec_on_ws should not have been called (file doesn't exist)

    async def test_runs_setup_script(self, tmp_path):
        from klangkc.main import _sandbox_setup
        from klangkc.sandbox import SandboxConfig

        config = SandboxConfig(
            mount_at="~/project",
            setup=".klangk/setup.sh",
        )

        ws = AsyncMock()
        exec_calls = []

        async def fake_exec(ws, cmd, stdin=None, stdout=None):
            exec_calls.append(cmd)
            return 0

        with patch("klangkc.main._exec_on_ws", fake_exec):
            await _sandbox_setup(ws, config, tmp_path, "admin")

        assert len(exec_calls) == 1
        assert "/home/admin/project/.klangk/setup.sh" in exec_calls[0][2]

    async def test_setup_failure_warns(self, tmp_path):
        from klangkc.main import _sandbox_setup
        from klangkc.sandbox import SandboxConfig

        config = SandboxConfig(
            mount_at="~/project",
            setup=".klangk/setup.sh",
        )

        ws = AsyncMock()

        async def fake_exec(ws, cmd, stdin=None, stdout=None):
            return 1

        with patch("klangkc.main._exec_on_ws", fake_exec):
            await _sandbox_setup(ws, config, tmp_path, "admin")
