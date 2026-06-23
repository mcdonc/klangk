"""Tests for main.py: lifespan, seed user, static files, logfire."""

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from klangk_backend import main, model


# --- Seed default user ---


class TestSeedDefaultUser:
    async def test_creates_user_when_missing(self, db, monkeypatch):
        monkeypatch.setenv("KLANGK_DEFAULT_USER", "seed-test")
        monkeypatch.setenv("KLANGK_DEFAULT_PASSWORD", "seed-pass")
        await main.seed_default_user()
        user = await model.get_user_by_email("seed-test")
        assert user is not None

    async def test_skips_existing_user(self, db, monkeypatch):
        monkeypatch.setenv("KLANGK_DEFAULT_USER", "seed-test")
        monkeypatch.setenv("KLANGK_DEFAULT_PASSWORD", "seed-pass")
        await main.seed_default_user()
        # Call again — should not raise
        await main.seed_default_user()
        user = await model.get_user_by_email("seed-test")
        assert user is not None

    async def test_generates_password_when_not_set(self, db, monkeypatch):
        monkeypatch.setenv("KLANGK_DEFAULT_USER", "gen-test")
        monkeypatch.delenv("KLANGK_DEFAULT_PASSWORD", raising=False)
        await main.seed_default_user()
        user = await model.get_user_by_email("gen-test")
        assert user is not None
        # User exists and is in the admin group
        admin_group = await model.get_group_by_name("admin")
        assert admin_group is not None
        group_ids = await model.get_user_group_ids(user["id"])
        assert admin_group["id"] in group_ids

    async def test_generated_password_printed_to_stderr(
        self, db, monkeypatch, caplog, capsys
    ):
        monkeypatch.setenv("KLANGK_DEFAULT_USER", "log-test")
        monkeypatch.delenv("KLANGK_DEFAULT_PASSWORD", raising=False)
        import logging

        with caplog.at_level(logging.INFO):
            await main.seed_default_user()
        # Password must NOT appear in log output (security)
        assert "password printed to stderr" in caplog.text
        assert "log-test" in caplog.text
        # Password appears on stderr only
        captured = capsys.readouterr()
        assert "Default admin password for" in captured.err


# --- Seed agent user ---


class TestSeedAgentUser:
    async def test_creates_agent_user(self, db):
        await main.seed_agent_user()
        user = await model.get_user_by_id(model.AGENT_USER_ID)
        assert user is not None
        assert user["email"] == "MrBoops@example.com"
        assert user["handle"] == "MrBoops"

    async def test_custom_env_vars(self, db, monkeypatch):
        monkeypatch.setenv("KLANGK_CHAT_AGENT_EMAIL", "bot@test.com")
        monkeypatch.setenv("KLANGK_CHAT_AGENT_HANDLE", "TestBot")
        await main.seed_agent_user()
        user = await model.get_user_by_id(model.AGENT_USER_ID)
        assert user is not None
        assert user["email"] == "bot@test.com"
        assert user["handle"] == "TestBot"

    async def test_upserts_existing(self, db, monkeypatch):
        await main.seed_agent_user()
        monkeypatch.setenv("KLANGK_CHAT_AGENT_EMAIL", "new@test.com")
        monkeypatch.setenv("KLANGK_CHAT_AGENT_HANDLE", "NewBot")
        await main.seed_agent_user()
        user = await model.get_user_by_id(model.AGENT_USER_ID)
        assert user["email"] == "new@test.com"
        assert user["handle"] == "NewBot"

    async def test_clears_cache(self, db):
        # Prime cache with fallback
        await model.get_agent_user()
        await main.seed_agent_user()
        # Cache should now reflect DB values
        agent = await model.get_agent_user()
        assert agent["email"] == "MrBoops@example.com"


# --- Lifespan ---


class TestLifespan:
    async def test_lifespan_starts_and_stops(self, db, monkeypatch):
        monkeypatch.delenv("KLANGK_OIDC_CONFIG", raising=False)
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        monkeypatch.delenv("KLANGK_PREVENT_INSECURE_JWT_SECRET", raising=False)
        app = FastAPI()
        with (
            patch.object(
                main.container.registry,
                "adopt_orphaned_containers",
                new_callable=AsyncMock,
            ) as mock_adopt,
            patch.object(
                main.container.registry, "start_cleanup_loop"
            ) as mock_start,
            patch.object(
                main.container.registry,
                "shutdown",
                new_callable=AsyncMock,
            ) as mock_shutdown,
            patch(
                "klangk_backend.main.check_pid_file", return_value=None
            ) as mock_check,
            patch("klangk_backend.main.write_pid_file") as mock_write,
            patch("klangk_backend.main.remove_pid_file") as mock_remove,
        ):
            async with main.lifespan(app):
                mock_check.assert_called_once()
                mock_write.assert_called_once()
                mock_adopt.assert_awaited_once()
                mock_start.assert_called_once()
            mock_shutdown.assert_awaited_once()
            mock_remove.assert_called_once()

    async def test_lifespan_refuses_if_pid_alive(self, db, monkeypatch):
        monkeypatch.delenv("KLANGK_OIDC_CONFIG", raising=False)
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        monkeypatch.delenv("KLANGK_PREVENT_INSECURE_JWT_SECRET", raising=False)
        app = FastAPI()
        with (
            patch("klangk_backend.main.check_pid_file", return_value=12345),
            pytest.raises(SystemExit),
        ):
            async with main.lifespan(app):
                pass  # pragma: no cover


# --- Static files ---


class TestSetupStaticFiles:
    async def test_mounts_static_files_and_adds_middleware(self, tmp_path):
        # Create a fake frontend directory with an index.html
        (tmp_path / "index.html").write_text("<html>hello</html>")

        test_app = FastAPI()
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/index.html")
        assert resp.status_code == 200
        assert b"hello" in resp.content

    async def test_no_cache_headers_on_html(self, tmp_path):
        (tmp_path / "index.html").write_text("<html>hi</html>")

        test_app = FastAPI()
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/index.html")
        assert (
            resp.headers["Cache-Control"]
            == "no-cache, no-store, must-revalidate"
        )
        assert resp.headers["Pragma"] == "no-cache"
        assert resp.headers["Expires"] == "0"

    async def test_no_cache_headers_on_js(self, tmp_path):
        (tmp_path / "app.js").write_text("console.log('hi')")

        test_app = FastAPI()
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/app.js")
        assert (
            resp.headers["Cache-Control"]
            == "no-cache, no-store, must-revalidate"
        )

    async def test_no_cache_headers_not_on_other_files(self, tmp_path):
        (tmp_path / "image.png").write_bytes(b"\x89PNG")

        test_app = FastAPI()
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            resp = await client.get("/image.png")
        assert "Cache-Control" not in resp.headers


# --- Logfire ---


class TestSetupLogfire:
    def test_no_token_returns_false(self, monkeypatch):
        monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)
        app = FastAPI()
        assert main.setup_logfire(app) is False

    def test_with_token_instruments_app(self, monkeypatch):
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        monkeypatch.delenv("LOGFIRE_BASE_URL", raising=False)
        monkeypatch.delenv("LOGFIRE_ENVIRONMENT", raising=False)
        mock_logfire = MagicMock()
        with patch.dict("sys.modules", {"logfire": mock_logfire}):
            app = FastAPI()
            result = main.setup_logfire(app)
        assert result is True
        mock_logfire.configure.assert_called_once_with()
        mock_logfire.instrument_fastapi.assert_called_once_with(app)


class TestCorsOrigins:
    def test_default_localhost(self, monkeypatch):
        monkeypatch.delenv("KLANGK_CORS_ORIGINS", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.delenv("KLANGK_NGINX_PORT", raising=False)
        assert main._cors_origins() == ["http://localhost:8995"]

    def test_custom_nginx_port(self, monkeypatch):
        monkeypatch.delenv("KLANGK_CORS_ORIGINS", raising=False)
        monkeypatch.delenv("KLANGK_HOSTING_HOSTNAME", raising=False)
        monkeypatch.setenv("KLANGK_NGINX_PORT", "9000")
        assert main._cors_origins() == ["http://localhost:9000"]

    def test_hosting_hostname(self, monkeypatch):
        monkeypatch.delenv("KLANGK_CORS_ORIGINS", raising=False)
        monkeypatch.setenv("KLANGK_HOSTING_HOSTNAME", "klangk.example.com")
        monkeypatch.setenv("KLANGK_HOSTING_PROTO", "https")
        assert main._cors_origins() == ["https://klangk.example.com"]

    def test_hosting_hostname_default_proto(self, monkeypatch):
        monkeypatch.delenv("KLANGK_CORS_ORIGINS", raising=False)
        monkeypatch.setenv("KLANGK_HOSTING_HOSTNAME", "klangk.example.com")
        monkeypatch.delenv("KLANGK_HOSTING_PROTO", raising=False)
        assert main._cors_origins() == ["http://klangk.example.com"]

    def test_explicit_origins(self, monkeypatch):
        monkeypatch.setenv(
            "KLANGK_CORS_ORIGINS",
            "https://a.example.com, https://b.example.com",
        )
        assert main._cors_origins() == [
            "https://a.example.com",
            "https://b.example.com",
        ]

    def test_explicit_origins_strips_empties(self, monkeypatch):
        monkeypatch.setenv("KLANGK_CORS_ORIGINS", "https://a.com,,")
        assert main._cors_origins() == ["https://a.com"]

    def test_explicit_overrides_hosting(self, monkeypatch):
        monkeypatch.setenv("KLANGK_CORS_ORIGINS", "https://override.com")
        monkeypatch.setenv("KLANGK_HOSTING_HOSTNAME", "ignored.com")
        assert main._cors_origins() == ["https://override.com"]

    def test_with_base_url_and_environment(self, monkeypatch):
        monkeypatch.setenv("LOGFIRE_TOKEN", "test-token")
        monkeypatch.setenv("LOGFIRE_BASE_URL", "https://custom.logfire")
        monkeypatch.setenv("LOGFIRE_ENVIRONMENT", "staging")
        mock_logfire = MagicMock()
        with patch.dict("sys.modules", {"logfire": mock_logfire}):
            app = FastAPI()
            main.setup_logfire(app)
        mock_logfire.configure.assert_called_once_with(
            base_url="https://custom.logfire",
            environment="staging",
        )


# --- PID file helpers ---


class TestPidFile:
    def test_check_pid_file_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            main, "_pid_file_path", lambda: tmp_path / "klangk-test.pid"
        )
        assert main.check_pid_file() is None

    def test_check_pid_file_stale_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        # Use a PID that (almost certainly) doesn't exist
        pid_file.write_text("2000000")
        monkeypatch.setattr(main, "_pid_file_path", lambda: pid_file)
        assert main.check_pid_file() is None
        assert not pid_file.exists()

    def test_check_pid_file_own_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text(str(os.getpid()))
        monkeypatch.setattr(main, "_pid_file_path", lambda: pid_file)
        assert main.check_pid_file() is None

    def test_check_pid_file_live_pid_permission_error(
        self, tmp_path, monkeypatch
    ):
        pid_file = tmp_path / "klangk-test.pid"
        # PID 1 (init) is always alive
        pid_file.write_text("1")
        monkeypatch.setattr(main, "_pid_file_path", lambda: pid_file)
        # os.kill(1, 0) raises PermissionError for non-root
        result = main.check_pid_file()
        assert result == 1

    def test_check_pid_file_live_foreign_pid(self, tmp_path, monkeypatch):
        """Live PID that os.kill(pid, 0) succeeds on (not our PID)."""
        pid_file = tmp_path / "klangk-test.pid"
        # Use a PID we know is alive and can signal (our parent process)
        ppid = os.getppid()
        pid_file.write_text(str(ppid))
        monkeypatch.setattr(main, "_pid_file_path", lambda: pid_file)
        result = main.check_pid_file()
        assert result == ppid

    def test_check_pid_file_invalid_content(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text("not-a-number")
        monkeypatch.setattr(main, "_pid_file_path", lambda: pid_file)
        assert main.check_pid_file() is None

    def test_write_and_remove_pid_file(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        monkeypatch.setattr(main, "_pid_file_path", lambda: pid_file)
        main.write_pid_file()
        assert pid_file.read_text() == str(os.getpid())
        main.remove_pid_file()
        assert not pid_file.exists()

    def test_remove_pid_file_only_own_pid(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        pid_file.write_text("99999")
        monkeypatch.setattr(main, "_pid_file_path", lambda: pid_file)
        main.remove_pid_file()
        # File should still exist — not our PID
        assert pid_file.exists()

    def test_remove_pid_file_missing(self, tmp_path, monkeypatch):
        pid_file = tmp_path / "klangk-test.pid"
        monkeypatch.setattr(main, "_pid_file_path", lambda: pid_file)
        # Should not raise
        main.remove_pid_file()

    def test_pid_file_path_uses_run_dir(self):
        path = main._pid_file_path()
        assert path.parent == Path(f"/run/user/{os.getuid()}")
        assert f"klangk-{main.container.INSTANCE_ID}" in path.name
