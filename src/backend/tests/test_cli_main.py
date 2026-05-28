"""Tests for bark CLI commands (main.py)."""

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest
import typer

from bark_backend.cli.client import WorkspaceNotFoundError
from bark_backend.cli.config import CLIConfig
from bark_backend.cli.client import Workspace


@pytest.fixture
def logged_in_cfg(tmp_path, monkeypatch):
    """Config with a valid token and email pre-loaded."""
    config_path = tmp_path / "cli.toml"
    monkeypatch.setattr("bark_backend.cli.config._CONFIG_PATH", config_path)
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
    import bark_backend.cli.main as _main

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
    def test_login_cmd_stores_token(self, tmp_path, monkeypatch):
        from bark_backend.cli.main import login_cmd

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "bark_backend.cli.config._CONFIG_PATH", config_path
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "new-token"}
        with patch("httpx.post", return_value=mock_resp):
            with patch(
                "bark_backend.cli.auth.Prompt.ask",
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
        from bark_backend.cli.main import login_cmd

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "bark_backend.cli.config._CONFIG_PATH", config_path
        )
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
        from bark_backend.cli.main import login_cmd

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "bark_backend.cli.config._CONFIG_PATH", config_path
        )
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
        from bark_backend.cli import main

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "bark_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig()
        cfg.auth.token = None
        cfg.save()

        with pytest.raises(typer.Exit):
            main._require_auth()

    def test_require_auth_passes_when_logged_in(self, logged_in_cfg):
        from bark_backend.cli import main

        main._require_auth()  # Should not raise

    def test_list_workspaces_empty(self, logged_in_cfg, monkeypatch):
        from bark_backend.cli import main

        monkeypatch.setattr(main, "_cfg", lambda: CLIConfig.load())
        client = MagicMock()
        client.list_workspaces.return_value = []
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo"):
            main.list_workspaces()

        client.list_workspaces.assert_called_once()

    def test_list_workspaces_plain(self, logged_in_cfg, monkeypatch):
        from bark_backend.cli import main

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

        from bark_backend.cli import main

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

        from bark_backend.cli import main

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

        from bark_backend.cli import main

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
        from bark_backend.cli import main

        client = MagicMock()
        monkeypatch.setattr(main, "_client", lambda: client)

        with patch("typer.echo"):
            main.delete("my-ws")
        client.delete_workspace.assert_called_once_with("my-ws")

    def test_delete_workspace_not_found(self, logged_in_cfg, monkeypatch):
        import typer

        from bark_backend.cli.client import WorkspaceNotFoundError
        from bark_backend.cli import main

        client = MagicMock()
        client.delete_workspace.side_effect = WorkspaceNotFoundError("nope")
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.delete("nope")

    def test_shell_requires_auth(self, tmp_path, monkeypatch):
        import typer
        from bark_backend.cli import main

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "bark_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig()
        cfg.auth.token = None
        cfg.save()

        with pytest.raises(typer.Exit):
            main.shell(None)

    def test_status_not_logged_in(self, tmp_path, monkeypatch, capsys):
        from bark_backend.cli import main

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "bark_backend.cli.config._CONFIG_PATH", config_path
        )
        cfg = CLIConfig()
        cfg.server.url = "http://custom:1234"
        cfg.auth.token = None
        cfg.save()

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "custom:1234" in output
        assert "not_logged_in" in output

    def test_status_logged_in(self, logged_in_cfg, capsys):
        from bark_backend.cli import main

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "test@example.com" in output
        assert "logged_in" in output

    def test_status_rich_logged_in(self, logged_in_cfg):
        from io import StringIO

        from rich.console import Console

        from bark_backend.cli import main

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

        from bark_backend.cli import main

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "bark_backend.cli.config._CONFIG_PATH", config_path
        )
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
        from bark_backend.cli import main

        main.status(plain=True)
        output = capsys.readouterr().out
        assert "server=http://localhost:8995" in output
        assert "user=test@example.com" in output
        assert "status=logged_in" in output

    def test_status_plain_not_logged_in(self, tmp_path, monkeypatch, capsys):
        from bark_backend.cli import main

        config_path = tmp_path / "cli.toml"
        monkeypatch.setattr(
            "bark_backend.cli.config._CONFIG_PATH", config_path
        )
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
        from bark_backend.cli import main

        with patch("httpx.post", return_value=MagicMock(status_code=200)):
            main.logout()
        cfg = CLIConfig.load()
        assert cfg.auth.token is None

    def test_logout_network_error_does_not_propagate(self, logged_in_cfg):
        from bark_backend.cli import main

        with patch("httpx.post", side_effect=httpx.ConnectError("no route")):
            main.logout()  # must not raise

    def test_shell_with_single_workspace_auto_selects(
        self, logged_in_cfg, monkeypatch, reset_env
    ):
        from bark_backend.cli import main

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
        from bark_backend.cli import main

        client = MagicMock()
        client.list_workspaces.return_value = []
        monkeypatch.setattr(main, "_client", lambda: client)

        with pytest.raises(typer.Exit):
            main.shell(None)

    def test_shell_multiple_workspaces_prompts(
        self, logged_in_cfg, monkeypatch
    ):
        from bark_backend.cli import main

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
        from bark_backend.cli import main

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

    def test_set_command(self, logged_in_cfg, monkeypatch):
        from bark_backend.cli import main

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
            result = runner.invoke(main.app, ["set-command", "my-ws", "pi"])
            assert result.exit_code == 0

        client.put.assert_called_once()
        call_args = client.put.call_args
        assert call_args[0][0] == f"/workspaces/{ws.id}"
        assert call_args[1]["json"] == {"default_command": "pi"}

    def test_set_command_clear(self, logged_in_cfg, monkeypatch):
        from bark_backend.cli import main

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
            result = runner.invoke(main.app, ["set-command", "my-ws"])
            assert result.exit_code == 0

        call_args = client.put.call_args
        assert call_args[1]["json"] == {"default_command": None}

    def test_set_command_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from bark_backend.cli import main

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")

        with patch.object(main, "_client", return_value=client):
            from typer.testing import CliRunner

            runner = CliRunner()
            result = runner.invoke(main.app, ["set-command", "nope", "pi"])
            assert result.exit_code == 1

    def test_set_command_404_from_server(self, logged_in_cfg, monkeypatch):
        from bark_backend.cli import main

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
            result = runner.invoke(main.app, ["set-command", "my-ws", "pi"])
            assert result.exit_code == 1

    def test_exec_runs_command(self, logged_in_cfg, monkeypatch):
        from bark_backend.cli import main
        from bark_backend.cli.client import Workspace

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
        from bark_backend.cli import main

        ctx = MagicMock()
        ctx.args = []
        with pytest.raises(typer.Exit) as exc_info:
            main.exec_cmd(ctx, workspace="my-ws")
        assert exc_info.value.exit_code == 1

    def test_exec_workspace_not_found(self, logged_in_cfg, monkeypatch):
        from bark_backend.cli import main
        from bark_backend.cli.client import WorkspaceNotFoundError

        client = MagicMock()
        client.resolve_workspace.side_effect = WorkspaceNotFoundError("nope")
        monkeypatch.setattr(main, "_client", lambda: client)

        ctx = MagicMock()
        ctx.args = ["ls"]
        with pytest.raises(typer.Exit) as exc_info:
            main.exec_cmd(ctx, workspace="nope")
        assert exc_info.value.exit_code == 1

    def test_sync_runs_rsync(self, logged_in_cfg):
        from bark_backend.cli import main

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
        assert "bark exec" in " ".join(cmd)

    def test_sync_no_rsync(self, logged_in_cfg):
        from bark_backend.cli import main

        def which_no_rsync(name):
            return "/usr/bin/bark" if name == "bark" else None

        ctx = MagicMock()
        ctx.args = []
        with patch("shutil.which", side_effect=which_no_rsync):
            with pytest.raises(typer.Exit) as exc_info:
                main.sync(ctx, src="/tmp/foo", dest="ws:/work/foo")
        assert exc_info.value.exit_code == 1

    def test_sync_passes_extra_args(self, logged_in_cfg):
        from bark_backend.cli import main

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
        from bark_backend.cli import main

        ctx = MagicMock()
        ctx.args = []
        mock_result = MagicMock()
        mock_result.returncode = 23
        with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}"):
            with patch("subprocess.run", return_value=mock_result):
                with pytest.raises(typer.Exit) as exc_info:
                    main.sync(ctx, src="/tmp/foo", dest="ws:/work/foo")
        assert exc_info.value.exit_code == 23
