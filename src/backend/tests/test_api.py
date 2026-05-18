"""Tests for api.py: HTTP route handlers via FastAPI TestClient."""

import io
import zipfile

import pytest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from bark_backend import api, container_manager, user_store


@pytest.fixture
async def app(db):
    """Create a minimal FastAPI app with just the API router."""
    app = FastAPI()
    app.include_router(api.router)
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _auth_headers(client):
    resp = await client.post(
        "/auth/login", json={"username": "testuser", "password": "testpass"}
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --- Config ---


class TestConfig:
    async def test_get_config(self, client):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        assert "soliplex_url" in resp.json()


# --- Auth routes ---


class TestAuthRoutes:
    async def test_register(self, client):
        resp = await client.post(
            "/auth/register", json={"username": "newuser", "password": "newpass"}
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_register_duplicate(self, client, user):
        resp = await client.post(
            "/auth/register", json={"username": "testuser", "password": "pass"}
        )
        assert resp.status_code == 400

    async def test_login(self, client, user):
        resp = await client.post(
            "/auth/login", json={"username": "testuser", "password": "testpass"}
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_login_bad_password(self, client, user):
        resp = await client.post(
            "/auth/login", json={"username": "testuser", "password": "wrong"}
        )
        assert resp.status_code == 401

    async def test_logout(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            api.container_manager, "stop_user_containers", new_callable=AsyncMock
        ):
            resp = await client.post("/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_logout_no_auth(self, client):
        resp = await client.post("/auth/logout")
        assert resp.status_code == 401


# --- Workspace routes ---


class TestWorkspaceRoutes:
    async def test_list_empty(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/workspaces", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_create_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post("/workspaces?name=test-ws", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-ws"
        assert "id" in data

    async def test_create_duplicate(self, client, user):
        headers = await _auth_headers(client)
        await client.post("/workspaces?name=dup", headers=headers)
        resp = await client.post("/workspaces?name=dup", headers=headers)
        assert resp.status_code == 400

    async def test_delete_workspace(self, client, user):
        headers = await _auth_headers(client)
        create_resp = await client.post("/workspaces?name=doomed", headers=headers)
        ws_id = create_resp.json()["id"]

        with patch.object(
            api.container_manager, "stop_and_remove_container", new_callable=AsyncMock
        ):
            resp = await client.delete(f"/workspaces/{ws_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    async def test_delete_nonexistent(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete("/workspaces/fake-id", headers=headers)
        assert resp.status_code == 404

    async def test_delete_workspace_with_container(self, client, user):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/workspaces?name=has-container", headers=headers
        )
        ws_id = create_resp.json()["id"]
        # Simulate a running container
        await user_store.update_workspace_container(ws_id, "fake-container-id")

        with patch.object(
            api.container_manager, "stop_and_remove_container", new_callable=AsyncMock
        ) as mock_rm:
            resp = await client.delete(f"/workspaces/{ws_id}", headers=headers)
        assert resp.status_code == 200
        mock_rm.assert_awaited_once_with("fake-container-id")

    async def test_list_no_auth(self, client):
        resp = await client.get("/workspaces")
        assert resp.status_code == 401


# --- Messages ---


class TestMessageRoutes:
    async def test_get_messages(self, client, user):
        headers = await _auth_headers(client)
        create_resp = await client.post("/workspaces?name=msg-ws", headers=headers)
        ws_id = create_resp.json()["id"]

        await user_store.save_message(ws_id, "user", "hello")
        resp = await client.get(f"/workspaces/{ws_id}/messages", headers=headers)
        assert resp.status_code == 200
        msgs = resp.json()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"

    async def test_get_messages_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/workspaces/fake-id/messages", headers=headers)
        assert resp.status_code == 404


# --- File routes ---


class TestFileRoutes:
    async def _create_workspace(self, client, headers):
        resp = await client.post("/workspaces?name=file-ws", headers=headers)
        return resp.json()["id"]

    async def test_list_files(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.get(f"/workspaces/{ws_id}/files?path=.", headers=headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_list_files_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/workspaces/fake-id/files?path=.", headers=headers)
        assert resp.status_code == 404

    async def test_upload_and_read(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)

        resp = await client.post(
            f"/workspaces/{ws_id}/files/upload?path=hello.txt",
            headers=headers,
            files={"file": ("hello.txt", b"hello world", "text/plain")},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "uploaded"

        resp = await client.get(
            f"/workspaces/{ws_id}/files/content?path=hello.txt", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "hello world"

    async def test_read_nonexistent(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.get(
            f"/workspaces/{ws_id}/files/content?path=nope.txt", headers=headers
        )
        assert resp.status_code == 404

    async def test_upload_no_filename(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.post(
            f"/workspaces/{ws_id}/files/upload",
            headers=headers,
            files={"file": ("", b"data", "application/octet-stream")},
        )
        assert resp.status_code in (400, 422)

    async def test_delete_file(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)

        await client.post(
            f"/workspaces/{ws_id}/files/upload?path=doomed.txt",
            headers=headers,
            files={"file": ("doomed.txt", b"bye", "text/plain")},
        )
        resp = await client.delete(
            f"/workspaces/{ws_id}/files?path=doomed.txt", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    async def test_delete_nonexistent_file(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.delete(
            f"/workspaces/{ws_id}/files?path=ghost.txt", headers=headers
        )
        assert resp.status_code == 404

    async def test_rename_file(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)

        await client.post(
            f"/workspaces/{ws_id}/files/upload?path=old.txt",
            headers=headers,
            files={"file": ("old.txt", b"data", "text/plain")},
        )
        resp = await client.post(
            f"/workspaces/{ws_id}/files/rename?old_path=old.txt&new_path=new.txt",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "renamed"

    async def test_rename_nonexistent(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.post(
            f"/workspaces/{ws_id}/files/rename?old_path=nope.txt&new_path=new.txt",
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_rename_to_existing(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)

        await client.post(
            f"/workspaces/{ws_id}/files/upload?path=a.txt",
            headers=headers,
            files={"file": ("a.txt", b"a", "text/plain")},
        )
        await client.post(
            f"/workspaces/{ws_id}/files/upload?path=b.txt",
            headers=headers,
            files={"file": ("b.txt", b"b", "text/plain")},
        )
        resp = await client.post(
            f"/workspaces/{ws_id}/files/rename?old_path=a.txt&new_path=b.txt",
            headers=headers,
        )
        assert resp.status_code == 409

    async def test_download_file(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)

        await client.post(
            f"/workspaces/{ws_id}/files/upload?path=dl.txt",
            headers=headers,
            files={"file": ("dl.txt", b"download me", "text/plain")},
        )
        resp = await client.get(
            f"/workspaces/{ws_id}/files/download?path=dl.txt", headers=headers
        )
        assert resp.status_code == 200
        assert resp.content == b"download me"

    async def test_download_directory_as_zip(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)

        await client.post(
            f"/workspaces/{ws_id}/files/upload?path=mydir/a.txt",
            headers=headers,
            files={"file": ("a.txt", b"aaa", "text/plain")},
        )
        await client.post(
            f"/workspaces/{ws_id}/files/upload?path=mydir/b.txt",
            headers=headers,
            files={"file": ("b.txt", b"bbb", "text/plain")},
        )
        resp = await client.get(
            f"/workspaces/{ws_id}/files/download?path=mydir", headers=headers
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"

        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        names = zf.namelist()
        assert "a.txt" in names
        assert "b.txt" in names
        assert zf.read("a.txt") == b"aaa"

    async def test_download_nonexistent(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.get(
            f"/workspaces/{ws_id}/files/download?path=nope.txt", headers=headers
        )
        assert resp.status_code == 404

    async def test_upload_to_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces/fake-id/files/upload?path=f.txt",
            headers=headers,
            files={"file": ("f.txt", b"data", "text/plain")},
        )
        assert resp.status_code == 404

    async def test_file_traversal_rejected(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.get(
            f"/workspaces/{ws_id}/files/content?path=../../etc/passwd",
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_list_files_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.get(
            f"/workspaces/{ws_id}/files?path=../../etc",
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_delete_file_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete(
            "/workspaces/fake-id/files?path=f.txt", headers=headers
        )
        assert resp.status_code == 404

    async def test_delete_file_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.delete(
            f"/workspaces/{ws_id}/files?path=../../etc/passwd",
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_rename_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces/fake-id/files/rename?old_path=a&new_path=b",
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_rename_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.post(
            f"/workspaces/{ws_id}/files/rename?old_path=../../etc/passwd&new_path=stolen",
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_download_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/workspaces/fake-id/files/download?path=f.txt", headers=headers
        )
        assert resp.status_code == 404

    async def test_download_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.get(
            f"/workspaces/{ws_id}/files/download?path=../../etc/passwd",
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_read_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/workspaces/fake-id/files/content?path=f.txt", headers=headers
        )
        assert resp.status_code == 404

    async def test_upload_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.post(
            f"/workspaces/{ws_id}/files/upload?path=../../etc/evil",
            headers=headers,
            files={"file": ("evil.txt", b"bad", "text/plain")},
        )
        assert resp.status_code == 400


# --- Test mode endpoint ---


class TestSetIdleTimeout:
    async def test_set_idle_timeout_global(self, db):
        """Setting global idle timeout changes the module-level variable."""
        original_timeout = container_manager.IDLE_TIMEOUT_SECONDS
        try:
            container_manager.IDLE_TIMEOUT_SECONDS = 42
            assert container_manager.IDLE_TIMEOUT_SECONDS == 42
            # Per-workspace lookup falls back to global
            assert container_manager.get_workspace_idle_timeout("any") == 42
        finally:
            container_manager.IDLE_TIMEOUT_SECONDS = original_timeout

    async def test_endpoint_missing_without_test_mode(self, client):
        """Without BARK_TEST_MODE, the endpoints should not exist."""
        resp = await client.post("/api/test/set-idle-timeout?seconds=10")
        assert resp.status_code in (404, 405)
        resp = await client.get("/api/test/idle-timeout")
        assert resp.status_code in (404, 405)

    async def test_set_idle_timeout_per_workspace(self, db):
        """Per-workspace idle timeout should not affect global."""
        original_timeout = container_manager.IDLE_TIMEOUT_SECONDS
        try:
            container_manager.set_workspace_idle_timeout("ws-test", 5)
            assert container_manager.get_workspace_idle_timeout("ws-test") == 5
            assert container_manager.IDLE_TIMEOUT_SECONDS == original_timeout
            # Unknown workspace returns global default
            assert (
                container_manager.get_workspace_idle_timeout("ws-other")
                == original_timeout
            )
        finally:
            container_manager._workspace_idle_timeouts.clear()
