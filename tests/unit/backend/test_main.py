"""Tests for main.py: lifespan, seed user, proxy, WebSocket endpoint."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from backend import main, user_store


# --- Seed default user ---


class TestSeedDefaultUser:
    async def test_creates_user_when_missing(self, db, monkeypatch):
        monkeypatch.setenv("BARK_DEFAULT_USER", "seed-test")
        monkeypatch.setenv("BARK_DEFAULT_PASSWORD", "seed-pass")
        await main._seed_default_user()
        user = await user_store.get_user_by_username("seed-test")
        assert user is not None

    async def test_skips_existing_user(self, db, monkeypatch):
        monkeypatch.setenv("BARK_DEFAULT_USER", "seed-test")
        monkeypatch.setenv("BARK_DEFAULT_PASSWORD", "seed-pass")
        await main._seed_default_user()
        # Call again — should not raise
        await main._seed_default_user()
        user = await user_store.get_user_by_username("seed-test")
        assert user is not None


# --- Lifespan ---


class TestLifespan:
    async def test_lifespan_starts_and_stops(self, db):
        app = FastAPI()
        with (
            patch.object(main.container_manager, "start_cleanup_loop") as mock_start,
            patch.object(
                main.container_manager, "shutdown", new_callable=AsyncMock
            ) as mock_shutdown,
        ):
            async with main.lifespan(app):
                mock_start.assert_called_once()
            mock_shutdown.assert_awaited_once()


# --- Proxy ---


class TestProxy:
    @pytest.fixture
    def mock_proxy_client(self):
        client = AsyncMock(spec=httpx.AsyncClient)
        return client

    async def test_proxy_success(self, mock_proxy_client):
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.aiter_bytes = lambda: _async_iter([b"<html>hello</html>"])
        mock_resp.aclose = AsyncMock()

        mock_proxy_client.build_request = MagicMock(return_value=MagicMock())
        mock_proxy_client.send = AsyncMock(return_value=mock_resp)

        transport = ASGITransport(app=main.app)
        with patch.object(main, "_get_proxy_client", return_value=mock_proxy_client):
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/hosted/ws-1/9000/index.html")
        assert resp.status_code == 200
        assert b"hello" in resp.content

    async def test_proxy_with_query_string(self, mock_proxy_client):
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.aiter_bytes = lambda: _async_iter([b"ok"])
        mock_resp.aclose = AsyncMock()

        mock_proxy_client.build_request = MagicMock(return_value=MagicMock())
        mock_proxy_client.send = AsyncMock(return_value=mock_resp)

        transport = ASGITransport(app=main.app)
        with patch.object(main, "_get_proxy_client", return_value=mock_proxy_client):
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/hosted/ws-1/9000/api?foo=bar")
        assert resp.status_code == 200
        # Verify query was included in upstream URL
        built_url = mock_proxy_client.build_request.call_args[1]["url"]
        assert "?foo=bar" in built_url

    async def test_proxy_connect_error(self, mock_proxy_client):
        mock_proxy_client.build_request = MagicMock(return_value=MagicMock())
        mock_proxy_client.send = AsyncMock(side_effect=httpx.ConnectError("refused"))

        transport = ASGITransport(app=main.app)
        with patch.object(main, "_get_proxy_client", return_value=mock_proxy_client):
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/hosted/ws-1/9000/")
        assert resp.status_code == 502
        assert "not running" in resp.json()["detail"]

    async def test_proxy_remote_protocol_error(self, mock_proxy_client):
        mock_proxy_client.build_request = MagicMock(return_value=MagicMock())
        mock_proxy_client.send = AsyncMock(
            side_effect=httpx.RemoteProtocolError("bad response")
        )

        transport = ASGITransport(app=main.app)
        with patch.object(main, "_get_proxy_client", return_value=mock_proxy_client):
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/hosted/ws-1/9000/")
        assert resp.status_code == 502
        assert "valid response" in resp.json()["detail"]

    async def test_proxy_timeout(self, mock_proxy_client):
        mock_proxy_client.build_request = MagicMock(return_value=MagicMock())
        mock_proxy_client.send = AsyncMock(
            side_effect=httpx.TimeoutException("timed out")
        )

        transport = ASGITransport(app=main.app)
        with patch.object(main, "_get_proxy_client", return_value=mock_proxy_client):
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/hosted/ws-1/9000/")
        assert resp.status_code == 504
        assert "not respond" in resp.json()["detail"]

    async def test_proxy_filters_hop_headers(self, mock_proxy_client):
        mock_resp = AsyncMock()
        mock_resp.status_code = 200
        mock_resp.headers = {
            "content-type": "text/plain",
            "transfer-encoding": "chunked",
            "connection": "keep-alive",
            "x-custom": "kept",
        }
        mock_resp.aiter_bytes = lambda: _async_iter([b"data"])
        mock_resp.aclose = AsyncMock()

        mock_proxy_client.build_request = MagicMock(return_value=MagicMock())
        mock_proxy_client.send = AsyncMock(return_value=mock_resp)

        transport = ASGITransport(app=main.app)
        with patch.object(main, "_get_proxy_client", return_value=mock_proxy_client):
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.get("/hosted/ws-1/9000/data")
        assert "x-custom" in resp.headers
        assert "connection" not in resp.headers

    async def test_proxy_post_with_body(self, mock_proxy_client):
        mock_resp = AsyncMock()
        mock_resp.status_code = 201
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.aiter_bytes = lambda: _async_iter([b'{"ok":true}'])
        mock_resp.aclose = AsyncMock()

        mock_proxy_client.build_request = MagicMock(return_value=MagicMock())
        mock_proxy_client.send = AsyncMock(return_value=mock_resp)

        transport = ASGITransport(app=main.app)
        with patch.object(main, "_get_proxy_client", return_value=mock_proxy_client):
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                resp = await client.post("/hosted/ws-1/9000/api", content=b'{"data":1}')
        assert resp.status_code == 201
        # Verify body was forwarded
        build_kwargs = mock_proxy_client.build_request.call_args[1]
        assert build_kwargs["content"] == b'{"data":1}'


# --- Static files ---


class TestSetupStaticFiles:
    async def test_mounts_static_files_and_adds_middleware(self, tmp_path):
        # Create a fake frontend directory with an index.html
        (tmp_path / "index.html").write_text("<html>hello</html>")

        test_app = FastAPI()
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/index.html")
        assert resp.status_code == 200
        assert b"hello" in resp.content

    async def test_no_cache_headers_on_html(self, tmp_path):
        (tmp_path / "index.html").write_text("<html>hi</html>")

        test_app = FastAPI()
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/index.html")
        assert resp.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"
        assert resp.headers["Pragma"] == "no-cache"
        assert resp.headers["Expires"] == "0"

    async def test_no_cache_headers_on_js(self, tmp_path):
        (tmp_path / "app.js").write_text("console.log('hi')")

        test_app = FastAPI()
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/app.js")
        assert resp.headers["Cache-Control"] == "no-cache, no-store, must-revalidate"

    async def test_no_cache_headers_not_on_other_files(self, tmp_path):
        (tmp_path / "image.png").write_bytes(b"\x89PNG")

        test_app = FastAPI()
        main.setup_static_files(test_app, tmp_path)

        transport = ASGITransport(app=test_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/image.png")
        assert "Cache-Control" not in resp.headers


async def _async_iter(items):
    for item in items:
        yield item
