"""Tests for main.py: lifespan, seed user, static files, logfire."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from bark_backend import main, user_store


# --- Seed default user ---


class TestSeedDefaultUser:
    async def test_creates_user_when_missing(self, db, monkeypatch):
        monkeypatch.setenv("BARK_DEFAULT_USER", "seed-test")
        monkeypatch.setenv("BARK_DEFAULT_PASSWORD", "seed-pass")
        await main.seed_default_user()
        user = await user_store.get_user_by_email("seed-test")
        assert user is not None

    async def test_skips_existing_user(self, db, monkeypatch):
        monkeypatch.setenv("BARK_DEFAULT_USER", "seed-test")
        monkeypatch.setenv("BARK_DEFAULT_PASSWORD", "seed-pass")
        await main.seed_default_user()
        # Call again — should not raise
        await main.seed_default_user()
        user = await user_store.get_user_by_email("seed-test")
        assert user is not None

    async def test_generates_password_when_not_set(self, db, monkeypatch):
        monkeypatch.setenv("BARK_DEFAULT_USER", "gen-test")
        monkeypatch.delenv("BARK_DEFAULT_PASSWORD", raising=False)
        await main.seed_default_user()
        user = await user_store.get_user_by_email("gen-test")
        assert user is not None
        # Password was generated, so we can't know it, but user exists
        # and has admin role
        roles = await user_store.get_user_roles(user["id"])
        assert "admin" in roles

    async def test_generated_password_is_logged(self, db, monkeypatch, caplog):
        monkeypatch.setenv("BARK_DEFAULT_USER", "log-test")
        monkeypatch.delenv("BARK_DEFAULT_PASSWORD", raising=False)
        import logging

        with caplog.at_level(logging.INFO):
            await main.seed_default_user()
        assert "generated password:" in caplog.text
        assert "log-test" in caplog.text


# --- Lifespan ---


class TestLifespan:
    async def test_lifespan_starts_and_stops(self, db):
        app = FastAPI()
        with (
            patch.object(
                main.container_manager.registry,
                "adopt_orphaned_containers",
                new_callable=AsyncMock,
            ) as mock_adopt,
            patch.object(
                main.container_manager.registry, "start_cleanup_loop"
            ) as mock_start,
            patch.object(
                main.container_manager.registry,
                "shutdown",
                new_callable=AsyncMock,
            ) as mock_shutdown,
        ):
            async with main.lifespan(app):
                mock_adopt.assert_awaited_once()
                mock_start.assert_called_once()
            mock_shutdown.assert_awaited_once()


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
