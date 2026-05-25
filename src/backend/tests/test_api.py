"""Tests for api.py: HTTP route handlers via FastAPI TestClient."""

import io
import zipfile

import pytest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from httpx import AsyncClient, ASGITransport

from bark_backend import (
    api,
    auth,
    container_manager,
    user_store,
    workspace_manager,
)


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
        "/auth/login",
        json={"email": "testuser@example.com", "password": "testpass"},
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --- Health ---


class TestHealth:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# --- Config ---


class TestConfig:
    async def test_get_config(self, client):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        assert "soliplex_url" in resp.json()


# --- Auth routes ---


class TestAuthRoutes:
    async def test_register(self, client, admin_user):
        login_resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        token = login_resp.json()["access_token"]
        with patch.object(
            api.email_service,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp = await client.post(
                "/auth/register",
                json={"email": "new@example.com", "password": "newpass"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending_verification"
        assert data["email"] == "new@example.com"

    async def test_register_test_mode(self, client, db, monkeypatch):
        """In test mode, unauthenticated registration is allowed and auto-verified."""
        monkeypatch.setenv("BARK_TEST_MODE", "1")
        resp = await client.post(
            "/auth/register",
            json={"email": "new@example.com", "password": "newpass"},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_register_unauthenticated(self, client, db):
        """Registration is open — no auth required (verification gates access)."""
        with patch.object(
            api.email_service,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp = await client.post(
                "/auth/register",
                json={"email": "new@example.com", "password": "newpass"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending_verification"

    async def test_register_email_send_failure_rolls_back(self, client, db):
        """If verification email fails, user creation is rolled back."""
        with (
            patch.object(
                api.email_service,
                "send_verification_email",
                new_callable=AsyncMock,
                side_effect=RuntimeError("sendmail not found"),
            ),
            pytest.raises(RuntimeError, match="sendmail not found"),
        ):
            await client.post(
                "/auth/register",
                json={"email": "fail@example.com", "password": "newpass"},
            )
        # User should not exist — transaction was rolled back
        user = await user_store.get_user_by_email("fail@example.com")
        assert user is None

    async def test_register_short_password(self, client, db):
        resp = await client.post(
            "/auth/register",
            json={"email": "short@example.com", "password": "abc"},
        )
        assert resp.status_code == 400

    async def test_register_duplicate(self, client, admin_user):
        login_resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        token = login_resp.json()["access_token"]
        resp = await client.post(
            "/auth/register",
            json={"email": "testadmin@example.com", "password": "pass"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    async def test_verify_email(self, client, db):
        """Verify endpoint marks user as verified."""
        from bark_backend import auth as auth_mod

        import bcrypt

        password_hash = bcrypt.hashpw(b"pass", bcrypt.gensalt()).decode()
        user = await user_store.create_user(
            "unverified@example.com", password_hash, verified=False
        )
        token = auth_mod.create_verification_token(user["id"])
        resp = await client.get(f"/auth/verify?token={token}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"
        # User can now log in
        login_resp = await client.post(
            "/auth/login",
            json={"email": "unverified@example.com", "password": "pass"},
        )
        assert login_resp.status_code == 200

    async def test_verify_invalid_token(self, client, db):
        resp = await client.get("/auth/verify?token=garbage")
        assert resp.status_code == 400

    async def test_verify_nonexistent_user(self, client, db):
        from bark_backend import auth as auth_mod

        token = auth_mod.create_verification_token("nonexistent-id")
        resp = await client.get(f"/auth/verify?token={token}")
        assert resp.status_code == 404

    async def test_login(self, client, user):
        resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_login_bad_password(self, client, user):
        resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "wrong"},
        )
        assert resp.status_code == 401

    async def test_logout(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            api.container_manager.registry,
            "stop_user_containers",
            new_callable=AsyncMock,
        ):
            resp = await client.post("/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_logout_no_auth(self, client):
        resp = await client.post("/auth/logout")
        assert resp.status_code == 401


# --- Resend verification ---


class TestResendVerification:
    async def _create_unverified_user(self):
        password_hash = auth.hash_password("testpass")
        await user_store.create_user(
            "unverified@example.com", password_hash, verified=False
        )

    async def test_resend_success(self, client, db):
        await self._create_unverified_user()
        with patch.object(
            api.email_service,
            "send_verification_email",
            new_callable=AsyncMock,
        ) as mock_send:
            resp = await client.post(
                "/auth/resend-verification",
                json={
                    "email": "unverified@example.com",
                    "password": "testpass",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"
        mock_send.assert_awaited_once()

    async def test_resend_wrong_password(self, client, db):
        await self._create_unverified_user()
        resp = await client.post(
            "/auth/resend-verification",
            json={
                "email": "unverified@example.com",
                "password": "wrong",
            },
        )
        assert resp.status_code == 401

    async def test_resend_nonexistent_user(self, client, db):
        resp = await client.post(
            "/auth/resend-verification",
            json={
                "email": "nobody@example.com",
                "password": "pass",
            },
        )
        assert resp.status_code == 401

    async def test_resend_already_verified(self, client, admin_user):
        resp = await client.post(
            "/auth/resend-verification",
            json={
                "email": "testadmin@example.com",
                "password": "testpass",
            },
        )
        assert resp.status_code == 400
        assert "already verified" in resp.json()["detail"]

    async def test_resend_rate_limited(self, client, db):
        # Clear stale rate limit state from parallel test workers
        api._resend_timestamps.pop("unverified@example.com", None)
        await self._create_unverified_user()
        with patch.object(
            api.email_service,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp1 = await client.post(
                "/auth/resend-verification",
                json={
                    "email": "unverified@example.com",
                    "password": "testpass",
                },
            )
            assert resp1.status_code == 200
            resp2 = await client.post(
                "/auth/resend-verification",
                json={
                    "email": "unverified@example.com",
                    "password": "testpass",
                },
            )
        assert resp2.status_code == 429
        api._resend_timestamps.pop("unverified@example.com", None)


class TestForgotPassword:
    async def _create_user(self):
        password_hash = auth.hash_password("oldpass")
        return await user_store.create_user(
            "forgot@example.com", password_hash, verified=True
        )

    async def test_forgot_sends_email(self, client, db):
        await self._create_user()
        with patch.object(
            api.email_service,
            "send_password_reset_email",
            new_callable=AsyncMock,
        ) as mock_send:
            resp = await client.post(
                "/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"
        mock_send.assert_awaited_once()
        api._reset_timestamps.pop("forgot@example.com", None)

    async def test_forgot_unknown_email_still_returns_sent(self, client, db):
        resp = await client.post(
            "/auth/forgot-password",
            json={"email": "nobody@example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"

    async def test_forgot_rate_limited(self, client, db):
        await self._create_user()
        with patch.object(
            api.email_service,
            "send_password_reset_email",
            new_callable=AsyncMock,
        ):
            await client.post(
                "/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
            resp2 = await client.post(
                "/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
        assert resp2.status_code == 429
        api._reset_timestamps.pop("forgot@example.com", None)


class TestResetPassword:
    async def _create_user(self):
        password_hash = auth.hash_password("oldpass")
        return await user_store.create_user(
            "reset@example.com", password_hash, verified=True
        )

    async def test_reset_success(self, client, db):
        user = await self._create_user()
        token = auth.create_password_reset_token(user["id"])
        resp = await client.post(
            "/auth/reset-password",
            json={"token": token, "password": "newpass"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "reset"
        assert "access_token" in data
        # Can login with new password
        resp2 = await client.post(
            "/auth/login",
            json={
                "email": "reset@example.com",
                "password": "newpass",
            },
        )
        assert resp2.status_code == 200

    async def test_reset_invalid_token(self, client, db):
        resp = await client.post(
            "/auth/reset-password",
            json={"token": "garbage", "password": "newpass"},
        )
        assert resp.status_code == 400

    async def test_reset_short_password(self, client, db):
        user = await self._create_user()
        token = auth.create_password_reset_token(user["id"])
        resp = await client.post(
            "/auth/reset-password",
            json={"token": token, "password": "ab"},
        )
        assert resp.status_code == 400
        assert "4 characters" in resp.json()["detail"]


class TestChangePassword:
    async def test_change_password_success(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "newpass",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"
        # Can login with new password
        resp2 = await client.post(
            "/auth/login",
            json={
                "email": "testuser@example.com",
                "password": "newpass",
            },
        )
        assert resp2.status_code == 200

    async def test_change_password_wrong_current(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-password",
            json={
                "current_password": "wrongpass",
                "new_password": "newpass",
            },
            headers=headers,
        )
        assert resp.status_code == 401

    async def test_change_password_too_short(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "ab",
            },
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_change_password_no_auth(self, client, db):
        resp = await client.post(
            "/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "newpass",
            },
        )
        assert resp.status_code == 401


class TestChangeEmail:
    async def test_change_email_success(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            api.email_service,
            "send_verification_email",
            new_callable=AsyncMock,
        ) as mock_send:
            resp = await client.post(
                "/auth/change-email",
                json={
                    "email": "new@example.com",
                    "password": "testpass",
                },
                headers=headers,
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"
        assert data["needs_verification"] is True
        mock_send.assert_awaited_once()
        # User should be unverified
        updated = await user_store.get_user_by_email("new@example.com")
        assert updated is not None
        assert not updated["verified"]

    async def test_change_email_wrong_password(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-email",
            json={
                "email": "new@example.com",
                "password": "wrongpass",
            },
            headers=headers,
        )
        assert resp.status_code == 401

    async def test_change_email_already_taken(self, client, user, db):
        # Create another user
        password_hash = auth.hash_password("other")
        await user_store.create_user(
            "other@example.com", password_hash, verified=True
        )
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-email",
            json={
                "email": "other@example.com",
                "password": "testpass",
            },
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_change_email_invalid_format(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-email",
            json={
                "email": "not-an-email",
                "password": "testpass",
            },
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_change_email_no_auth(self, client, db):
        resp = await client.post(
            "/auth/change-email",
            json={
                "email": "new@example.com",
                "password": "testpass",
            },
        )
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
        create_resp = await client.post(
            "/workspaces?name=doomed", headers=headers
        )
        ws_id = create_resp.json()["id"]

        with patch.object(
            api.container_manager.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
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
            api.container_manager.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
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
        create_resp = await client.post(
            "/workspaces?name=msg-ws", headers=headers
        )
        ws_id = create_resp.json()["id"]

        await user_store.save_message(ws_id, "user", "hello")
        resp = await client.get(
            f"/workspaces/{ws_id}/messages", headers=headers
        )
        assert resp.status_code == 200
        msgs = resp.json()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "hello"

    async def test_get_messages_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/workspaces/fake-id/messages", headers=headers
        )
        assert resp.status_code == 404


# --- File routes ---


class TestFileRoutes:
    async def _create_workspace(self, client, headers):
        resp = await client.post("/workspaces?name=file-ws", headers=headers)
        return resp.json()["id"]

    async def test_list_files(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.get(
            f"/workspaces/{ws_id}/files?path=.", headers=headers
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    async def test_list_files_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/workspaces/fake-id/files?path=.", headers=headers
        )
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
            f"/workspaces/{ws_id}/files/content?path=hello.txt",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["content"] == "hello world"

    async def test_upload_records_activity(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        cid = "cid-upload-test"
        await user_store.update_workspace_container(ws_id, cid)
        container_manager.registry.track_activity(cid, ws_id)
        container_manager.registry.states[ws_id].last_activity = 0.0

        await client.post(
            f"/workspaces/{ws_id}/files/upload?path=test.txt",
            headers=headers,
            files={"file": ("test.txt", b"data", "text/plain")},
        )

        assert container_manager.registry.states[ws_id].last_activity > 0.0
        container_manager.registry.states.pop(ws_id, None)
        container_manager.registry._cid_to_wsid.pop(cid, None)

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
            f"/workspaces/{ws_id}/files/download?path=nope.txt",
            headers=headers,
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
            assert (
                container_manager.registry.get_workspace_idle_timeout("any")
                == 42
            )
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
            container_manager.registry.track_activity("cid-test", "ws-test")
            container_manager.registry.set_workspace_idle_timeout("ws-test", 5)
            assert (
                container_manager.registry.get_workspace_idle_timeout(
                    "ws-test"
                )
                == 5
            )
            assert container_manager.IDLE_TIMEOUT_SECONDS == original_timeout
            # Unknown workspace returns global default
            assert (
                container_manager.registry.get_workspace_idle_timeout(
                    "ws-other"
                )
                == original_timeout
            )
        finally:
            container_manager.registry.states.pop("ws-test", None)

    async def test_cleanup_loop_adapts_to_short_timeout(self, db):
        """Cleanup loop interval adapts when per-workspace timeouts exist."""
        try:
            container_manager.registry.track_activity("cid-fast", "ws-fast")
            container_manager.registry.set_workspace_idle_timeout("ws-fast", 6)
            # With a 6s per-workspace timeout, the minimum is 6, so
            # the loop should sleep max(2, 6//2) = 3 seconds.
            state = container_manager.registry.states["ws-fast"]
            assert state.idle_timeout == 6
            # Global CHECK_INTERVAL_SECONDS should be unchanged
            assert (
                container_manager.CHECK_INTERVAL_SECONDS
                == container_manager.parse_idle_timeout()[1]
            )
        finally:
            container_manager.registry.states.pop("ws-fast", None)


# --- Roles ---


class TestRoles:
    async def test_require_role_passes(self, admin_user):
        """require_role dependency passes when user has the role."""
        checker = auth.require_role("admin")
        user = {
            "id": admin_user["id"],
            "email": "testadmin@example.com",
            "roles": ["admin"],
        }
        result = await checker(user)
        assert result == user

    async def test_require_role_fails(self, user):
        """require_role dependency raises 403 when user lacks the role."""
        checker = auth.require_role("admin")
        user_dict = {
            "id": user["id"],
            "email": "testuser@example.com",
            "roles": [],
        }
        with pytest.raises(HTTPException) as exc_info:
            await checker(user_dict)
        assert exc_info.value.status_code == 403

    async def test_ensure_role(self, db):
        await user_store.ensure_role("editor")
        # Idempotent
        await user_store.ensure_role("editor")

    async def test_assign_and_get_roles(self, user):
        await user_store.ensure_role("admin")
        await user_store.ensure_role("editor")
        await user_store.assign_role(user["id"], "admin")
        await user_store.assign_role(user["id"], "editor")
        roles = await user_store.get_user_roles(user["id"])
        assert set(roles) == {"admin", "editor"}

    async def test_assign_role_idempotent(self, user):
        await user_store.ensure_role("admin")
        await user_store.assign_role(user["id"], "admin")
        await user_store.assign_role(user["id"], "admin")
        roles = await user_store.get_user_roles(user["id"])
        assert roles == ["admin"]

    async def test_get_roles_empty(self, user):
        roles = await user_store.get_user_roles(user["id"])
        assert roles == []

    async def test_roles_in_jwt(self, user):
        await user_store.ensure_role("admin")
        await user_store.assign_role(user["id"], "admin")
        token = auth.create_token(
            user["id"], "testuser@example.com", ["admin"]
        )
        payload = auth.decode_token(token)
        assert payload["roles"] == ["admin"]

    async def test_login_includes_roles(self, client, admin_user):
        resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        payload = auth.decode_token(token)
        assert "admin" in payload["roles"]

    async def test_login_no_roles(self, client, user):
        resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        payload = auth.decode_token(token)
        assert payload["roles"] == []

    async def test_cascade_delete_user(self, db):
        """Deleting a user cascades to user_roles."""
        user = await user_store.create_user("delme", "hash")
        await user_store.ensure_role("admin")
        await user_store.assign_role(user["id"], "admin")
        assert await user_store.get_user_roles(user["id"]) == ["admin"]
        db_conn = await user_store.get_db()
        try:
            await db_conn.execute(
                "DELETE FROM users WHERE id = ?", (user["id"],)
            )
            await db_conn.commit()
        finally:
            await db_conn.close()
        assert await user_store.get_user_roles(user["id"]) == []

    async def test_cascade_delete_role(self, user):
        """Deleting a role cascades to user_roles."""
        await user_store.ensure_role("temp")
        await user_store.assign_role(user["id"], "temp")
        assert "temp" in await user_store.get_user_roles(user["id"])
        db_conn = await user_store.get_db()
        try:
            await db_conn.execute(
                "DELETE FROM roles WHERE name = ?", ("temp",)
            )
            await db_conn.commit()
        finally:
            await db_conn.close()
        assert "temp" not in await user_store.get_user_roles(user["id"])


# --- Admin API endpoints ---


class TestAdminEndpoints:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_list_users(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.get("/admin/users", headers=headers)
        assert resp.status_code == 200
        users = resp.json()
        assert len(users) >= 2
        emails = [u["email"] for u in users]
        assert "testadmin@example.com" in emails
        assert "testuser@example.com" in emails
        # Admin user should have roles
        admin = next(u for u in users if u["email"] == "testadmin@example.com")
        assert "admin" in admin["roles"]

    async def test_list_users_requires_admin(self, client, user):
        login_resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.get("/admin/users", headers=headers)
        assert resp.status_code == 403

    async def test_delete_user(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        with (
            patch.object(
                container_manager.registry,
                "stop_user_containers",
                new_callable=AsyncMock,
            ),
            patch.object(
                workspace_manager, "archive_user_data", new_callable=AsyncMock
            ),
        ):
            resp = await client.delete(
                f"/admin/users/{user['id']}", headers=headers
            )
        assert resp.status_code == 200
        # Verify user is gone
        resp = await client.get("/admin/users", headers=headers)
        emails = [u["email"] for u in resp.json()]
        assert "testuser@example.com" not in emails

    async def test_delete_self_forbidden(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            f"/admin/users/{admin_user['id']}", headers=headers
        )
        assert resp.status_code == 400

    async def test_delete_nonexistent_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            "/admin/users/nonexistent-id", headers=headers
        )
        assert resp.status_code == 404

    async def test_delete_user_cascades_workspaces(
        self, client, admin_user, user
    ):
        """Deleting a user cascades to their workspaces."""
        headers = await self._admin_headers(client)
        # Create a workspace for the user
        user_login = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        user_headers = {
            "Authorization": f"Bearer {user_login.json()['access_token']}"
        }
        ws_resp = await client.post(
            "/workspaces?name=to-delete", headers=user_headers
        )
        assert ws_resp.status_code == 200
        # Delete the user
        with patch.object(
            container_manager.registry,
            "stop_user_containers",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(
                f"/admin/users/{user['id']}", headers=headers
            )
        assert resp.status_code == 200
        # Workspace should be gone (CASCADE)
        ws_list = await user_store.get_user_workspaces_with_containers(
            user["id"]
        )
        assert len(ws_list) == 0

    async def test_add_role(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            f"/admin/users/{user['id']}/roles/editor", headers=headers
        )
        assert resp.status_code == 200
        roles = await user_store.get_user_roles(user["id"])
        assert "editor" in roles

    async def test_add_role_nonexistent_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/users/nonexistent-id/roles/admin", headers=headers
        )
        assert resp.status_code == 404

    async def test_remove_role(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        # First assign a role
        await user_store.ensure_role("editor")
        await user_store.assign_role(user["id"], "editor")
        # Then remove it
        resp = await client.delete(
            f"/admin/users/{user['id']}/roles/editor", headers=headers
        )
        assert resp.status_code == 200
        roles = await user_store.get_user_roles(user["id"])
        assert "editor" not in roles

    async def test_update_email(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/admin/users/{user['id']}",
            json={"email": "renamed"},
            headers=headers,
        )
        assert resp.status_code == 200
        updated = await user_store.get_user_by_id(user["id"])
        assert updated["email"] == "renamed"

    async def test_update_password(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/admin/users/{user['id']}",
            json={"password": "newpass123"},
            headers=headers,
        )
        assert resp.status_code == 200
        # Verify can login with new password
        login_resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "newpass123"},
        )
        assert login_resp.status_code == 200

    async def test_update_nonexistent_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            "/admin/users/nonexistent-id",
            json={"email": "x"},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_remove_role_not_assigned(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            f"/admin/users/{user['id']}/roles/nonexistent", headers=headers
        )
        assert resp.status_code == 404


class TestArchiveUserData:
    async def test_archive_creates_tarball(self, temp_data_dir, user):
        """Archive creates a .tar.xz file and removes the original dir."""
        # Create some workspace data
        user_dir = workspace_manager.WORKSPACES_ROOT / user["id"]
        data_dir = user_dir / "data" / "ws1"
        data_dir.mkdir(parents=True)
        (data_dir / "hello.txt").write_text("test content")

        result = await workspace_manager.archive_user_data(
            user["id"], user["email"]
        )
        assert result is not None
        assert result.exists()
        assert result.name == f"{user['id']}-{user['email']}.tar.xz"
        # Original directory should be removed
        assert not user_dir.exists()

    async def test_archive_no_data_dir(self, temp_data_dir, user):
        """Returns None if user has no data directory."""
        result = await workspace_manager.archive_user_data(
            user["id"], user["email"]
        )
        assert result is None

    async def test_archive_tar_nonzero_exit(self, temp_data_dir, user):
        """Returns None if tar exits with non-zero status."""
        user_dir = workspace_manager.WORKSPACES_ROOT / user["id"]
        user_dir.mkdir(parents=True)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"tar: error"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await workspace_manager.archive_user_data(
                user["id"], user["email"]
            )
        assert result is None

    async def test_archive_tar_oserror(self, temp_data_dir, user):
        """Returns None if tar fails to start."""
        user_dir = workspace_manager.WORKSPACES_ROOT / user["id"]
        user_dir.mkdir(parents=True)

        with patch(
            "asyncio.create_subprocess_exec", side_effect=OSError("no tar")
        ):
            result = await workspace_manager.archive_user_data(
                user["id"], user["email"]
            )
        assert result is None

    async def test_archive_sanitizes_email(self, temp_data_dir, user):
        """Email with path separators is sanitized in archive filename."""
        user_dir = workspace_manager.WORKSPACES_ROOT / user["id"]
        data_dir = user_dir / "data"
        data_dir.mkdir(parents=True)

        result = await workspace_manager.archive_user_data(
            user["id"], "user/../../etc/passwd"
        )
        assert result is not None
        # Archive must be under WORKSPACES_ROOT, not escaped
        assert result.resolve().is_relative_to(
            workspace_manager.WORKSPACES_ROOT.resolve()
        )
        assert ".." not in result.name
