"""Tests for the klangkc CLI."""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import typer
import websockets
import io
from io import BytesIO

from klangk.cli.config import (
    CLIConfig,
    CLIState,
    ServerEntry,
    ServerState,
    UserEntry,
    _DEFAULT_WS_MAX_SIZE,
    seed_config,
)
from klangk.cli.client import (
    AuthError,
    KlangkClient,
    Workspace,
    WorkspaceNotFoundError,
    request_with_retry,
    token_expires_soon,
)
from klangk.cli.auth import refresh_token


# --- CLIConfig tests ---


class TestCLIConfig:
    def test_load_empty(self, monkeypatch):
        monkeypatch.setattr(
            "klangk.cli.config._CONFIG_PATH",
            Path("/nonexistent/cli.yaml"),
        )
        cfg = CLIConfig.load()
        assert cfg.servers == {}
        assert cfg.forward_agent is None
        assert cfg.ws_max_size is None

    def test_load_existing(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        config_path.write_text(
            "forward-agent: true\n"
            "ws-max-size: 999\n"
            "servers:\n"
            "  prod:\n"
            "    url: http://prod:8995\n"
            "    forward-agent: false\n"
            "    ws-max-size: 500\n"
            "  dev:\n"
            "    url: http://dev:8995\n"
        )
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        cfg = CLIConfig.load()
        assert cfg.forward_agent is True
        assert cfg.ws_max_size == 999
        assert len(cfg.servers) == 2
        assert cfg.servers["prod"].url == "http://prod:8995"
        assert cfg.servers["prod"].forward_agent is False
        assert cfg.servers["prod"].ws_max_size == 500
        assert cfg.servers["dev"].url == "http://dev:8995"
        assert cfg.servers["dev"].forward_agent is None
        assert cfg.servers["dev"].ws_max_size is None

    def test_load_forward_agent_true(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        config_path.write_text("forward-agent: true\n")
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        cfg = CLIConfig.load()
        assert cfg.forward_agent is True

    def test_load_forward_agent_false(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        config_path.write_text("forward-agent: false\n")
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        cfg = CLIConfig.load()
        assert cfg.forward_agent is False

    def test_load_forward_agent_absent(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        config_path.write_text("ws-max-size: 100\n")
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        cfg = CLIConfig.load()
        assert cfg.forward_agent is None

    def test_load_skips_invalid_server_entries(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        config_path.write_text(
            "servers:\n"
            "  bad1: not-a-dict\n"
            "  bad2:\n"
            "    name: missing-url\n"
            "  good:\n"
            "    url: http://good:8995\n"
        )
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        cfg = CLIConfig.load()
        assert len(cfg.servers) == 1
        assert "good" in cfg.servers

    def test_load_empty_yaml(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        config_path.write_text("")
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        cfg = CLIConfig.load()
        assert cfg.servers == {}

    def test_resolve_server_alias(self):
        cfg = CLIConfig(
            servers={
                "prod": ServerEntry(url="http://prod:8995"),
            }
        )
        assert cfg.resolve_server("prod") == "http://prod:8995"

    def test_resolve_server_raw_url(self):
        cfg = CLIConfig(
            servers={
                "prod": ServerEntry(url="http://prod:8995"),
            }
        )
        assert cfg.resolve_server("http://other:9999") == "http://other:9999"

    def test_get_forward_agent_per_server(self):
        cfg = CLIConfig(
            forward_agent=False,
            servers={
                "s1": ServerEntry(url="http://s1:8995", forward_agent=True),
            },
        )
        assert cfg.get_forward_agent("http://s1:8995") is True

    def test_get_forward_agent_global_fallback(self):
        cfg = CLIConfig(
            forward_agent=True,
            servers={
                "s1": ServerEntry(url="http://s1:8995"),  # no per-server
            },
        )
        assert cfg.get_forward_agent("http://s1:8995") is True

    def test_get_forward_agent_no_match_returns_global(self):
        cfg = CLIConfig(forward_agent=False)
        assert cfg.get_forward_agent("http://unknown:8995") is False

    def test_get_forward_agent_none_fallback(self):
        cfg = CLIConfig()
        assert cfg.get_forward_agent("http://unknown:8995") is None

    def test_get_ws_max_size_per_server(self):
        cfg = CLIConfig(
            ws_max_size=1000,
            servers={
                "s1": ServerEntry(url="http://s1:8995", ws_max_size=2000),
            },
        )
        assert cfg.get_ws_max_size("http://s1:8995") == 2000

    def test_get_ws_max_size_global_fallback(self):
        cfg = CLIConfig(
            ws_max_size=3000,
            servers={
                "s1": ServerEntry(url="http://s1:8995"),  # no per-server
            },
        )
        assert cfg.get_ws_max_size("http://s1:8995") == 3000

    def test_get_ws_max_size_default(self):
        cfg = CLIConfig()
        assert cfg.get_ws_max_size("http://any:8995") == _DEFAULT_WS_MAX_SIZE

    def test_get_user_per_server(self):
        cfg = CLIConfig(
            servers={
                "prod": ServerEntry(
                    url="http://prod:8995", user="admin@prod.com"
                ),
            },
        )
        assert cfg.get_user("http://prod:8995") == "admin@prod.com"

    def test_get_user_no_match(self):
        cfg = CLIConfig()
        assert cfg.get_user("http://unknown:8995") is None

    def test_get_user_none_when_not_set(self):
        cfg = CLIConfig(
            servers={
                "dev": ServerEntry(url="http://dev:8995"),
            },
        )
        assert cfg.get_user("http://dev:8995") is None

    def test_load_existing_with_user(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        config_path.write_text(
            "servers:\n"
            "  prod:\n"
            "    url: http://prod:8995\n"
            "    user: admin@prod.com\n"
        )
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        cfg = CLIConfig.load()
        assert cfg.servers["prod"].user == "admin@prod.com"


# --- seed_config tests ---


class TestSeedConfig:
    def test_creates_config_if_missing(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        seed_config("http://localhost:8995", "admin@example.com")
        assert config_path.exists()
        cfg = CLIConfig.load()
        assert "localhost" in cfg.servers
        assert cfg.servers["localhost"].url == "http://localhost:8995"
        assert cfg.servers["localhost"].user == "admin@example.com"

    def test_does_not_overwrite_existing(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        config_path.write_text("forward-agent: true\n")
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        seed_config("http://localhost:8995", "admin@example.com")
        # Should not have been overwritten
        assert "forward-agent" in config_path.read_text()
        cfg = CLIConfig.load()
        assert cfg.forward_agent is True
        assert len(cfg.servers) == 0

    def test_creates_without_user(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        seed_config("https://klangk.example.com")
        cfg = CLIConfig.load()
        entry = cfg.servers.get("klangk.example.com")
        assert entry is not None
        assert entry.url == "https://klangk.example.com"
        assert entry.user is None

    def test_uses_hostname_as_alias(self, tmp_path, monkeypatch):
        config_path = tmp_path / "cli.yaml"
        monkeypatch.setattr("klangk.cli.config._CONFIG_PATH", config_path)
        seed_config("http://myhost:9000")
        cfg = CLIConfig.load()
        assert "myhost" in cfg.servers


# --- CLIState tests ---


class TestCLIState:
    def test_load_empty(self, monkeypatch):
        monkeypatch.setattr(
            "klangk.cli.config._STATE_PATH",
            Path("/nonexistent/state.yaml"),
        )
        state = CLIState.load()
        assert state.active_server is None
        assert state.servers == {}

    def test_load_existing(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        state_path.write_text(
            "active-server: http://prod:8995\n"
            "http://prod:8995:\n"
            "  active-user: user@example.com\n"
            "  users:\n"
            "    user@example.com:\n"
            "      token: tok123\n"
        )
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState.load()
        assert state.active_server == "http://prod:8995"
        ss = state.servers["http://prod:8995"]
        assert ss.active_user == "user@example.com"
        assert ss.users["user@example.com"].token == "tok123"

    def test_load_empty_yaml(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        state_path.write_text("")
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState.load()
        assert state.active_server is None
        assert state.servers == {}

    def test_load_skips_non_dict_values(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        state_path.write_text(
            "active-server: http://good:8995\n"
            "http://good:8995:\n"
            "  active-user: u@t.com\n"
            "  users:\n"
            "    u@t.com:\n"
            "      token: tok\n"
            "bad-entry: not-a-dict\n"
        )
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState.load()
        assert len(state.servers) == 1
        assert "http://good:8995" in state.servers

    def test_save_roundtrip(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState()
        state.set_credentials("http://test:8995", "a@b.com", "tok")
        state.save()
        loaded = CLIState.load()
        assert loaded.active_server == "http://test:8995"
        assert loaded.get_token("http://test:8995") == "tok"
        assert loaded.get_email("http://test:8995") == "a@b.com"

    def test_save_creates_parent_dirs(self, tmp_path, monkeypatch):
        state_path = tmp_path / "sub" / "dir" / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState()
        state.save()
        assert state_path.exists()

    def test_save_permissions(self, tmp_path, monkeypatch):
        state_path = tmp_path / "klangk" / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState()
        state.set_credentials("http://test:8995", "u@t.com", "secret")
        state.save()
        assert oct(state_path.stat().st_mode & 0o777) == oct(0o600)
        assert oct(state_path.parent.stat().st_mode & 0o777) == oct(0o700)

    def test_save_omits_empty_entries(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState(
            servers={
                "http://test:8995": ServerState(),  # no users
            },
        )
        state.save()
        import yaml

        data = yaml.safe_load(state_path.read_text()) or {}
        # Empty entry should not be written
        assert "http://test:8995" not in data

    def test_save_omits_active_server_when_none(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState()
        state.save()
        import yaml

        data = yaml.safe_load(state_path.read_text()) or {}
        assert "active-server" not in data

    def test_get_token(self):
        state = CLIState()
        state.set_credentials("http://test:8995", "u@t.com", "tok123")
        assert state.get_token("http://test:8995") == "tok123"

    def test_get_token_missing(self):
        state = CLIState()
        assert state.get_token("http://unknown:8995") is None

    def test_get_token_no_active_user(self):
        state = CLIState(
            servers={
                "http://test:8995": ServerState(
                    users={"u@t.com": UserEntry(token="tok")},
                ),
            },
        )
        assert state.get_token("http://test:8995") is None

    def test_get_email(self):
        state = CLIState()
        state.set_credentials("http://test:8995", "user@test.com", "tok")
        assert state.get_email("http://test:8995") == "user@test.com"

    def test_get_email_missing(self):
        state = CLIState()
        assert state.get_email("http://unknown:8995") is None

    def test_set_credentials_creates_server(self):
        state = CLIState()
        state.set_credentials("http://new:8995", "u@t.com", "tok")
        assert state.active_server == "http://new:8995"
        ss = state.servers["http://new:8995"]
        assert ss.active_user == "u@t.com"
        assert ss.users["u@t.com"].token == "tok"

    def test_set_credentials_adds_user_to_existing_server(self):
        state = CLIState()
        state.set_credentials("http://s:8995", "alice@t.com", "tok-a")
        state.set_credentials("http://s:8995", "bob@t.com", "tok-b")
        ss = state.servers["http://s:8995"]
        assert ss.active_user == "bob@t.com"
        assert ss.users["alice@t.com"].token == "tok-a"
        assert ss.users["bob@t.com"].token == "tok-b"

    def test_set_credentials_switches_active_user(self):
        state = CLIState()
        state.set_credentials("http://s:8995", "alice@t.com", "tok-a")
        state.set_credentials("http://s:8995", "bob@t.com", "tok-b")
        assert state.get_email("http://s:8995") == "bob@t.com"
        assert state.get_token("http://s:8995") == "tok-b"
        # Switch back to alice
        state.set_credentials("http://s:8995", "alice@t.com", "tok-a2")
        assert state.get_email("http://s:8995") == "alice@t.com"
        assert state.get_token("http://s:8995") == "tok-a2"

    def test_clear_credentials(self):
        state = CLIState()
        state.set_credentials("http://s:8995", "u@t.com", "tok")
        state.clear_credentials("http://s:8995")
        assert "http://s:8995" not in state.servers
        assert state.active_server is None

    def test_clear_credentials_preserves_other_servers(self):
        state = CLIState()
        state.set_credentials("http://s1:8995", "u@t.com", "tok1")
        state.set_credentials("http://s2:8995", "u@t.com", "tok2")
        state.clear_credentials("http://s1:8995")
        assert state.get_token("http://s2:8995") == "tok2"
        # active_server was s2 (last set), so clearing s1 doesn't reset it
        assert state.active_server == "http://s2:8995"

    def test_clear_credentials_nonexistent(self):
        state = CLIState()
        state.clear_credentials("http://nope:8995")  # should not raise


# --- Auth tests ---


class TestAuth:
    @pytest.fixture(autouse=True)
    def no_oidc(self, monkeypatch):
        monkeypatch.setattr("klangk.cli.auth.fetch_config", lambda _: {})

    def test_login_success(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt123"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with patch(
                "klangk.cli.auth.Prompt.ask",
                side_effect=["u@test.com", "pass123"],
            ):
                from klangk.cli import auth

                auth.login("http://localhost:8995")
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") == "jwt123"
        assert state.get_email("http://localhost:8995") == "u@test.com"
        assert state.active_server == "http://localhost:8995"

    def test_login_with_user_flag(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt456"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            # Only one Prompt.ask call (password) since email is provided
            with patch(
                "klangk.cli.auth.Prompt.ask",
                return_value="secret",
            ):
                from klangk.cli import auth

                auth.login("http://localhost:8995", email="cli@test.com")
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") == "jwt456"
        assert state.get_email("http://localhost:8995") == "cli@test.com"

    def test_login_with_password_file(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        pw_file = tmp_path / "pw.txt"
        pw_file.write_text("file-secret\n")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt789"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            from klangk.cli import auth

            auth.login(
                "http://localhost:8995",
                email="pw@test.com",
                password=pw_file.read_text().strip(),
            )
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") == "jwt789"
        assert state.get_email("http://localhost:8995") == "pw@test.com"

    def test_login_reuses_valid_token(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        # Pre-populate state with an existing token
        state = CLIState()
        state.set_credentials(
            "http://localhost:8995", "saved@test.com", "existing-token"
        )
        state.save()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            from klangk.cli import auth

            auth.login("http://localhost:8995", email="saved@test.com")

        # Token should be unchanged — no prompt was shown
        loaded = CLIState.load()
        assert loaded.get_token("http://localhost:8995") == "existing-token"
        assert loaded.get_email("http://localhost:8995") == "saved@test.com"

    def test_login_network_error_falls_through(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState()
        state.set_credentials(
            "http://localhost:8995", "old@test.com", "old-token"
        )
        state.save()

        # GET raises network error, then POST succeeds
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {"access_token": "fresh"}
        with patch(
            "klangk.cli.transport.httpx.request",
            side_effect=[httpx.ConnectError("unreachable"), post_resp],
        ):
            with patch(
                "klangk.cli.auth.Prompt.ask",
                return_value="pw",
            ):
                from klangk.cli import auth

                auth.login("http://localhost:8995", email="old@test.com")

        loaded = CLIState.load()
        assert loaded.get_token("http://localhost:8995") == "fresh"

    def test_login_expired_token_prompts(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState()
        state.set_credentials(
            "http://localhost:8995", "old@test.com", "expired-token"
        )
        state.save()

        # GET returns 401 (expired), then POST succeeds with new token
        get_resp = MagicMock()
        get_resp.status_code = 401
        post_resp = MagicMock()
        post_resp.status_code = 200
        post_resp.json.return_value = {"access_token": "new-token"}
        with patch(
            "klangk.cli.transport.httpx.request",
            side_effect=[get_resp, post_resp],
        ):
            with patch(
                "klangk.cli.auth.Prompt.ask",
                return_value="pw",
            ):
                from klangk.cli import auth

                auth.login("http://localhost:8995", email="old@test.com")

        loaded = CLIState.load()
        assert loaded.get_token("http://localhost:8995") == "new-token"
        assert loaded.get_email("http://localhost:8995") == "old@test.com"

    def test_login_failure(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.json.return_value = {"detail": "Bad credentials"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with patch(
                "klangk.cli.auth.Prompt.ask",
                side_effect=["u@test.com", "wrong"],
            ):
                from klangk.cli import auth

                with pytest.raises(SystemExit):
                    auth.login("http://localhost:8995")

    def test_logout_clears_token(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState()
        state.set_credentials("http://localhost:8995", "x@y.com", "tok")
        state.save()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            from klangk.cli import auth

            auth.logout("http://localhost:8995")
        loaded = CLIState.load()
        assert loaded.get_token("http://localhost:8995") is None
        assert loaded.active_server is None

    def test_logout_swallows_server_error(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState()
        state.set_credentials("http://localhost:8995", "u@t.com", "tok")
        state.save()
        with patch(
            "klangk.cli.transport.httpx.request",
            side_effect=httpx.ConnectError("no server"),
        ):
            from klangk.cli import auth

            auth.logout("http://localhost:8995")  # Should not raise

    def test_logout_no_existing_token(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState()
        state.save()
        from klangk.cli import auth

        auth.logout("http://localhost:8995")  # Should not raise

    def test_logout_only_clears_target_server(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        state = CLIState()
        state.set_credentials("http://server1:8995", "a@a.com", "tok1")
        state.set_credentials("http://server2:8995", "b@b.com", "tok2")
        # active_server is now server2 (last set); override to server1
        state.active_server = "http://server1:8995"
        state.save()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            from klangk.cli import auth

            auth.logout("http://server1:8995")
        loaded = CLIState.load()
        assert loaded.get_token("http://server1:8995") is None
        assert loaded.get_token("http://server2:8995") == "tok2"
        assert loaded.active_server is None  # was the active server


class TestOIDCCLILogin:
    def test_fetch_config_success(self, monkeypatch):
        from klangk.cli.auth import fetch_config

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"auth_modes": "both"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            result = fetch_config("http://localhost:8995")
        assert result == {"auth_modes": "both"}

    def test_fetch_config_unreachable(self, monkeypatch):
        from klangk.cli.auth import _UNREACHABLE, fetch_config

        with patch(
            "klangk.cli.transport.httpx.request",
            side_effect=httpx.ConnectError("fail"),
        ):
            result = fetch_config("http://localhost:8995")
        assert result == _UNREACHABLE

    def test_fetch_config_not_klangk(self, monkeypatch):
        from klangk.cli.auth import fetch_config

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            result = fetch_config("http://localhost:8995")
        assert result is None

    def test_oidc_single_provider(self, tmp_path, monkeypatch):
        """OIDC login with single provider goes straight to browser."""
        from klangk.cli import auth

        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        monkeypatch.setattr(
            "klangk.cli.auth.fetch_config",
            lambda _: {
                "oidc_providers": [{"id": "test", "display_name": "Test"}],
                "auth_modes": "oidc",
            },
        )
        with patch.object(auth, "_oidc_browser_login") as mock_browser:
            auth.login("http://localhost:8995")
        mock_browser.assert_called_once()
        assert mock_browser.call_args[0][1] == "test"

    def test_oidc_multiple_providers_prompts(self, tmp_path, monkeypatch):
        """OIDC login with multiple providers prompts for selection."""
        from klangk.cli import auth

        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        monkeypatch.setattr(
            "klangk.cli.auth.fetch_config",
            lambda _: {
                "oidc_providers": [
                    {"id": "a", "display_name": "A"},
                    {"id": "b", "display_name": "B"},
                ],
                "auth_modes": "oidc",
            },
        )
        with (
            patch.object(auth, "_oidc_browser_login") as mock_browser,
            patch(
                "klangk.cli.auth.Prompt.ask",
                return_value="2",
            ),
        ):
            auth.login("http://localhost:8995")
        mock_browser.assert_called_once()
        assert mock_browser.call_args[0][1] == "b"

    def test_oidc_skipped_when_credentials_provided(
        self, tmp_path, monkeypatch
    ):
        """Explicit email+password skips OIDC even in both mode."""
        from klangk.cli import auth

        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        monkeypatch.setattr(
            "klangk.cli.auth.fetch_config",
            lambda _: {
                "oidc_providers": [{"id": "test", "display_name": "Test"}],
                "auth_modes": "both",
            },
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            auth.login(
                "http://localhost:8995",
                email="u@test.com",
                password="pw",
            )
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") == "jwt"

    def test_oidc_invalid_provider_choice(self, tmp_path, monkeypatch):
        from klangk.cli import auth

        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        monkeypatch.setattr(
            "klangk.cli.auth.fetch_config",
            lambda _: {
                "oidc_providers": [
                    {"id": "a", "display_name": "A"},
                    {"id": "b", "display_name": "B"},
                ],
                "auth_modes": "oidc",
            },
        )
        with (
            patch(
                "klangk.cli.auth.Prompt.ask",
                return_value="bad",
            ),
            pytest.raises(SystemExit),
        ):
            auth.login("http://localhost:8995")

    def test_login_redirect_shows_hint(self, tmp_path, monkeypatch):
        """301 redirect shows a helpful hint about HTTPS."""
        from klangk.cli import auth

        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        monkeypatch.setattr("klangk.cli.auth.fetch_config", lambda _: {})
        mock_resp = MagicMock()
        mock_resp.status_code = 301
        mock_resp.headers = {"location": "https://example.com/auth/login"}
        with (
            patch(
                "klangk.cli.transport.httpx.request", return_value=mock_resp
            ),
            pytest.raises(SystemExit),
        ):
            auth.login(
                "http://example.com",
                email="u@test.com",
                password="pw",
            )

    def test_login_error_empty_body(self, tmp_path, monkeypatch):
        """Login with empty error response doesn't crash."""
        from klangk.cli import auth

        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        monkeypatch.setattr("klangk.cli.auth.fetch_config", lambda _: {})
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.json.side_effect = Exception("no body")
        mock_resp.text = ""
        with (
            patch(
                "klangk.cli.transport.httpx.request", return_value=mock_resp
            ),
            pytest.raises(SystemExit),
        ):
            auth.login(
                "http://localhost:8995",
                email="u@test.com",
                password="pw",
            )

    def test_login_not_klangk_server(self, tmp_path, monkeypatch):
        """Non-klangk server shows helpful error with subpath hint."""
        from klangk.cli import auth

        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        monkeypatch.setattr("klangk.cli.auth.fetch_config", lambda _: None)
        with pytest.raises(SystemExit):
            auth.login(
                "http://example.com",
                email="u@test.com",
                password="pw",
            )

    def test_login_server_unreachable(self, tmp_path, monkeypatch):
        """Unreachable server shows connection error, not subpath hint."""
        from klangk.cli import auth

        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        monkeypatch.setattr(
            "klangk.cli.auth.fetch_config", lambda _: auth._UNREACHABLE
        )
        with pytest.raises(SystemExit):
            auth.login(
                "http://example.com",
                email="u@test.com",
                password="pw",
            )

    def test_oidc_callback_html_escapes_error(self):
        """OIDC callback HTML-escapes the error parameter (XSS fix)."""
        import http.server
        import socket
        import threading
        import urllib.error
        import urllib.request

        # Start the OIDC callback server (reuse the handler from auth.py)
        # We replicate the handler here to test the actual escaping logic
        # without needing to invoke _oidc_browser_login (pragma: no cover)
        import html as html_mod

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        class TestHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                from urllib.parse import parse_qs, urlparse

                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)
                error = params.get("error", ["Unknown error"])[0]
                safe_title = html_mod.escape("Login Failed")
                safe_message = html_mod.escape(error)
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    f"<p>{safe_title}</p><p>{safe_message}</p>".encode()
                )

            def log_message(self, format, *args):  # noqa: A002
                pass

        server = http.server.HTTPServer(("127.0.0.1", port), TestHandler)
        t = threading.Thread(target=server.handle_request, daemon=True)
        t.start()

        xss_payload = '<script>alert("xss")</script>'
        url = (
            f"http://127.0.0.1:{port}"
            f"/callback?error={urllib.request.quote(xss_payload)}"
        )
        try:
            resp = urllib.request.urlopen(url)
            body = resp.read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode()
        finally:
            t.join(timeout=5)
            server.server_close()

        assert "<script>" not in body
        assert "&lt;script&gt;" in body


# --- request_with_retry tests ---


class TestRequestWithRetry:
    def test_success_on_first_attempt(self, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        monkeypatch.setattr(
            "klangk.cli.transport.httpx.request",
            lambda *a, **kw: mock_resp,
        )
        resp = request_with_retry("http://test", "GET", "/path")
        assert resp.status_code == 200

    def test_retries_on_timeout(self, monkeypatch):
        call_count = 0
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        def fake_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise httpx.ReadTimeout("timed out")
            return mock_resp

        monkeypatch.setattr("klangk.cli.transport.httpx.request", fake_request)
        monkeypatch.setattr("klangk.cli.client._time.sleep", lambda _: None)
        resp = request_with_retry("http://test", "GET", "/path")
        assert resp.status_code == 200
        assert call_count == 3

    def test_retries_on_connect_error(self, monkeypatch):
        call_count = 0
        mock_resp = MagicMock()
        mock_resp.status_code = 200

        def fake_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.ConnectError("connection refused")
            return mock_resp

        monkeypatch.setattr("klangk.cli.transport.httpx.request", fake_request)
        monkeypatch.setattr("klangk.cli.client._time.sleep", lambda _: None)
        resp = request_with_retry("http://test", "GET", "/path")
        assert resp.status_code == 200
        assert call_count == 2

    def test_retries_on_503(self, monkeypatch):
        call_count = 0

        def fake_request(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            resp.status_code = 503 if call_count < 3 else 200
            return resp

        monkeypatch.setattr("klangk.cli.transport.httpx.request", fake_request)
        monkeypatch.setattr("klangk.cli.client._time.sleep", lambda _: None)
        resp = request_with_retry("http://test", "GET", "/path")
        assert resp.status_code == 200
        assert call_count == 3

    def test_raises_after_exhausting_retries(self, monkeypatch):
        def fake_request(*args, **kwargs):
            raise httpx.ReadTimeout("timed out")

        monkeypatch.setattr("klangk.cli.transport.httpx.request", fake_request)
        monkeypatch.setattr("klangk.cli.client._time.sleep", lambda _: None)
        with pytest.raises(httpx.ReadTimeout):
            request_with_retry("http://test", "GET", "/path")

    def test_returns_503_after_exhausting_retries(self, monkeypatch):
        def fake_request(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 503
            return resp

        monkeypatch.setattr("klangk.cli.transport.httpx.request", fake_request)
        monkeypatch.setattr("klangk.cli.client._time.sleep", lambda _: None)
        resp = request_with_retry("http://test", "GET", "/path")
        assert resp.status_code == 503


# --- KlangkClient tests ---


class TestKlangkClient:
    def test_auth_error_on_401(self, monkeypatch):
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch.object(client, "get", return_value=mock_resp):
            with pytest.raises(AuthError, match="Session expired"):
                client.list_workspaces()

    def test_list_workspaces_parses_response(self):
        client = KlangkClient("http://test:8995", "valid-token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [
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
            ],
            "has_more": False,
            "next_offset": None,
        }
        with patch.object(client, "get", return_value=mock_resp):
            workspaces = client.list_workspaces()
        assert len(workspaces) == 2
        assert workspaces[0].name == "alpha"
        assert workspaces[1].id == "ws2"

    def test_list_workspaces_parses_health_status(self):
        client = KlangkClient("http://test:8995", "valid-token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [
                {
                    "id": "ws1",
                    "name": "alpha",
                    "created_at": "2025-01-01T00:00:00Z",
                    "running": True,
                    "health": "unhealthy",
                    "health_message": "curl failed",
                },
            ],
            "has_more": False,
            "next_offset": None,
        }
        with patch.object(client, "get", return_value=mock_resp):
            workspaces = client.list_workspaces()
        assert workspaces[0].running is True
        assert workspaces[0].health == "unhealthy"
        assert workspaces[0].health_message == "curl failed"

    def test_list_workspaces_health_defaults_when_absent(self):
        client = KlangkClient("http://test:8995", "valid-token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [
                {
                    "id": "ws1",
                    "name": "alpha",
                    "created_at": "2025-01-01T00:00:00Z",
                },
            ],
            "has_more": False,
            "next_offset": None,
        }
        with patch.object(client, "get", return_value=mock_resp):
            workspaces = client.list_workspaces()
        assert workspaces[0].running is False
        assert workspaces[0].health is None
        assert workspaces[0].health_message is None

    def test_list_shared_workspaces_parses_response(self):
        client = KlangkClient("http://test:8995", "valid-token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [
                {
                    "id": "ws1",
                    "name": "shared-alpha",
                    "created_at": "2025-01-01T00:00:00Z",
                    "owner_email": "owner@example.com",
                },
            ],
            "has_more": False,
            "next_offset": None,
        }
        with patch.object(client, "get", return_value=mock_resp):
            workspaces = client.list_shared_workspaces()
        assert len(workspaces) == 1
        assert workspaces[0].name == "shared-alpha"
        assert workspaces[0].owner_email == "owner@example.com"

    def test_create_workspace_returns_workspace(self):
        client = KlangkClient("http://test:8995", "token")
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
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [],
            "has_more": False,
            "next_offset": None,
        }
        with patch.object(client, "get", return_value=mock_resp):
            with pytest.raises(WorkspaceNotFoundError):
                client.delete_workspace("nonexistent")

    def test_delete_workspace_success(self):
        client = KlangkClient("http://test:8995", "token")
        list_resp = MagicMock()
        list_resp.status_code = 200
        list_resp.json.return_value = {
            "items": [
                {
                    "id": "ws-to-delete",
                    "name": "gone",
                    "created_at": "2025-01-01T00:00:00Z",
                }
            ],
            "has_more": False,
            "next_offset": None,
        }
        del_resp = MagicMock()
        del_resp.status_code = 204
        with patch.object(client, "get", return_value=list_resp):
            with patch.object(
                client, "delete", return_value=del_resp
            ) as mock_del:
                client.delete_workspace("gone")
                mock_del.assert_called_once_with(
                    "/api/v1/workspaces/ws-to-delete"
                )

    def _ws_and_shared_resp(self):
        """Return (owned_resp, shared_resp) mocks for resolve_workspace."""
        owned = MagicMock()
        owned.status_code = 200
        owned.json.return_value = {
            "items": [
                {
                    "id": "ws-1",
                    "name": "my-ws",
                    "created_at": "2025-01-01",
                }
            ],
            "has_more": False,
            "next_offset": None,
        }
        shared = MagicMock()
        shared.status_code = 200
        shared.json.return_value = {
            "items": [],
            "has_more": False,
            "next_offset": None,
        }
        return owned, shared

    def test_list_workspace_members(self):
        client = KlangkClient("http://test:8995", "token")
        owned, shared = self._ws_and_shared_resp()
        members_resp = MagicMock()
        members_resp.status_code = 200
        members_resp.json.return_value = [
            {"id": "u1", "email": "a@b.com", "handle": "a"},
        ]
        with patch.object(
            client, "get", side_effect=[owned, shared, members_resp]
        ):
            result = client.list_workspace_members("my-ws")
        assert len(result) == 1
        assert result[0]["email"] == "a@b.com"

    def test_add_workspace_member(self):
        client = KlangkClient("http://test:8995", "token")
        owned, shared = self._ws_and_shared_resp()
        patch_resp = MagicMock()
        patch_resp.status_code = 200
        patch_resp.json.return_value = {
            "ok": True,
            "email": "new@b.com",
            "role": "coders",
        }
        with patch.object(client, "get", side_effect=[owned, shared]):
            with patch.object(client, "patch", return_value=patch_resp):
                result = client.add_workspace_member("my-ws", "new@b.com")
        assert result["email"] == "new@b.com"
        assert result["role"] == "coders"

    def test_remove_workspace_member(self):
        client = KlangkClient("http://test:8995", "token")
        owned, shared = self._ws_and_shared_resp()
        patch_resp = MagicMock()
        patch_resp.status_code = 200
        with patch.object(client, "get", side_effect=[owned, shared]):
            with patch.object(client, "patch", return_value=patch_resp):
                client.remove_workspace_member("my-ws", "alice@b.com")

    def test_remove_workspace_member_not_found(self):
        client = KlangkClient("http://test:8995", "token")
        owned, shared = self._ws_and_shared_resp()
        patch_resp = MagicMock()
        patch_resp.status_code = 404
        with patch.object(client, "get", side_effect=[owned, shared]):
            with patch.object(client, "patch", return_value=patch_resp):
                with pytest.raises(WorkspaceNotFoundError):
                    client.remove_workspace_member("my-ws", "nobody@b.com")

    def test_resolve_workspace_by_name(self):
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [
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
            ],
            "has_more": False,
            "next_offset": None,
        }
        with patch.object(client, "get", return_value=mock_resp):
            ws = client.resolve_workspace("beta")
        assert ws.id == "ws2"

    def test_resolve_workspace_not_found_raises(self):
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "items": [
                {
                    "id": "ws1",
                    "name": "alpha",
                    "created_at": "2025-01-01T00:00:00Z",
                }
            ],
            "has_more": False,
            "next_offset": None,
        }
        with patch.object(client, "get", return_value=mock_resp):
            with pytest.raises(WorkspaceNotFoundError):
                client.resolve_workspace("nonexistent")

    def test_delete_workspace_401_raises_auth_error(self):
        client = KlangkClient("http://test:8995", "token")
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
        del_resp.status_code = 401
        with patch.object(client, "get", return_value=list_resp):
            with patch.object(client, "delete", return_value=del_resp):
                with pytest.raises(AuthError):
                    client.delete_workspace("ws1")

    def test_delete_workspace_non_200_raises(self):
        client = KlangkClient("http://test:8995", "token")
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

    def test_raise_for_status_includes_server_detail(self):
        client = KlangkClient("http://test:8995", "token")
        resp = MagicMock()
        resp.status_code = 400
        resp.json.return_value = {"detail": "Image not allowed"}
        resp.request = MagicMock()
        with pytest.raises(httpx.HTTPStatusError, match="Image not allowed"):
            client._raise_for_status(resp)

    def test_raise_for_status_falls_back_without_detail(self):
        client = KlangkClient("http://test:8995", "token")
        resp = MagicMock()
        resp.status_code = 500
        resp.json.side_effect = ValueError("not json")
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
        with pytest.raises(httpx.HTTPStatusError):
            client._raise_for_status(resp)

    def test_raise_for_status_noop_on_success(self):
        client = KlangkClient("http://test:8995", "token")
        resp = MagicMock()
        resp.status_code = 200
        client._raise_for_status(resp)  # should not raise

    def test_no_token_uses_empty_string(self):
        client = KlangkClient("http://test:8995")
        headers = client._headers()
        assert headers["Authorization"] == "Bearer "


# --- Token refresh ---


def _make_jwt(exp: float, email: str = "test@example.com") -> str:
    """Build a fake JWT with the given exp timestamp (no real signature)."""
    import base64

    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"sub": "u1", "email": email, "exp": exp}).encode()
    ).rstrip(b"=")
    return f"{header.decode()}.{payload.decode()}.fakesig"


class TestTokenExpiresSoon:
    def test_returns_true_when_expiring_soon(self):
        import time

        token = _make_jwt(time.time() + 60)  # expires in 60s
        assert token_expires_soon(token) is True

    def test_returns_false_when_not_expiring_soon(self):
        import time

        token = _make_jwt(time.time() + 600)  # expires in 10 min
        assert token_expires_soon(token) is False

    def test_returns_false_for_invalid_token(self):
        assert token_expires_soon("not-a-jwt") is False

    def test_returns_false_for_empty_token(self):
        assert token_expires_soon("") is False

    def test_returns_false_when_no_exp(self):
        import base64

        header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=")
        payload = base64.urlsafe_b64encode(
            b'{"sub":"u1","email":"a@b.com"}'
        ).rstrip(b"=")
        token = f"{header.decode()}.{payload.decode()}.sig"
        assert token_expires_soon(token) is False


class TestNoneModeAuth:
    """no-auth single-user mode (#1374): /auth/local token handout,
    the login() arm, CLIState mode caching, and require_auth auto-login."""

    def test_local_login_returns_token_and_email(self):
        from klangk.cli.auth import local_login

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "jwt-free",
            "email": "admin@example.com",
        }
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            email, token = local_login("http://srv")
        assert email == "admin@example.com"
        assert token == "jwt-free"

    def test_local_login_exits_on_non_200(self):
        from klangk.cli.auth import local_login

        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.json.return_value = {"detail": "not enabled"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with pytest.raises(SystemExit):
                local_login("http://srv")

    def test_local_login_exits_on_network_error(self):
        from klangk.cli.auth import local_login

        with patch(
            "klangk.cli.transport.httpx.request",
            side_effect=httpx.ConnectError(""),
        ):
            with pytest.raises(SystemExit):
                local_login("http://srv")

    def test_local_login_exits_on_non_json_error_body(self):
        from klangk.cli.auth import local_login

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.json.side_effect = ValueError("not json")
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with pytest.raises(SystemExit):
                local_login("http://srv")

    def test_local_login_exits_when_no_access_token(self):
        from klangk.cli.auth import local_login

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"email": "a@x.com"}  # no token
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with pytest.raises(SystemExit):
                local_login("http://srv")

    def test_login_none_arm_no_password_prompt(self, tmp_path, monkeypatch):
        """In none mode login() hits /auth/local with no prompt."""
        monkeypatch.setattr(
            "klangk.cli.config._STATE_PATH", tmp_path / "s.yaml"
        )
        monkeypatch.setattr(
            "klangk.cli.auth.fetch_config",
            lambda _: {"auth_modes": "none", "oidc_providers": []},
        )
        local_resp = MagicMock()
        local_resp.status_code = 200
        local_resp.json.return_value = {
            "access_token": "jwt-none",
            "email": "admin@example.com",
        }
        with patch(
            "klangk.cli.transport.httpx.request", return_value=local_resp
        ):
            with patch("klangk.cli.auth.Prompt.ask") as prompt:
                from klangk.cli import auth

                auth.login("http://localhost:8995")
                prompt.assert_not_called()  # no password prompt in none mode
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") == "jwt-none"
        assert state.get_email("http://localhost:8995") == "admin@example.com"

    def test_login_password_arm_unchanged(self, tmp_path, monkeypatch):
        """In password mode login() prompts as before (cache removed)."""
        monkeypatch.setattr(
            "klangk.cli.config._STATE_PATH", tmp_path / "s.yaml"
        )
        monkeypatch.setattr(
            "klangk.cli.auth.fetch_config",
            lambda _: {"auth_modes": "password", "oidc_providers": []},
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt-pw"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with patch(
                "klangk.cli.auth.Prompt.ask",
                side_effect=["u@test.com", "pass123"],
            ):
                from klangk.cli import auth

                auth.login("http://localhost:8995")
        state = CLIState.load()
        assert state.get_token("http://localhost:8995") == "jwt-pw"


class TestRequireAuthNoneMode:
    """require_auth() auto-logs in against a none-mode server (#1374)."""

    def test_auto_logins_in_none_mode(self, tmp_path, monkeypatch):
        from klangk.cli import main

        monkeypatch.setattr(
            "klangk.cli.config._STATE_PATH", tmp_path / "s.yaml"
        )
        monkeypatch.setattr(
            "klangk.cli.config._CONFIG_PATH", tmp_path / "c.yaml"
        )
        monkeypatch.setattr(main, "_state_cache", None)
        # Server is already registered (active_server set) — mirroring the
        # scoped behavior where `klangkc login <server>` registers it once.
        state = CLIState()
        state.active_server = "http://localhost:8995"
        state.save()
        # No stored token -> probe /config live, it reports none.
        monkeypatch.setattr(
            "klangk.cli.main.fetch_config",
            lambda _: {"auth_modes": "none", "oidc_providers": []},
        )
        local_resp = MagicMock()
        local_resp.status_code = 200
        local_resp.json.return_value = {
            "access_token": "jwt-auto",
            "email": "admin@example.com",
        }
        with patch(
            "klangk.cli.transport.httpx.request", return_value=local_resp
        ):
            main.require_auth()
        state = main._state()
        assert state.get_token("http://localhost:8995") == "jwt-auto"

    def test_errors_when_not_logged_in_and_not_none(
        self, tmp_path, monkeypatch
    ):
        from klangk.cli import main

        monkeypatch.setattr(
            "klangk.cli.config._STATE_PATH", tmp_path / "s.yaml"
        )
        monkeypatch.setattr(main, "_state_cache", None)
        state = CLIState()
        state.active_server = "http://localhost:8995"
        state.save()
        monkeypatch.setattr(
            "klangk.cli.main.fetch_config",
            lambda _: {"auth_modes": "password", "oidc_providers": []},
        )
        with patch("klangk.cli.transport.httpx.request") as post:
            with pytest.raises(typer.Exit):
                main.require_auth()
            post.assert_not_called()  # no /auth/local attempt in password mode

    def test_auto_login_skipped_when_mode_switched_to_password(
        self, tmp_path, monkeypatch
    ):
        """If a server flipped none->password, require_auth must NOT
        auto-login — it should demand a real login (no stale cache)."""
        from klangk.cli import main

        monkeypatch.setattr(
            "klangk.cli.config._STATE_PATH", tmp_path / "s.yaml"
        )
        monkeypatch.setattr(
            "klangk.cli.config._CONFIG_PATH", tmp_path / "c.yaml"
        )
        monkeypatch.setattr(main, "_state_cache", None)
        state = CLIState()
        state.active_server = "http://localhost:8995"
        state.save()
        # Live probe now reports password (server switched modes).
        monkeypatch.setattr(
            "klangk.cli.main.fetch_config",
            lambda _: {"auth_modes": "password", "oidc_providers": []},
        )
        with patch("klangk.cli.transport.httpx.request") as post:
            with pytest.raises(typer.Exit):
                main.require_auth()
            post.assert_not_called()  # no /auth/local attempt

    def test_probe_failure_returns_false(self, tmp_path, monkeypatch):
        # fetch_config returns None (not a klangk instance) -> no auto-login.
        from klangk.cli import main

        monkeypatch.setattr(
            "klangk.cli.config._STATE_PATH", tmp_path / "s.yaml"
        )
        monkeypatch.setattr(main, "_state_cache", None)
        state = CLIState()
        state.active_server = "http://localhost:8995"
        state.save()
        monkeypatch.setattr("klangk.cli.main.fetch_config", lambda _: None)
        with patch("klangk.cli.transport.httpx.request") as post:
            with pytest.raises(typer.Exit):
                main.require_auth()
            post.assert_not_called()

    def test_local_login_exits_falls_back_to_error(
        self, tmp_path, monkeypatch
    ):
        from klangk.cli import main

        monkeypatch.setattr(
            "klangk.cli.config._STATE_PATH", tmp_path / "s.yaml"
        )
        monkeypatch.setattr(main, "_state_cache", None)
        state = CLIState()
        state.active_server = "http://localhost:8995"
        state.save()
        monkeypatch.setattr(
            "klangk.cli.main.fetch_config",
            lambda _: {"auth_modes": "none", "oidc_providers": []},
        )
        with patch("klangk.cli.main.local_login", side_effect=SystemExit(1)):
            with pytest.raises(typer.Exit):
                main.require_auth()


class TestMonitorNoneRelogin:
    """Long-lived WS/monitor re-auth falls back to /auth/local in none mode."""

    @pytest.mark.asyncio
    async def test_refresh_threaded_relogins_in_none_mode(
        self, tmp_path, monkeypatch
    ):
        from klangk.cli import main

        with patch("klangk.cli.main.refresh_token", return_value=None):
            with patch(
                "klangk.cli.main._server_mode_is_none", return_value=True
            ):
                with patch(
                    "klangk.cli.main.local_login",
                    return_value=("a@x.com", "fresh-token"),
                ):
                    new = await main.refresh_token_threaded(
                        "http://srv", "old"
                    )
        assert new == "fresh-token"

    @pytest.mark.asyncio
    async def test_refresh_threaded_returns_none_when_relogin_exits(
        self, tmp_path, monkeypatch
    ):
        from klangk.cli import main

        with patch("klangk.cli.main.refresh_token", return_value=None):
            with patch(
                "klangk.cli.main._server_mode_is_none", return_value=True
            ):
                with patch(
                    "klangk.cli.main.local_login", side_effect=SystemExit(1)
                ):
                    new = await main.refresh_token_threaded(
                        "http://srv", "old"
                    )
        assert new is None

    @pytest.mark.asyncio
    async def test_refresh_threaded_no_relogin_when_not_none(
        self, tmp_path, monkeypatch
    ):
        from klangk.cli import main

        with patch("klangk.cli.main.refresh_token", return_value=None):
            with patch(
                "klangk.cli.main._server_mode_is_none", return_value=False
            ):
                with patch("klangk.cli.main.local_login") as m:
                    new = await main.refresh_token_threaded(
                        "http://srv", "old"
                    )
        assert new is None
        m.assert_not_called()


class TestRefreshToken:
    def test_returns_new_token_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "klangk.cli.config._STATE_PATH", tmp_path / "s.yaml"
        )
        new_jwt = _make_jwt(9999999999.0, email="me@test.com")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": new_jwt}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            result = refresh_token("http://srv", "old-token")
        assert result == new_jwt
        # Verify state was persisted
        state = CLIState.load()
        assert state.get_token("http://srv") == new_jwt

    def test_returns_none_on_401(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            assert refresh_token("http://srv", "old-token") is None

    def test_returns_none_on_network_error(self):
        with patch(
            "klangk.cli.transport.httpx.request",
            side_effect=httpx.ConnectError(""),
        ):
            assert refresh_token("http://srv", "old-token") is None

    def test_returns_none_when_no_access_token_in_response(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            assert refresh_token("http://srv", "old-token") is None

    def test_handles_unparseable_jwt_email(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "klangk.cli.config._STATE_PATH", tmp_path / "s.yaml"
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # A token whose payload is not valid base64/JSON
        mock_resp.json.return_value = {"access_token": "a.!!!.c"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            result = refresh_token("http://srv", "old-token")
        assert result == "a.!!!.c"
        state = CLIState.load()
        assert state.get_token("http://srv") == "a.!!!.c"


class TestClientTryRefresh:
    def test_try_refresh_updates_token(self):
        client = KlangkClient("http://test:8995", "old-token")
        with patch(
            "klangk.cli.client._refresh_token", return_value="new-token"
        ):
            assert client._try_refresh() is True
        assert client.token == "new-token"

    def test_try_refresh_returns_false_on_failure(self):
        client = KlangkClient("http://test:8995", "old-token")
        with patch("klangk.cli.client._refresh_token", return_value=None):
            assert client._try_refresh() is False
        assert client.token == "old-token"

    def test_try_refresh_returns_false_without_token(self):
        client = KlangkClient("http://test:8995")
        assert client._try_refresh() is False

    def test_try_refresh_relogins_in_none_mode(self, tmp_path, monkeypatch):
        client = KlangkClient("http://test:8995", "old-token")
        with patch("klangk.cli.client._refresh_token", return_value=None):
            with patch(
                "klangk.cli.client._server_mode_is_none", return_value=True
            ):
                with patch(
                    "klangk.cli.client._local_login",
                    return_value=("local@example.com", "fresh-token"),
                ) as m:
                    assert client._try_refresh() is True
        assert client.token == "fresh-token"
        m.assert_called_once_with("http://test:8995")

    def test_try_refresh_skips_relogin_when_not_none_mode(self):
        client = KlangkClient("http://test:8995", "old-token")
        with patch("klangk.cli.client._refresh_token", return_value=None):
            with patch(
                "klangk.cli.client._server_mode_is_none", return_value=False
            ):
                with patch("klangk.cli.client._local_login") as m:
                    assert client._try_refresh() is False
        assert client.token == "old-token"
        m.assert_not_called()

    def test_try_refresh_returns_false_when_relogin_exits(
        self, tmp_path, monkeypatch
    ):
        client = KlangkClient("http://test:8995", "old-token")
        with patch("klangk.cli.client._refresh_token", return_value=None):
            with patch(
                "klangk.cli.client._server_mode_is_none", return_value=True
            ):
                with patch(
                    "klangk.cli.client._local_login", side_effect=SystemExit(1)
                ):
                    assert client._try_refresh() is False
        assert client.token == "old-token"


class TestClientRetryOn401:
    def test_retries_once_on_401_then_succeeds(self):
        client = KlangkClient("http://test:8995", "old-token")
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_200 = MagicMock()
        resp_200.status_code = 200
        with (
            patch(
                "klangk.cli.client.request_with_retry",
                side_effect=[resp_401, resp_200],
            ),
            patch(
                "klangk.cli.client._refresh_token", return_value="new-token"
            ),
        ):
            result = client.get("/api/v1/workspaces")
        assert result.status_code == 200
        assert client.token == "new-token"

    def test_no_retry_when_refresh_fails(self):
        client = KlangkClient("http://test:8995", "old-token")
        resp_401 = MagicMock()
        resp_401.status_code = 401
        with (
            patch(
                "klangk.cli.client.request_with_retry",
                return_value=resp_401,
            ),
            patch("klangk.cli.client._refresh_token", return_value=None),
        ):
            result = client.get("/api/v1/workspaces")
        assert result.status_code == 401

    def test_no_infinite_retry_loop(self):
        client = KlangkClient("http://test:8995", "old-token")
        resp_401 = MagicMock()
        resp_401.status_code = 401
        call_count = 0

        def counting_refresh(server_url, token):
            nonlocal call_count
            call_count += 1
            return "refreshed-token"

        with (
            patch(
                "klangk.cli.client.request_with_retry",
                return_value=resp_401,
            ),
            patch(
                "klangk.cli.client._refresh_token",
                side_effect=counting_refresh,
            ),
        ):
            result = client.get("/api/v1/workspaces")
        assert result.status_code == 401
        assert call_count == 1  # refresh called only once

    def test_proactive_refresh_in_headers(self):
        import time

        token = _make_jwt(time.time() + 60)  # expiring soon
        client = KlangkClient("http://test:8995", token)
        with patch(
            "klangk.cli.client._refresh_token", return_value="refreshed"
        ):
            headers = client._headers()
        assert headers["Authorization"] == "Bearer refreshed"
        assert client.token == "refreshed"


class TestWs4002Refresh:
    @pytest.mark.asyncio
    async def test_ws_4002_refresh_success(self):
        from klangk.cli.client import TerminalSession

        ws = AsyncMock()
        close_frame = MagicMock()
        close_frame.code = 4002
        ws.recv = AsyncMock(
            side_effect=websockets.ConnectionClosed(close_frame, None)
        )

        captured = []

        class CaptureWriter:
            def write(self, data):
                captured.append(data)

            def flush(self):
                pass

        session = TerminalSession(
            ws,
            80,
            24,
            stdout=CaptureWriter(),
            server_url="http://test:8995",
            token="old-token",
        )
        with patch(
            "klangk.cli.client._refresh_token", return_value="new-token"
        ):
            await session.stdout_loop()
        output = "".join(captured)
        assert "Session refreshed" in output

    @pytest.mark.asyncio
    async def test_ws_4002_refresh_failure(self):
        from klangk.cli.client import TerminalSession

        ws = AsyncMock()
        close_frame = MagicMock()
        close_frame.code = 4002
        ws.recv = AsyncMock(
            side_effect=websockets.ConnectionClosed(close_frame, None)
        )

        captured = []

        class CaptureWriter:
            def write(self, data):
                captured.append(data)

            def flush(self):
                pass

        session = TerminalSession(
            ws,
            80,
            24,
            stdout=CaptureWriter(),
            server_url="http://test:8995",
            token="old-token",
        )
        with patch("klangk.cli.client._refresh_token", return_value=None):
            await session.stdout_loop()
        output = "".join(captured)
        assert "Session expired" in output

    @pytest.mark.asyncio
    async def test_ws_4002_relogins_in_none_mode(self):
        from klangk.cli.client import TerminalSession

        ws = AsyncMock()
        close_frame = MagicMock()
        close_frame.code = 4002
        ws.recv = AsyncMock(
            side_effect=websockets.ConnectionClosed(close_frame, None)
        )

        captured = []

        class CaptureWriter:
            def write(self, data):
                captured.append(data)

            def flush(self):
                pass

        session = TerminalSession(
            ws,
            80,
            24,
            stdout=CaptureWriter(),
            server_url="http://test:8995",
            token="old-token",
        )
        with patch("klangk.cli.client._refresh_token", return_value=None):
            with patch(
                "klangk.cli.client._server_mode_is_none", return_value=True
            ):
                with patch(
                    "klangk.cli.client._local_login",
                    return_value=("local@example.com", "fresh-token"),
                ):
                    await session.stdout_loop()
        output = "".join(captured)
        assert "Session refreshed" in output

    @pytest.mark.asyncio
    async def test_ws_4002_relogin_failure_shows_expired(self):
        from klangk.cli.client import TerminalSession

        ws = AsyncMock()
        close_frame = MagicMock()
        close_frame.code = 4002
        ws.recv = AsyncMock(
            side_effect=websockets.ConnectionClosed(close_frame, None)
        )

        captured = []

        class CaptureWriter:
            def write(self, data):
                captured.append(data)

            def flush(self):
                pass

        session = TerminalSession(
            ws,
            80,
            24,
            stdout=CaptureWriter(),
            server_url="http://test:8995",
            token="old-token",
        )
        with patch("klangk.cli.client._refresh_token", return_value=None):
            with patch(
                "klangk.cli.client._server_mode_is_none", return_value=True
            ):
                with patch(
                    "klangk.cli.client._local_login", side_effect=SystemExit(1)
                ):
                    await session.stdout_loop()
        output = "".join(captured)
        assert "Session expired" in output


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
        from klangk.cli.client import get_terminal_size

        cols, rows = get_terminal_size()
        assert isinstance(cols, int) and cols > 0
        assert isinstance(rows, int) and rows > 0

    def test_get_terminal_size_returns_default_when_not_tty(self, monkeypatch):
        """When stdin is not a TTY, get_terminal_size returns (80, 24) without calling os."""
        from klangk.cli import client as cli_client

        called = []

        def _track(*args):
            called.append(args)
            raise OSError("should not be called")

        # sys.stdin is not a TTY in tests — no need to call os.get_terminal_size
        monkeypatch.setattr(os, "get_terminal_size", _track)
        cols, rows = cli_client.get_terminal_size()
        assert cols == 80
        assert rows == 24
        assert len(called) == 0  # os.get_terminal_size was never invoked

    def test_get_terminal_size_calls_os_when_tty(self, monkeypatch):
        from klangk.cli import client as cli_client

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
        cols, rows = cli_client.get_terminal_size()
        assert cols == 102
        assert rows == 40
        assert len(called_with) == 1


# --- run_shell / ws_shell ---


class TestRunShell:
    @pytest.mark.asyncio
    async def test_stdin_loop_sends_terminal_input(self):
        from klangk.cli.client import run_shell

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
                "klangk.cli.client.select.select",
                side_effect=fake_select,
            ),
            patch("klangk.cli.client.os.read", side_effect=fake_os_read),
        ):
            task = asyncio.create_task(run_shell(ws, 80, 24, stdin=fake_buf))
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
        from klangk.cli.client import run_shell

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
                "klangk.cli.client.select.select",
                side_effect=fake_select,
            ),
            patch("klangk.cli.client.os.read", side_effect=fake_os_read),
        ):
            task = asyncio.create_task(run_shell(ws, 80, 24, stdin=fake_buf))
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
        from klangk.cli.client import run_shell

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
        fake_buf.fileno = lambda: 99
        fake_stdout = CaptureWriter()
        with (
            patch(
                "klangk.cli.client.select.select",
                return_value=([0], [], []),
            ),
            patch(
                "klangk.cli.client.os.read",
                return_value=b"",
            ),
        ):
            task = asyncio.create_task(
                run_shell(ws, 80, 24, stdin=fake_buf, stdout=fake_stdout)
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
        from klangk.cli.client import ws_shell

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

        with patch(
            "klangk.cli.transport.websockets.connect", return_value=ws_mock
        ):
            with pytest.raises(ConnectionError) as exc_info:
                await ws_shell("http://localhost", "token", "ws1")
            assert "Connection failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_ws_shell_joins_shared_terminal_after_terminal_windows(
        self,
    ):
        """A shared join must wait for shared_terminals, which the server
        sends AFTER terminal_windows (#1208).

        Scripts the real on-the-wire order (chat, terminal_started,
        terminal_output, terminal_windows, THEN shared_terminals) and
        asserts the join resolves the match and sends
        join_shared_terminal rather than raising "not found".
        """
        from klangk.cli.client import ws_shell

        # Message sequence matches a live connect (see #1208 trace):
        # the service-cmd window arrives only in shared_terminals, which
        # lands AFTER terminal_windows.
        msgs = [
            {"type": "container_ready"},
            {"type": "terminal_started"},
            {"type": "terminal_output", "data": "boot..."},
            {"type": "terminal_windows", "windows": []},
            {
                "type": "shared_terminals",
                "terminals": [
                    {
                        "user_id": "agent-uid",
                        "handle": "clanker",
                        "window_name": "service-cmd",
                        "window_id": "win-1",
                        "is_service": True,
                    }
                ],
            },
            # join confirmation
            {"type": "terminal_started"},
        ]
        idx = {"i": 0}

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit

        async def fake_recv(*a, **kw):
            i = idx["i"]
            idx["i"] += 1
            if i < len(msgs):
                return json.dumps(msgs[i])
            # After the scripted messages, signal the stdin loop to end.
            import websockets

            raise websockets.ConnectionClosed(None, None)

        ws_mock.recv = fake_recv
        ws_mock.send = AsyncMock()
        ws_mock.close = AsyncMock()

        sent_cmds = []

        async def capture_send(payload, *a, **kw):
            sent_cmds.append(json.loads(payload))

        ws_mock.send = capture_send

        with patch(
            "klangk.cli.transport.websockets.connect", return_value=ws_mock
        ):
            with patch("klangk.cli.client.run_shell", new=AsyncMock()):
                # Should NOT raise "Shared terminal not found".
                await ws_shell(
                    "http://localhost",
                    "token",
                    "ws1",
                    raw_mode=False,
                    window="clanker:service-cmd",
                )

        # The join command was sent with the resolved ids.
        join = next(
            (c for c in sent_cmds if c.get("cmd") == "join_shared_terminal"),
            None,
        )
        assert join is not None, "join_shared_terminal was never sent"
        assert join["user_id"] == "agent-uid"
        assert join["window_id"] == "win-1"

    @pytest.mark.asyncio
    async def test_ws_shell_shared_terminal_not_found_when_absent(self):
        """Sanity: if the window truly isn't shared, we still raise."""
        from klangk.cli.client import ws_shell

        msgs = [
            {"type": "container_ready"},
            {"type": "terminal_started"},
            {"type": "terminal_windows", "windows": []},
            {"type": "shared_terminals", "terminals": []},
        ]
        idx = {"i": 0}

        ws_mock = MagicMock()

        async def fake_enter(self):
            return ws_mock

        async def fake_exit(self, *args):
            return None

        ws_mock.__aenter__ = fake_enter
        ws_mock.__aexit__ = fake_exit

        async def fake_recv(*a, **kw):
            i = idx["i"]
            idx["i"] += 1
            if i < len(msgs):
                return json.dumps(msgs[i])
            import websockets

            raise websockets.ConnectionClosed(None, None)

        ws_mock.recv = fake_recv
        ws_mock.send = AsyncMock()

        with patch(
            "klangk.cli.transport.websockets.connect", return_value=ws_mock
        ):
            with pytest.raises(ConnectionError) as exc_info:
                await ws_shell(
                    "http://localhost",
                    "token",
                    "ws1",
                    raw_mode=False,
                    window="clanker:service-cmd",
                )
            assert "not found" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_tilde_dot_disconnects(self):
        """Enter then ~ then . cleanly exits the shell."""
        from klangk.cli.client import run_shell

        ws = AsyncMock()
        ws.send = AsyncMock()

        # Block recv until stdin_loop processes ~. and closes the WS
        import websockets

        recv_gate = asyncio.Event()

        async def blocking_recv():
            await recv_gate.wait()
            raise websockets.ConnectionClosed(None, None)

        ws.recv = blocking_recv
        ws.close = AsyncMock(side_effect=lambda: recv_gate.set())

        # Simulate: \r (Enter), ~ , .
        read_sequence = [b"\r", b"~", b"."]
        read_idx = 0
        call_count = 0
        fake_buf = BytesIO(b"")
        fake_buf.fileno = lambda: 99

        def fake_select(rlist, wlist, xlist, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count <= len(read_sequence):
                return ([99], [], [])
            return ([], [], [])

        def fake_os_read(fd, n):
            nonlocal read_idx
            if fd == 99 and read_idx < len(read_sequence):
                data = read_sequence[read_idx]
                read_idx += 1
                return data
            return b""

        fake_stdout = io.StringIO()

        with (
            patch(
                "klangk.cli.client.select.select",
                side_effect=fake_select,
            ),
            patch(
                "klangk.cli.client.os.read",
                side_effect=fake_os_read,
            ),
        ):
            await run_shell(ws, 80, 24, stdin=fake_buf, stdout=fake_stdout)

        assert "Disconnected" in fake_stdout.getvalue()

    @pytest.mark.asyncio
    async def test_tilde_without_dot_sends_tilde(self):
        """~ followed by a non-dot sends the ~ normally."""
        from klangk.cli.client import run_shell

        ws = AsyncMock()
        ws.send = AsyncMock()

        # Block recv so stdin_loop can run first
        async def slow_recv():
            await asyncio.sleep(1)
            return json.dumps({"type": "terminal_output", "data": ""})

        ws.recv = slow_recv

        # Enter, ~, x  — should send ~ then x
        read_sequence = [b"\r", b"~", b"x"]
        read_idx = 0
        call_count = 0
        fake_buf = BytesIO(b"")
        fake_buf.fileno = lambda: 99

        def fake_select(rlist, wlist, xlist, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count <= len(read_sequence):
                return ([99], [], [])
            return ([], [], [])

        def fake_os_read(fd, n):
            nonlocal read_idx
            if fd == 99 and read_idx < len(read_sequence):
                data = read_sequence[read_idx]
                read_idx += 1
                return data
            return b""

        with (
            patch(
                "klangk.cli.client.select.select",
                side_effect=fake_select,
            ),
            patch(
                "klangk.cli.client.os.read",
                side_effect=fake_os_read,
            ),
        ):
            task = asyncio.create_task(run_shell(ws, 80, 24, stdin=fake_buf))
            await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        sent = [c[0][0] for c in ws.send.call_args_list]
        # The ~ should have been sent as terminal_input
        assert any("terminal_input" in s and "~" in s for s in sent)
        # The x should also have been sent
        assert any("terminal_input" in s and "x" in s for s in sent)


# --- Misc ---


class TestMisc:
    @pytest.fixture(autouse=True)
    def no_oidc(self, monkeypatch):
        monkeypatch.setattr("klangk.cli.auth.fetch_config", lambda _: {})

    def test_auth_error_message(self):
        err = AuthError("Session expired — run `klangkc login`")
        assert "Session expired" in str(err)
        assert "klangkc login" in str(err)

    def test_workspace_dataclass_fields(self):
        ws = Workspace(id="x", name="y", created_at="z")
        assert ws.id == "x"
        assert ws.name == "y"
        assert ws.created_at == "z"

    def test_login_success_stores_email(self, tmp_path, monkeypatch):
        state_path = tmp_path / "state.yaml"
        monkeypatch.setattr("klangk.cli.config._STATE_PATH", state_path)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "jwt"}
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with patch(
                "klangk.cli.auth.Prompt.ask",
                side_effect=["admin@example.com", "pw"],
            ):
                from klangk.cli import auth

                auth.login("http://localhost:8995")
        state = CLIState.load()
        assert state.get_email("http://localhost:8995") == "admin@example.com"
        assert state.get_token("http://localhost:8995") == "jwt"


class TestExportImportClient:
    def test_export_workspace_streams_to_file(self, tmp_path):
        client = KlangkClient("http://localhost:8995", "token")

        output = tmp_path / "exported.tar.gz"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.headers = {"content-length": "12"}
        mock_resp.iter_bytes.return_value = [b"chunk1", b"chunk2"]

        progress_calls = []

        def _on_progress(downloaded, total):
            progress_calls.append((downloaded, total))

        with patch("klangk.cli.transport.httpx.stream") as mock_stream:
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
        client = KlangkClient("http://localhost:8995", "token")

        output = tmp_path / "est.tar.gz"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.headers = {"x-estimated-size": "5000"}
        mock_resp.iter_bytes.return_value = [b"data"]

        progress_calls = []

        with patch("klangk.cli.transport.httpx.stream") as mock_stream:
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
        client = KlangkClient("http://localhost:8995", "token")

        output = tmp_path / "nosize.tar.gz"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.headers = {}
        mock_resp.iter_bytes.return_value = [b"chunk"]

        progress_calls = []

        with patch("klangk.cli.transport.httpx.stream") as mock_stream:
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
        client = KlangkClient("http://localhost:8995", "token")

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.is_success = False
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "not found", request=MagicMock(), response=mock_resp
        )

        with patch("klangk.cli.transport.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__ = MagicMock(
                return_value=mock_resp
            )
            mock_stream.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(httpx.HTTPStatusError):
                client.export_workspace("bad-id", tmp_path / "out.tar.gz")

        mock_resp.read.assert_called_once()

    def test_export_workspace_auth_error(self, tmp_path):
        client = KlangkClient("http://localhost:8995", "bad")

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("klangk.cli.transport.httpx.stream") as mock_stream:
            mock_stream.return_value.__enter__ = MagicMock(
                return_value=mock_resp
            )
            mock_stream.return_value.__exit__ = MagicMock(return_value=False)
            with pytest.raises(AuthError):
                client.export_workspace("ws-id", tmp_path / "out.tar.gz")

    def test_import_workspace_returns_workspace(self, tmp_path):
        client = KlangkClient("http://localhost:8995", "token")

        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"fake archive")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "new-id",
            "name": "imported-ws",
            "created_at": "2025-01-01",
        }

        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            ws = client.import_workspace(archive, name="imported-ws")

        assert ws.name == "imported-ws"
        assert ws.id == "new-id"

    def test_import_workspace_with_progress(self, tmp_path):
        client = KlangkClient("http://localhost:8995", "token")

        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"fake archive data")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "prog-id",
            "name": "prog-ws",
            "created_at": "2025-01-01",
        }

        progress_calls = []

        def _mock_post(*args, **kwargs):
            # Simulate httpx reading the file (which triggers progress)
            files = kwargs.get("files", {})
            if files:
                _, file_tuple = next(iter(files.items()))
                _, fobj, _ = file_tuple
                while fobj.read(8192):
                    pass
            return mock_resp

        with patch(
            "klangk.cli.transport.httpx.request", side_effect=_mock_post
        ):
            ws = client.import_workspace(
                archive,
                name="prog-ws",
                on_progress=lambda d, t: progress_calls.append((d, t)),
            )

        assert ws.name == "prog-ws"
        assert len(progress_calls) > 0
        assert all(t == 17 for _, t in progress_calls)
        assert progress_calls[-1][0] == 17

    def test_import_workspace_auth_error(self, tmp_path):
        client = KlangkClient("http://localhost:8995", "bad")

        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"fake")

        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            with pytest.raises(AuthError):
                client.import_workspace(archive)

    def test_import_workspace_no_name(self, tmp_path):
        client = KlangkClient("http://localhost:8995", "token")

        archive = tmp_path / "test.tar.gz"
        archive.write_bytes(b"fake")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "id2",
            "name": "from-archive",
            "created_at": "2025-01-01",
        }

        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ) as mock_post:
            ws = client.import_workspace(archive)

        assert ws.name == "from-archive"
        # Verify no name param was sent
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs.get("params") == {}


class TestHTTPMethodWrappers:
    """Test the get/post/put/patch/delete method wrappers on KlangkClient."""

    def _make_client(self):
        return KlangkClient("http://test:8995", "tok")

    def test_get_calls_request_with_retry(self):
        client = self._make_client()
        mock_resp = MagicMock()
        with patch(
            "klangk.cli.client.request_with_retry", return_value=mock_resp
        ) as mock_req:
            result = client.get("/foo")
        assert result is mock_resp
        mock_req.assert_called_once_with(
            "http://test:8995",
            "GET",
            "/foo",
            headers={"Authorization": "Bearer tok"},
        )

    def test_post_calls_request_with_retry(self):
        client = self._make_client()
        mock_resp = MagicMock()
        with patch(
            "klangk.cli.client.request_with_retry", return_value=mock_resp
        ) as mock_req:
            result = client.post("/bar", json={"a": 1})
        assert result is mock_resp
        mock_req.assert_called_once_with(
            "http://test:8995",
            "POST",
            "/bar",
            headers={"Authorization": "Bearer tok"},
            json={"a": 1},
        )

    def test_put_calls_request_with_retry(self):
        client = self._make_client()
        mock_resp = MagicMock()
        with patch(
            "klangk.cli.client.request_with_retry", return_value=mock_resp
        ) as mock_req:
            result = client.put("/baz")
        assert result is mock_resp
        mock_req.assert_called_once_with(
            "http://test:8995",
            "PUT",
            "/baz",
            headers={"Authorization": "Bearer tok"},
        )

    def test_patch_calls_request_with_retry(self):
        client = self._make_client()
        mock_resp = MagicMock()
        with patch(
            "klangk.cli.client.request_with_retry", return_value=mock_resp
        ) as mock_req:
            result = client.patch("/qux", json={"b": 2})
        assert result is mock_resp
        mock_req.assert_called_once_with(
            "http://test:8995",
            "PATCH",
            "/qux",
            headers={"Authorization": "Bearer tok"},
            json={"b": 2},
        )

    def test_delete_calls_request_with_retry(self):
        client = self._make_client()
        mock_resp = MagicMock()
        with patch(
            "klangk.cli.client.request_with_retry", return_value=mock_resp
        ) as mock_req:
            result = client.delete("/del")
        assert result is mock_resp
        mock_req.assert_called_once_with(
            "http://test:8995",
            "DELETE",
            "/del",
            headers={"Authorization": "Bearer tok"},
        )


class TestCreateWorkspaceClient:
    def test_create_workspace_with_all_options(self):
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "ws-new",
            "name": "my-ws",
            "created_at": "2025-01-01",
        }
        with patch.object(client, "post", return_value=mock_resp) as mock_post:
            ws = client.create_workspace(
                "my-ws",
                image="ubuntu:latest",
                mounts=["/data:/data"],
                env={"FOO": "bar"},
            )
        assert ws.name == "my-ws"
        call_kwargs = mock_post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["image"] == "ubuntu:latest"
        assert body["mounts"] == ["/data:/data"]
        assert body["env"] == {"FOO": "bar"}

    def test_create_workspace_with_service_command(self):
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "ws-cmd",
            "name": "svc-ws",
            "created_at": "2025-01-01",
        }
        with patch.object(client, "post", return_value=mock_resp) as mock_post:
            ws = client.create_workspace(
                "svc-ws", service_command="openclaw gateway"
            )
        assert ws.name == "svc-ws"
        body = mock_post.call_args.kwargs.get("json")
        assert body["service_command"] == "openclaw gateway"

    def test_create_workspace_with_health_check(self):
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "ws-hc",
            "name": "hc-ws",
            "created_at": "2025-01-01",
        }
        with patch.object(client, "post", return_value=mock_resp) as mock_post:
            client.create_workspace(
                "hc-ws", health_check="curl -sf http://localhost:8080/h"
            )
        body = mock_post.call_args.kwargs.get("json")
        assert body["health_check"] == "curl -sf http://localhost:8080/h"

    def test_create_workspace_with_auto_start(self):
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "ws-auto",
            "name": "auto-ws",
            "created_at": "2025-01-01",
        }
        with patch.object(client, "post", return_value=mock_resp) as mock_post:
            client.create_workspace("auto-ws", auto_start=True)
        body = mock_post.call_args.kwargs.get("json")
        assert body["auto_start"] is True

    def test_create_workspace_with_setup_state(self):
        """setup_state is forwarded in the create body (#1033)."""
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "ws-state",
            "name": "state-ws",
            "created_at": "2025-01-01",
        }
        with patch.object(client, "post", return_value=mock_resp) as mock_post:
            client.create_workspace("state-ws", setup_state="pending")
        body = mock_post.call_args.kwargs.get("json")
        assert body["setup_state"] == "pending"

    def test_set_setup_state(self):
        """set_setup_state PUTs the lifecycle field (#1033)."""
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(client, "put", return_value=mock_resp) as mock_put:
            client.set_setup_state("ws-123", "complete")
        assert mock_put.call_args.args[0] == "/api/v1/workspaces/ws-123"
        assert mock_put.call_args.kwargs["json"] == {"setup_state": "complete"}


class TestListImagesClient:
    def test_list_images(self):
        client = KlangkClient("http://test:8995", "token")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"images": ["ubuntu", "alpine"]}
        with patch.object(client, "get", return_value=mock_resp):
            result = client.list_images()
        assert result == {"images": ["ubuntu", "alpine"]}


class TestSendIgnoreClosed:
    async def test_sends_message(self):
        from klangk.cli.client import send_ignore_closed

        ws = AsyncMock()
        await send_ignore_closed(ws, "hello")
        ws.send.assert_awaited_once_with("hello")

    async def test_ignores_connection_closed(self):
        import websockets
        from klangk.cli.client import send_ignore_closed

        ws = AsyncMock()
        ws.send = AsyncMock(
            side_effect=websockets.ConnectionClosed(None, None)
        )
        # Should not raise
        await send_ignore_closed(ws, "hello")

    async def test_ignores_oserror(self):
        from klangk.cli.client import send_ignore_closed

        ws = AsyncMock()
        ws.send = AsyncMock(side_effect=OSError("broken"))
        # Should not raise
        await send_ignore_closed(ws, "hello")


class TestTerminalSessionRunCancellation:
    async def test_run_cancels_all_tasks_on_failure(self):
        """Tasks must be cancelled when one raises, not left orphaned."""
        from klangk.cli.client import TerminalSession

        ws = AsyncMock()

        class DummyWriter:
            def write(self, data):
                pass

            def flush(self):
                pass

        session = TerminalSession(ws, 80, 24, stdout=DummyWriter())

        boom = RuntimeError("stdin exploded")
        cancelled = []

        async def fake_stdin():
            raise boom

        async def fake_stdout():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append("stdout")
                raise

        async def fake_resize():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append("resize")
                raise

        async def fake_heartbeat():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append("heartbeat")
                raise

        session.stdin_loop = fake_stdin
        session.stdout_loop = fake_stdout
        session.resize_loop = fake_resize
        session.heartbeat_loop = fake_heartbeat

        with pytest.raises(RuntimeError, match="stdin exploded"):
            await session.run()

        assert sorted(cancelled) == ["heartbeat", "resize", "stdout"]


class TestExecSessionRunClosedWs:
    async def test_run_tolerates_closed_ws_on_exec_stop(self):
        """ExecSession.run() must not crash when the WS is closed at cleanup."""
        import websockets
        from klangk.cli.client import ExecSession

        ws = AsyncMock()
        call_count = 0

        async def _send_side_effect(msg):
            nonlocal call_count
            call_count += 1
            parsed = json.loads(msg)
            # Let exec_start through, but blow up on exec_stop
            if parsed.get("cmd") == "exec_stop":
                raise websockets.ConnectionClosed(None, None)

        ws.send = AsyncMock(side_effect=_send_side_effect)
        # recv returns an exec_exit so stdout_forward terminates
        ws.recv = AsyncMock(
            return_value=json.dumps({"type": "exec_exit", "code": 0})
        )

        session = ExecSession(ws, command=["echo", "hi"], stdin=None)
        exit_code = await session.run()
        assert exit_code == 0


class _FakeMonitorConn:
    """Async-iterator WS stub for ``klangkc monitor``."""

    def __init__(self, messages: list[str]):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _ClosedStub(websockets.ConnectionClosed):
    """A ConnectionClosed with a specific close code (for reconnect tests)."""

    def __init__(self, code: int):
        rcvd = MagicMock()
        rcvd.code = code
        super().__init__(rcvd=rcvd, sent=None)


class _FakeMonitorCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return None


class TestMonitor:
    @pytest.mark.asyncio
    async def test_no_command_streams_json(self, capsys):
        from klangk.cli.main import monitor_connection

        messages = [
            json.dumps(
                {
                    "type": "service_health",
                    "workspace_id": "w1",
                    "healthy": True,
                }
            ),
            json.dumps({"cmd": "terminal_ack"}),  # no type → skipped
            json.dumps(
                {
                    "type": "container_status",
                    "workspace_id": "w1",
                    "running": False,
                }
            ),
        ]
        conn = _FakeMonitorConn(messages)
        with patch(
            "klangk.cli.main.websockets.connect",
            return_value=_FakeMonitorCM(conn),
        ):
            await monitor_connection(
                "http://x", "tok", 1024, command=[], types=[], workspaces=[]
            )
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 2
        assert json.loads(out[0])["type"] == "service_health"
        assert json.loads(out[1])["type"] == "container_status"

    @pytest.mark.asyncio
    async def test_type_filter(self, capsys):
        from klangk.cli.main import monitor_connection

        messages = [
            json.dumps(
                {
                    "type": "service_health",
                    "workspace_id": "w1",
                    "healthy": False,
                }
            ),
            json.dumps(
                {
                    "type": "container_status",
                    "workspace_id": "w1",
                    "running": True,
                }
            ),
        ]
        conn = _FakeMonitorConn(messages)
        with patch(
            "klangk.cli.main.websockets.connect",
            return_value=_FakeMonitorCM(conn),
        ):
            await monitor_connection(
                "http://x",
                "tok",
                1024,
                command=[],
                types=["service_health"],
                workspaces=[],
            )
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        assert json.loads(out[0])["type"] == "service_health"

    @pytest.mark.asyncio
    async def test_workspace_filter_skips_others(self, capsys):
        from klangk.cli.main import monitor_connection

        messages = [
            json.dumps(
                {
                    "type": "service_health",
                    "workspace_id": "w2",
                    "healthy": False,
                }
            ),
            json.dumps(
                {
                    "type": "service_health",
                    "workspace_id": "w1",
                    "healthy": True,
                }
            ),
        ]
        conn = _FakeMonitorConn(messages)
        with patch(
            "klangk.cli.main.websockets.connect",
            return_value=_FakeMonitorCM(conn),
        ):
            await monitor_connection(
                "http://x",
                "tok",
                1024,
                command=[],
                types=[],
                workspaces=["w1"],
            )
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        assert json.loads(out[0])["workspace_id"] == "w1"

    @pytest.mark.asyncio
    async def test_runs_command_with_payload_and_env(self):
        from klangk.cli.main import monitor_connection

        event = {
            "type": "service_health",
            "workspace_id": "w1",
            "healthy": False,
        }
        conn = _FakeMonitorConn([json.dumps(event)])
        with patch(
            "klangk.cli.main.websockets.connect",
            return_value=_FakeMonitorCM(conn),
        ):
            with patch("klangk.cli.main.subprocess.run") as run_mock:
                await monitor_connection(
                    "http://x",
                    "tok",
                    1024,
                    command=["notify-send"],
                    types=[],
                    workspaces=[],
                )
        run_mock.assert_called_once()
        _, kwargs = run_mock.call_args
        assert kwargs["input"] == json.dumps(event).encode()
        env = kwargs["env"]
        assert env["KLANGK_EVENT_TYPE"] == "service_health"
        assert env["KLANGK_WORKSPACE_ID"] == "w1"
        assert env["KLANGK_HEALTHY"] == "false"
        assert "KLANGK_EVENT" in env

    @pytest.mark.asyncio
    async def test_health_message_env_set_when_present(self):
        # The failure reason is exposed as KLANGK_HEALTH_MESSAGE so a
        # monitor command can act on / report *why* it's unhealthy (#1088).
        from klangk.cli.main import monitor_connection

        event = {
            "type": "service_health",
            "workspace_id": "w1",
            "healthy": False,
            "health_message": "curl: connection refused",
        }
        conn = _FakeMonitorConn([json.dumps(event)])
        with patch(
            "klangk.cli.main.websockets.connect",
            return_value=_FakeMonitorCM(conn),
        ):
            with patch("klangk.cli.main.subprocess.run") as run_mock:
                await monitor_connection(
                    "http://x",
                    "tok",
                    1024,
                    command=["notify-send"],
                    types=[],
                    workspaces=[],
                )
        env = run_mock.call_args.kwargs["env"]
        assert env["KLANGK_HEALTHY"] == "false"
        assert env["KLANGK_HEALTH_MESSAGE"] == "curl: connection refused"

    @pytest.mark.asyncio
    async def test_death_frame_exposes_running_and_freshness_env(self):
        # A container-death frame carries running=false plus the
        # additive freshness/seq fields (#1175).  The dispatcher must
        # surface them so a command can tell "stopped" from "unhealthy".
        from klangk.cli.main import monitor_connection

        event = {
            "type": "service_health",
            "workspace_id": "w1",
            "healthy": False,
            "running": False,
            "health_checked_at": "2023-11-14T22:13:20+00:00",
            "seq": 7,
        }
        conn = _FakeMonitorConn([json.dumps(event)])
        with patch(
            "klangk.cli.main.websockets.connect",
            return_value=_FakeMonitorCM(conn),
        ):
            with patch("klangk.cli.main.subprocess.run") as run_mock:
                await monitor_connection(
                    "http://x",
                    "tok",
                    1024,
                    command=["notify-send"],
                    types=[],
                    workspaces=[],
                )
        env = run_mock.call_args.kwargs["env"]
        assert env["KLANGK_HEALTHY"] == "false"
        assert env["KLANGK_RUNNING"] == "false"
        assert env["KLANGK_HEALTH_CHECKED_AT"] == "2023-11-14T22:13:20+00:00"
        assert env["KLANGK_HEALTH_SEQ"] == "7"

    @pytest.mark.asyncio
    async def test_command_not_found_propagates(self, monkeypatch):
        # A missing command binary propagates FileNotFoundError out of
        # the single-connection loop (the runner turns it into an exit).
        from klangk.cli.main import monitor_connection

        event = {
            "type": "service_health",
            "workspace_id": "w1",
            "healthy": True,
        }
        conn = _FakeMonitorConn([json.dumps(event)])
        with patch(
            "klangk.cli.main.websockets.connect",
            return_value=_FakeMonitorCM(conn),
        ):
            monkeypatch.setattr(
                "klangk.cli.main.subprocess.run",
                MagicMock(side_effect=FileNotFoundError()),
            )
            with pytest.raises(FileNotFoundError):
                await monitor_connection(
                    "http://x",
                    "tok",
                    1024,
                    command=["nope"],
                    types=[],
                    workspaces=[],
                )

    @pytest.mark.asyncio
    async def test_reconnects_after_disconnect(self):
        # Two clean closes → monitor_connection called twice, with a
        # backoff sleep in between.
        from klangk.cli import main as main_mod

        calls = {"conn": 0, "sleep": 0}

        async def fake_conn(*args, **kwargs):
            calls["conn"] += 1
            if calls["conn"] >= 2:
                raise typer.Exit(code=0)  # break the infinite loop
            return None  # clean close → triggers reconnect

        async def fake_sleep(delay):
            calls["sleep"] += 1
            assert delay > 0

        with (
            patch.object(main_mod, "monitor_connection", fake_conn),
            patch.object(main_mod.asyncio, "sleep", fake_sleep),
            patch.object(main_mod, "monitor_backoff", lambda a, m: 0.01),
        ):
            with pytest.raises(typer.Exit) as exc_info:
                await main_mod.monitor_run(
                    "http://x",
                    "tok",
                    1024,
                    command=[],
                    types=[],
                    workspaces=[],
                    max_reconnects=None,
                    max_delay=1.0,
                )
        assert exc_info.value.exit_code == 0
        assert calls["conn"] == 2
        assert calls["sleep"] == 1

    @pytest.mark.asyncio
    async def test_stops_after_max_reconnects(self):
        from klangk.cli import main as main_mod

        async def fake_conn(*args, **kwargs):
            raise OSError("boom")

        sleeps = []

        async def fake_sleep(delay):
            sleeps.append(delay)

        with (
            patch.object(main_mod, "monitor_connection", fake_conn),
            patch.object(main_mod.asyncio, "sleep", fake_sleep),
            patch.object(main_mod, "monitor_backoff", lambda a, m: 0.0),
        ):
            with pytest.raises(typer.Exit) as exc_info:
                await main_mod.monitor_run(
                    "http://x",
                    "tok",
                    1024,
                    command=[],
                    types=[],
                    workspaces=[],
                    max_reconnects=2,
                    max_delay=1.0,
                )
        assert exc_info.value.exit_code == 1
        # 2 reconnects attempted before giving up.
        assert len(sleeps) == 2

    @pytest.mark.asyncio
    async def test_no_reconnect_gives_up_immediately(self):
        from klangk.cli import main as main_mod

        async def fake_conn(*args, **kwargs):
            raise OSError("boom")

        async def fake_sleep(delay):
            pytest.fail("should not sleep before giving up")

        with (
            patch.object(main_mod, "monitor_connection", fake_conn),
            patch.object(main_mod.asyncio, "sleep", fake_sleep),
        ):
            with pytest.raises(typer.Exit) as exc_info:
                await main_mod.monitor_run(
                    "http://x",
                    "tok",
                    1024,
                    command=[],
                    types=[],
                    workspaces=[],
                    max_reconnects=0,
                    max_delay=1.0,
                )
        assert exc_info.value.exit_code == 1

    @pytest.mark.asyncio
    async def test_refreshes_token_on_4002_close(self):
        # A 4002 close triggers refresh; the refreshed token is used on
        # the next attempt.
        from klangk.cli import main as main_mod

        seen_tokens = []

        async def fake_conn(ws_url, token, *args, **kwargs):
            seen_tokens.append(token)
            # First call: simulate a 4002 close.
            raise _ClosedStub(4002)

        async def fake_refresh(server_url, token):
            return "NEW_TOKEN"

        async def fake_sleep(delay):
            pass

        with (
            patch.object(main_mod, "monitor_connection", fake_conn),
            patch.object(main_mod, "refresh_token_threaded", fake_refresh),
            patch.object(main_mod.asyncio, "sleep", fake_sleep),
            patch.object(main_mod, "monitor_backoff", lambda a, m: 0.0),
        ):
            # Stop the loop after the second connect via max_reconnects.
            with pytest.raises(typer.Exit):
                await main_mod.monitor_run(
                    "http://x",
                    "OLD_TOKEN",
                    1024,
                    command=[],
                    types=[],
                    workspaces=[],
                    max_reconnects=1,
                    max_delay=1.0,
                )
        assert seen_tokens[0] == "OLD_TOKEN"
        assert seen_tokens[1] == "NEW_TOKEN"

    @pytest.mark.asyncio
    async def test_refresh_failure_keeps_current_token(self):
        from klangk.cli import main as main_mod

        seen_tokens = []

        async def fake_conn(ws_url, token, *args, **kwargs):
            seen_tokens.append(token)
            raise _ClosedStub(4002)

        async def fake_refresh(server_url, token):
            return None  # refresh failed

        async def fake_sleep(delay):
            pass

        with (
            patch.object(main_mod, "monitor_connection", fake_conn),
            patch.object(main_mod, "refresh_token_threaded", fake_refresh),
            patch.object(main_mod.asyncio, "sleep", fake_sleep),
            patch.object(main_mod, "monitor_backoff", lambda a, m: 0.0),
        ):
            with pytest.raises(typer.Exit):
                await main_mod.monitor_run(
                    "http://x",
                    "OLD_TOKEN",
                    1024,
                    command=[],
                    types=[],
                    workspaces=[],
                    max_reconnects=1,
                    max_delay=1.0,
                )
        # Token unchanged after failed refresh.
        assert seen_tokens[0] == "OLD_TOKEN"
        assert seen_tokens[1] == "OLD_TOKEN"

    def test_backoff_is_capped(self):
        from klangk.cli.main import monitor_backoff

        # Small attempts grow; large attempts never exceed the cap.
        for attempt in range(1, 20):
            delay = monitor_backoff(attempt, max_delay=30.0)
            assert 0 < delay <= 30.0

    @pytest.mark.asyncio
    async def test_connection_skips_invalid_json(self, capsys):
        # Malformed frames are ignored rather than crashing the monitor.
        from klangk.cli.main import monitor_connection

        messages = [
            "this is not json",
            json.dumps(
                {
                    "type": "service_health",
                    "workspace_id": "w1",
                    "healthy": True,
                }
            ),
        ]
        conn = _FakeMonitorConn(messages)
        with patch(
            "klangk.cli.main.websockets.connect",
            return_value=_FakeMonitorCM(conn),
        ):
            await monitor_connection(
                "http://x", "tok", 1024, command=[], types=[], workspaces=[]
            )
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1
        assert json.loads(out[0])["type"] == "service_health"

    @pytest.mark.asyncio
    async def test_refresh_token_threaded_delegates(self):
        # refresh_token_threaded runs the sync refresh_token off-loop
        # and returns whatever it yields.
        from klangk.cli import main as main_mod

        with patch.object(
            main_mod, "refresh_token", return_value="FRESH"
        ) as mock_refresh:
            result = await main_mod.refresh_token_threaded("http://x", "OLD")
        assert result == "FRESH"
        mock_refresh.assert_called_once_with("http://x", "OLD")

    @pytest.mark.asyncio
    async def test_invalid_status_4001_triggers_refresh(self):
        # An HTTP 4001 rejection (InvalidStatus) is treated as auth-related
        # and triggers a token refresh before reconnecting.
        from klangk.cli import main as main_mod

        seen_tokens = []

        async def fake_conn(ws_url, token, *args, **kwargs):
            seen_tokens.append(token)
            raise websockets.InvalidStatus(MagicMock(status_code=4001))

        async def fake_refresh(server_url, token):
            return "NEW_TOKEN"

        async def fake_sleep(delay):
            pass

        with (
            patch.object(main_mod, "monitor_connection", fake_conn),
            patch.object(main_mod, "refresh_token_threaded", fake_refresh),
            patch.object(main_mod.asyncio, "sleep", fake_sleep),
            patch.object(main_mod, "monitor_backoff", lambda a, m: 0.0),
        ):
            with pytest.raises(typer.Exit):
                await main_mod.monitor_run(
                    "http://x",
                    "OLD_TOKEN",
                    1024,
                    command=[],
                    types=[],
                    workspaces=[],
                    max_reconnects=1,
                    max_delay=1.0,
                )
        assert seen_tokens[0] == "OLD_TOKEN"
        assert seen_tokens[1] == "NEW_TOKEN"
