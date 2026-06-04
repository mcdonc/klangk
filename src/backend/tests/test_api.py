"""Tests for api.py: HTTP route handlers via FastAPI TestClient."""

import io
import os
import tempfile
import zipfile

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI, HTTPException
from httpx import AsyncClient, ASGITransport

from klangk_backend import (
    api,
    auth,
    container,
    model,
    workspaces as ws_mod,
    wshandler,
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
        data = resp.json()
        assert "soliplex_url" in data
        assert "login_banner_title" in data
        assert "login_banner" in data

    async def test_get_config_banner_fields(self, client, monkeypatch):
        monkeypatch.setattr(api, "LOGIN_BANNER_TITLE", "Notice")
        monkeypatch.setattr(api, "LOGIN_BANNER", "You must accept terms.")
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["login_banner_title"] == "Notice"
        assert data["login_banner"] == "You must accept terms."


# --- Auth routes ---


class TestAuthRoutes:
    async def test_register(self, client, admin_user):
        login_resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        token = login_resp.json()["access_token"]
        with patch.object(
            api.emailsvc,
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
        monkeypatch.setenv("KLANGK_TEST_MODE", "1")
        resp = await client.post(
            "/auth/register",
            json={"email": "new@example.com", "password": "newpass"},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_register_unauthenticated(self, client, db):
        """Registration is open — no auth required (verification gates access)."""
        with patch.object(
            api.emailsvc,
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
                api.emailsvc,
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
        user = await model.get_user_by_email("fail@example.com")
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
        from klangk_backend import auth as auth_mod

        import bcrypt

        password_hash = bcrypt.hashpw(b"pass", bcrypt.gensalt()).decode()
        user = await model.create_user(
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
        from klangk_backend import auth as auth_mod

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
            api.container.registry,
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
        await model.create_user(
            "unverified@example.com", password_hash, verified=False
        )

    async def test_resend_success(self, client, db):
        await self._create_unverified_user()
        with patch.object(
            api.emailsvc,
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
            api.emailsvc,
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
        return await model.create_user(
            "forgot@example.com", password_hash, verified=True
        )

    async def test_forgot_sends_email(self, client, db):
        await self._create_user()
        with patch.object(
            api.emailsvc,
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
            api.emailsvc,
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
        return await model.create_user(
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
            api.emailsvc,
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
        updated = await model.get_user_by_email("new@example.com")
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
        await model.create_user(
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
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "test-ws"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-ws"
        assert "id" in data

    async def test_create_duplicate(self, client, user):
        headers = await _auth_headers(client)
        await client.post("/workspaces", headers=headers, json={"name": "dup"})
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "dup"}
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    async def test_create_with_disallowed_image(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces",
            headers=headers,
            json={"name": "bad-img", "image": "evil:latest"},
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]

    async def test_create_with_invalid_mount(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces",
            headers=headers,
            json={"name": "bad-mount", "mounts": ["not-valid"]},
        )
        assert resp.status_code == 400
        assert "Invalid mount" in resp.json()["detail"]

    async def test_create_with_valid_mount(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces",
            headers=headers,
            json={"name": "good-mount", "mounts": ["/tmp:/mnt/tmp"]},
        )
        assert resp.status_code == 200

    async def test_list_images(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/images", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "default" in data
        assert "allowed" in data
        assert data["default"] in data["allowed"]

    async def test_delete_workspace(self, client, user):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/workspaces", headers=headers, json={"name": "doomed"}
        )
        ws_id = create_resp.json()["id"]

        with patch.object(
            api.container.registry,
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
            "/workspaces", headers=headers, json={"name": "has-container"}
        )
        ws_id = create_resp.json()["id"]
        # Simulate a running container
        await model.update_workspace_container(ws_id, "fake-container-id")

        with patch.object(
            api.container.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ) as mock_rm:
            resp = await client.delete(f"/workspaces/{ws_id}", headers=headers)
        assert resp.status_code == 200
        mock_rm.assert_awaited_once_with("fake-container-id")

    async def test_list_no_auth(self, client):
        resp = await client.get("/workspaces")
        assert resp.status_code == 401

    async def test_create_with_default_command(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces",
            json={"name": "cmd-ws", "default_command": "pi"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["default_command"] == "pi"

    async def test_update_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces",
            json={"name": "upd-ws"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/workspaces/{ws_id}",
            json={
                "name": "renamed",
                "default_command": "pi",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["name"] == "renamed"
        assert match[0]["default_command"] == "pi"

    async def test_update_workspace_not_found(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.put(
            "/workspaces/nonexistent",
            json={"default_command": "pi"},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_update_workspace_bad_image(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces",
            json={"name": "img-upd"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/workspaces/{ws_id}",
            json={"image": "evil:latest"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]

    async def test_update_workspace_no_fields(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces",
            json={"name": "empty-upd"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/workspaces/{ws_id}",
            json={},
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_update_workspace_invalid_mount(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces",
            json={"name": "mnt-upd"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/workspaces/{ws_id}",
            json={"mounts": ["bad"]},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "Invalid mount" in resp.json()["detail"]

    async def test_duplicate_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces",
            json={
                "name": "src-ws",
                "image": "klangk",
                "default_command": "pi",
                "mounts": ["/tmp:/mnt/tmp"],
                "env": {"FOO": "bar"},
            },
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/workspaces/{ws_id}/duplicate",
            json={"name": "dup-ws"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "dup-ws"
        assert data["image"] == "klangk"
        assert data["default_command"] == "pi"
        assert data["mounts"] == ["/tmp:/mnt/tmp"]
        assert data["env"] == {"FOO": "bar"}
        assert data["id"] != ws_id

    async def test_duplicate_workspace_not_found(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces/nonexistent/duplicate",
            json={"name": "dup"},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_duplicate_workspace_name_conflict(self, client, user):
        headers = await _auth_headers(client)
        await client.post(
            "/workspaces",
            json={"name": "orig"},
            headers=headers,
        )
        ws_id = (
            await client.post(
                "/workspaces",
                json={"name": "taken"},
                headers=headers,
            )
        ).json()["id"]
        resp = await client.post(
            f"/workspaces/{ws_id}/duplicate",
            json={"name": "orig"},
            headers=headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]


# --- Workspace sharing ---


class TestWorkspaceSharingRoutes:
    async def _create_other_user(self):
        password_hash = auth.hash_password("otherpass")
        return await model.create_user(
            "other@example.com", password_hash, verified=True
        )

    async def _other_headers(self, client):
        resp = await client.post(
            "/auth/login",
            json={"email": "other@example.com", "password": "otherpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_list_shared_workspaces(self, client, user):
        headers = await _auth_headers(client)
        await self._create_other_user()
        other_headers = await self._other_headers(client)
        # Create workspace as owner
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "shared-ws"}
        )
        ws_id = resp.json()["id"]
        # Share with other
        await client.post(
            f"/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        # Other user sees it in shared list
        resp = await client.get("/workspaces/shared", headers=other_headers)
        assert resp.status_code == 200
        shared = resp.json()
        assert len(shared) >= 1
        assert any(w["id"] == ws_id for w in shared)
        assert any(w["owner_email"] == "testuser@example.com" for w in shared)

    async def test_get_members_empty(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.get(
            f"/workspaces/{ws_id}/members", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_add_member(self, client, user):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "shared"
        assert resp.json()["user_id"] == other["id"]
        # Verify member is listed
        resp = await client.get(
            f"/workspaces/{ws_id}/members", headers=headers
        )
        assert len(resp.json()) == 1
        assert resp.json()[0]["email"] == "other@example.com"

    async def test_add_member_not_found(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "nobody@example.com"},
        )
        assert resp.status_code == 404
        assert "User not found" in resp.json()["detail"]

    async def test_add_member_self(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "testuser@example.com"},
        )
        assert resp.status_code == 400
        assert "yourself" in resp.json()["detail"]

    async def test_remove_member(self, client, user):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        await client.post(
            f"/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        resp = await client.delete(
            f"/workspaces/{ws_id}/members/{other['id']}", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "removed"
        # Verify member is gone
        resp = await client.get(
            f"/workspaces/{ws_id}/members", headers=headers
        )
        assert resp.json() == []

    async def test_non_owner_cannot_manage_members(self, client, user):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        other_headers = await self._other_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        # Share with other so they can see the workspace
        await model.share_workspace(ws_id, other["id"])
        # Other tries to list members
        resp = await client.get(
            f"/workspaces/{ws_id}/members", headers=other_headers
        )
        assert resp.status_code == 403
        # Other tries to add a member
        resp = await client.post(
            f"/workspaces/{ws_id}/members",
            headers=other_headers,
            json={"email": "testuser@example.com"},
        )
        assert resp.status_code == 403
        # Other tries to remove a member
        resp = await client.delete(
            f"/workspaces/{ws_id}/members/{other['id']}", headers=other_headers
        )
        assert resp.status_code == 403

    async def test_members_workspace_not_found(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/workspaces/nonexistent/members", headers=headers
        )
        assert resp.status_code == 404

    async def test_add_member_workspace_not_found(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces/nonexistent/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        assert resp.status_code == 404

    async def test_remove_member_workspace_not_found(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete(
            "/workspaces/nonexistent/members/some-id", headers=headers
        )
        assert resp.status_code == 404


class TestUserSearch:
    async def test_search_users(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/users/search?q=testuser", headers=headers)
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        assert any(r["email"] == "testuser@example.com" for r in results)

    async def test_search_no_results(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/users/search?q=zzzzz", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_search_requires_auth(self, client, db):
        resp = await client.get("/users/search?q=test")
        assert resp.status_code == 401

    async def test_search_empty_query(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/users/search?q=", headers=headers)
        assert resp.status_code == 400


# --- Messages ---


# --- Browser bridge ---


class TestBrowserBridge:
    async def test_invalid_token_returns_403(self, client, user):
        resp = await client.post(
            "/api/browser-delegate",
            json={"action": "fetch", "token": "bad-token"},
        )
        assert resp.status_code == 403
        assert "Invalid bridge token" in resp.json()["detail"]

    async def test_per_connection_token_success(self, client, user):
        """Per-connection token routes to a specific browser."""
        mock_sock = MagicMock()
        token = container.registry.create_bridge_token(
            "ws-conn", sock=mock_sock
        )
        mock_session = AsyncMock()
        mock_session.browser_subscribers = {mock_sock}
        mock_session.dispatch_browser_request_to = AsyncMock(
            return_value={"status": 200, "body": "targeted"},
        )
        try:
            with patch.object(
                wshandler.state,
                "get_session",
                return_value=mock_session,
            ):
                resp = await client.post(
                    "/api/browser-delegate",
                    json={"action": "fetch", "token": token},
                )
            assert resp.status_code == 200
            assert resp.json()["body"] == "targeted"
            mock_session.dispatch_browser_request_to.assert_awaited_once_with(
                mock_sock, {"action": "fetch"}
            )
        finally:
            container.registry.revoke_bridge_token("ws-conn")

    async def test_per_connection_token_browser_not_subscribed(
        self, client, user
    ):
        """Per-connection token returns 502 when target not in browser_subscribers."""
        mock_sock = MagicMock()
        token = container.registry.create_bridge_token(
            "ws-nosub", sock=mock_sock
        )
        mock_session = AsyncMock()
        mock_session.browser_subscribers = set()  # target not subscribed
        try:
            with patch.object(
                wshandler.state,
                "get_session",
                return_value=mock_session,
            ):
                resp = await client.post(
                    "/api/browser-delegate",
                    json={"action": "fetch", "token": token},
                )
            assert resp.status_code == 502
            assert "Browser connection not available" in resp.json()["detail"]
        finally:
            container.registry.revoke_bridge_token("ws-nosub")

    async def test_valid_token_no_session_returns_502(self, client, user):
        mock_sock = MagicMock()
        token = container.registry.create_bridge_token("ws-nosess", mock_sock)
        try:
            resp = await client.post(
                "/api/browser-delegate",
                json={"action": "fetch", "token": token},
            )
            assert resp.status_code == 502
            assert "No browser client" in resp.json()["detail"]
        finally:
            container.registry.revoke_bridge_token("ws-nosess")

    async def test_dispatch_error_returns_502(self, client, user):
        mock_sock = MagicMock()
        token = container.registry.create_bridge_token("ws-err", mock_sock)
        mock_session = AsyncMock()
        mock_session.browser_subscribers = {mock_sock}
        mock_session.dispatch_browser_request_to = AsyncMock(
            return_value={
                "error": "Browser client did not respond within timeout"
            },
        )
        try:
            with patch.object(
                wshandler.state, "get_session", return_value=mock_session
            ):
                resp = await client.post(
                    "/api/browser-delegate",
                    json={"action": "fetch", "token": token},
                )
            assert resp.status_code == 502
            assert "timeout" in resp.json()["detail"].lower()
        finally:
            container.registry.revoke_bridge_token("ws-err")


# --- Volume routes ---


class TestVolumeRoutes:
    async def test_list_volumes(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            container.registry,
            "get_docker",
            return_value=MagicMock(
                volumes=MagicMock(
                    list=AsyncMock(
                        return_value={
                            "Volumes": [
                                {
                                    "Name": "test-vol",
                                    "CreatedAt": "2026-01-01T00:00:00Z",
                                    "Labels": {
                                        "klangk.instance": container.INSTANCE_ID
                                    },
                                }
                            ]
                        }
                    )
                )
            ),
        ):
            resp = await client.get("/volumes", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "test-vol"

    async def test_create_volume(self, client, user):
        headers = await _auth_headers(client)
        mock_vol = MagicMock()
        mock_vol.show = AsyncMock(
            return_value={"Name": "new-vol", "CreatedAt": "2026-01-01"}
        )
        mock_docker = MagicMock()
        mock_docker.volumes = MagicMock()
        mock_docker.volumes.get = AsyncMock(
            side_effect=container.aiodocker.exceptions.DockerError(
                404, {"message": "not found"}
            )
        )
        mock_docker.volumes.create = AsyncMock(return_value=mock_vol)
        with patch.object(
            container.registry, "get_docker", return_value=mock_docker
        ):
            resp = await client.post(
                "/volumes",
                json={"name": "new-vol"},
                headers=headers,
            )
        assert resp.status_code == 200
        assert resp.json()["name"] == "new-vol"

    async def test_create_duplicate_volume(self, client, user):
        headers = await _auth_headers(client)
        mock_vol = MagicMock()
        mock_vol.show = AsyncMock(return_value={"Name": "dup-vol"})
        mock_docker = MagicMock()
        mock_docker.volumes = MagicMock()
        mock_docker.volumes.get = AsyncMock(return_value=mock_vol)
        with patch.object(
            container.registry, "get_docker", return_value=mock_docker
        ):
            resp = await client.post(
                "/volumes",
                json={"name": "dup-vol"},
                headers=headers,
            )
        assert resp.status_code == 409

    async def test_delete_volume(self, client, user):
        headers = await _auth_headers(client)
        mock_vol = MagicMock()
        mock_vol.show = AsyncMock(
            return_value={
                "Labels": {
                    "klangk.managed": "true",
                    "klangk.instance": container.INSTANCE_ID,
                }
            }
        )
        mock_vol.delete = AsyncMock()
        mock_docker = MagicMock()
        mock_docker.volumes = MagicMock()
        mock_docker.volumes.get = AsyncMock(return_value=mock_vol)
        with patch.object(
            container.registry, "get_docker", return_value=mock_docker
        ):
            resp = await client.delete("/volumes/test-vol", headers=headers)
        assert resp.status_code == 200

    async def test_delete_volume_not_found(self, client, user):
        headers = await _auth_headers(client)
        mock_docker = MagicMock()
        mock_docker.volumes = MagicMock()
        mock_docker.volumes.get = AsyncMock(
            side_effect=container.aiodocker.exceptions.DockerError(
                404, {"message": "not found"}
            )
        )
        with patch.object(
            container.registry, "get_docker", return_value=mock_docker
        ):
            resp = await client.delete("/volumes/nope", headers=headers)
        assert resp.status_code == 404

    async def test_delete_volume_wrong_instance(self, client, user):
        headers = await _auth_headers(client)
        mock_vol = MagicMock()
        mock_vol.show = AsyncMock(
            return_value={"Labels": {"klangk.instance": "other-instance"}}
        )
        mock_docker = MagicMock()
        mock_docker.volumes = MagicMock()
        mock_docker.volumes.get = AsyncMock(return_value=mock_vol)
        with patch.object(
            container.registry, "get_docker", return_value=mock_docker
        ):
            resp = await client.delete("/volumes/foreign", headers=headers)
        assert resp.status_code == 404

    async def test_create_volume_docker_error(self, client, user):
        headers = await _auth_headers(client)
        mock_docker = MagicMock()
        mock_docker.volumes = MagicMock()
        mock_docker.volumes.get = AsyncMock(
            side_effect=container.aiodocker.exceptions.DockerError(
                500, {"message": "internal error"}
            )
        )
        with (
            patch.object(
                container.registry, "get_docker", return_value=mock_docker
            ),
            pytest.raises(container.aiodocker.exceptions.DockerError),
        ):
            await client.post(
                "/volumes",
                json={"name": "err-vol"},
                headers=headers,
            )

    async def test_delete_volume_docker_error(self, client, user):
        headers = await _auth_headers(client)
        mock_vol = MagicMock()
        mock_vol.show = AsyncMock(
            return_value={
                "Labels": {
                    "klangk.managed": "true",
                    "klangk.instance": container.INSTANCE_ID,
                }
            }
        )
        mock_vol.delete = AsyncMock(
            side_effect=container.aiodocker.exceptions.DockerError(
                500, {"message": "internal error"}
            )
        )
        mock_docker = MagicMock()
        mock_docker.volumes = MagicMock()
        mock_docker.volumes.get = AsyncMock(return_value=mock_vol)
        with (
            patch.object(
                container.registry, "get_docker", return_value=mock_docker
            ),
            pytest.raises(container.aiodocker.exceptions.DockerError),
        ):
            await client.delete("/volumes/err-vol", headers=headers)

    async def test_delete_volume_in_use(self, client, user):
        headers = await _auth_headers(client)
        mock_vol = MagicMock()
        mock_vol.show = AsyncMock(
            return_value={
                "Labels": {
                    "klangk.managed": "true",
                    "klangk.instance": container.INSTANCE_ID,
                }
            }
        )
        mock_vol.delete = AsyncMock(
            side_effect=container.aiodocker.exceptions.DockerError(
                409, {"message": "in use"}
            )
        )
        mock_docker = MagicMock()
        mock_docker.volumes = MagicMock()
        mock_docker.volumes.get = AsyncMock(return_value=mock_vol)
        with patch.object(
            container.registry, "get_docker", return_value=mock_docker
        ):
            resp = await client.delete("/volumes/busy", headers=headers)
        assert resp.status_code == 409


# --- File routes ---


class TestFileRoutes:
    async def _create_workspace(self, client, headers):
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "file-ws"}
        )
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
        await model.update_workspace_container(ws_id, cid)
        container.registry.track_activity(cid, ws_id)
        container.registry.states[ws_id].last_activity = 0.0

        await client.post(
            f"/workspaces/{ws_id}/files/upload?path=test.txt",
            headers=headers,
            files={"file": ("test.txt", b"data", "text/plain")},
        )

        assert container.registry.states[ws_id].last_activity > 0.0
        container.registry.states.pop(ws_id, None)
        container.registry._cid_to_wsid.pop(cid, None)

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
            f"/workspaces/{ws_id}/files/rename",
            headers=headers,
            json={"old_path": "old.txt", "new_path": "new.txt"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "renamed"

    async def test_rename_nonexistent(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.post(
            f"/workspaces/{ws_id}/files/rename",
            headers=headers,
            json={"old_path": "nope.txt", "new_path": "new.txt"},
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
            f"/workspaces/{ws_id}/files/rename",
            headers=headers,
            json={"old_path": "a.txt", "new_path": "b.txt"},
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
            "/workspaces/fake-id/files/rename",
            headers=headers,
            json={"old_path": "a", "new_path": "b"},
        )
        assert resp.status_code == 404

    async def test_rename_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        resp = await client.post(
            f"/workspaces/{ws_id}/files/rename",
            headers=headers,
            json={"old_path": "../../etc/passwd", "new_path": "stolen"},
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
        original_timeout = container.IDLE_TIMEOUT_SECONDS
        try:
            container.IDLE_TIMEOUT_SECONDS = 42
            assert container.IDLE_TIMEOUT_SECONDS == 42
            # Per-workspace lookup falls back to global
            assert container.registry.get_workspace_idle_timeout("any") == 42
        finally:
            container.IDLE_TIMEOUT_SECONDS = original_timeout

    async def test_endpoint_missing_without_test_mode(self, client):
        """Without KLANGK_TEST_MODE, the endpoints should not exist."""
        resp = await client.post(
            "/api/test/set-idle-timeout", json={"seconds": 10}
        )
        assert resp.status_code in (404, 405)
        resp = await client.get("/api/test/idle-timeout")
        assert resp.status_code in (404, 405)

    async def test_set_idle_timeout_per_workspace(self, db):
        """Per-workspace idle timeout should not affect global."""
        original_timeout = container.IDLE_TIMEOUT_SECONDS
        try:
            container.registry.track_activity("cid-test", "ws-test")
            container.registry.set_workspace_idle_timeout("ws-test", 5)
            assert (
                container.registry.get_workspace_idle_timeout("ws-test") == 5
            )
            assert container.IDLE_TIMEOUT_SECONDS == original_timeout
            # Unknown workspace returns global default
            assert (
                container.registry.get_workspace_idle_timeout("ws-other")
                == original_timeout
            )
        finally:
            container.registry.states.pop("ws-test", None)

    async def test_cleanup_loop_adapts_to_short_timeout(self, db):
        """Cleanup loop interval adapts when per-workspace timeouts exist."""
        try:
            container.registry.track_activity("cid-fast", "ws-fast")
            container.registry.set_workspace_idle_timeout("ws-fast", 6)
            # With a 6s per-workspace timeout, the minimum is 6, so
            # the loop should sleep max(2, 6//2) = 3 seconds.
            state = container.registry.states["ws-fast"]
            assert state.idle_timeout == 6
            # Global CHECK_INTERVAL_SECONDS should be unchanged
            assert (
                container.CHECK_INTERVAL_SECONDS
                == container.parse_idle_timeout()[1]
            )
        finally:
            container.registry.states.pop("ws-fast", None)


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
        await model.ensure_role("editor")
        # Idempotent
        await model.ensure_role("editor")

    async def test_assign_and_get_roles(self, user):
        await model.ensure_role("admin")
        await model.ensure_role("editor")
        await model.assign_role(user["id"], "admin")
        await model.assign_role(user["id"], "editor")
        roles = await model.get_user_roles(user["id"])
        assert set(roles) == {"admin", "editor"}

    async def test_assign_role_idempotent(self, user):
        await model.ensure_role("admin")
        await model.assign_role(user["id"], "admin")
        await model.assign_role(user["id"], "admin")
        roles = await model.get_user_roles(user["id"])
        assert roles == ["admin"]

    async def test_get_roles_empty(self, user):
        roles = await model.get_user_roles(user["id"])
        assert roles == []

    async def test_roles_in_jwt(self, user):
        await model.ensure_role("admin")
        await model.assign_role(user["id"], "admin")
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
        user = await model.create_user("delme", "hash")
        await model.ensure_role("admin")
        await model.assign_role(user["id"], "admin")
        assert await model.get_user_roles(user["id"]) == ["admin"]
        db_conn = await model.get_db()
        try:
            await db_conn.execute(
                "DELETE FROM users WHERE id = ?", (user["id"],)
            )
            await db_conn.commit()
        finally:
            await db_conn.close()
        assert await model.get_user_roles(user["id"]) == []

    async def test_cascade_delete_role(self, user):
        """Deleting a role cascades to user_roles."""
        await model.ensure_role("temp")
        await model.assign_role(user["id"], "temp")
        assert "temp" in await model.get_user_roles(user["id"])
        db_conn = await model.get_db()
        try:
            await db_conn.execute(
                "DELETE FROM roles WHERE name = ?", ("temp",)
            )
            await db_conn.commit()
        finally:
            await db_conn.close()
        assert "temp" not in await model.get_user_roles(user["id"])


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

    async def test_admin_create_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/users",
            headers=headers,
            json={"email": "newuser@example.com", "password": "testpass123"},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "newuser@example.com"
        assert resp.json()["status"] == "created"
        # User should be verified and able to log in
        login_resp = await client.post(
            "/auth/login",
            json={"email": "newuser@example.com", "password": "testpass123"},
        )
        assert login_resp.status_code == 200

    async def test_admin_create_user_duplicate(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/users",
            headers=headers,
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 400
        assert "already registered" in resp.json()["detail"]

    async def test_admin_create_user_short_password(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/users",
            headers=headers,
            json={"email": "short@example.com", "password": "ab"},
        )
        assert resp.status_code == 400
        assert "Password" in resp.json()["detail"]

    async def test_admin_create_user_requires_admin(self, client, user):
        login_resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.post(
            "/admin/users",
            headers=headers,
            json={"email": "new@example.com", "password": "testpass123"},
        )
        assert resp.status_code == 403

    async def test_delete_user(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        with (
            patch.object(
                container.registry,
                "stop_user_containers",
                new_callable=AsyncMock,
            ),
            patch.object(ws_mod, "archive_user_data", new_callable=AsyncMock),
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
        """Deleting a user cascades to their ws_mod."""
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
            "/workspaces", headers=user_headers, json={"name": "to-delete"}
        )
        assert ws_resp.status_code == 200
        # Delete the user
        with patch.object(
            container.registry,
            "stop_user_containers",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(
                f"/admin/users/{user['id']}", headers=headers
            )
        assert resp.status_code == 200
        # Workspace should be gone (CASCADE)
        ws_list = await model.get_user_workspaces_with_containers(user["id"])
        assert len(ws_list) == 0

    async def test_add_role(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            f"/admin/users/{user['id']}/roles/editor", headers=headers
        )
        assert resp.status_code == 200
        roles = await model.get_user_roles(user["id"])
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
        await model.ensure_role("editor")
        await model.assign_role(user["id"], "editor")
        # Then remove it
        resp = await client.delete(
            f"/admin/users/{user['id']}/roles/editor", headers=headers
        )
        assert resp.status_code == 200
        roles = await model.get_user_roles(user["id"])
        assert "editor" not in roles

    async def test_update_email(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/admin/users/{user['id']}",
            json={"email": "renamed"},
            headers=headers,
        )
        assert resp.status_code == 200
        updated = await model.get_user_by_id(user["id"])
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
        user_dir = ws_mod.WORKSPACES_ROOT / user["id"]
        data_dir = user_dir / "data" / "ws1"
        data_dir.mkdir(parents=True)
        (data_dir / "hello.txt").write_text("test content")

        result = await ws_mod.archive_user_data(user["id"], user["email"])
        assert result is not None
        assert result.exists()
        assert result.name == f"{user['id']}-{user['email']}.tar.xz"
        # Original directory should be removed
        assert not user_dir.exists()

    async def test_archive_no_data_dir(self, temp_data_dir, user):
        """Returns None if user has no data directory."""
        result = await ws_mod.archive_user_data(user["id"], user["email"])
        assert result is None

    async def test_archive_tar_nonzero_exit(self, temp_data_dir, user):
        """Returns None if tar exits with non-zero status."""
        user_dir = ws_mod.WORKSPACES_ROOT / user["id"]
        user_dir.mkdir(parents=True)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"tar: error"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await ws_mod.archive_user_data(user["id"], user["email"])
        assert result is None

    async def test_archive_tar_oserror(self, temp_data_dir, user):
        """Returns None if tar fails to start."""
        user_dir = ws_mod.WORKSPACES_ROOT / user["id"]
        user_dir.mkdir(parents=True)

        with patch(
            "asyncio.create_subprocess_exec", side_effect=OSError("no tar")
        ):
            result = await ws_mod.archive_user_data(user["id"], user["email"])
        assert result is None

    async def test_archive_sanitizes_email(self, temp_data_dir, user):
        """Email with path separators is sanitized in archive filename."""
        user_dir = ws_mod.WORKSPACES_ROOT / user["id"]
        data_dir = user_dir / "data"
        data_dir.mkdir(parents=True)

        result = await ws_mod.archive_user_data(
            user["id"], "user/../../etc/passwd"
        )
        assert result is not None
        # Archive must be under WORKSPACES_ROOT, not escaped
        assert result.resolve().is_relative_to(
            ws_mod.WORKSPACES_ROOT.resolve()
        )
        assert ".." not in result.name


# --- Workspace Export/Import ---


class TestWorkspaceExportImport:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def _user_headers(self, client):
        resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_export_workspace(self, client, admin_user, user):
        # Create a workspace as regular user
        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "export-test"}
        )
        assert resp.status_code == 200
        ws = resp.json()

        # Write a file into the workspace home dir
        import klangk_backend.workspaces as ws_mod

        home = ws_mod.home_path(user["id"], ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "work").mkdir(exist_ok=True)
        (home / "work" / "hello.txt").write_text("hello world")

        # Export as admin
        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/gzip"
        assert "export-test.tar.gz" in resp.headers["content-disposition"]

        # Verify the archive contents
        import io
        import json
        import tarfile

        buf = io.BytesIO(resp.content)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            names = tar.getnames()
            assert "workspace.json" in names
            assert any("home" in n for n in names)

            meta_file = tar.extractfile("workspace.json")
            metadata = json.loads(meta_file.read())
            assert metadata["name"] == "export-test"

    async def test_export_requires_admin(self, client, user):
        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "no-export"}
        )
        ws = resp.json()

        # Non-admin cannot export
        resp = await client.get(
            f"/workspaces/{ws['id']}/export", headers=headers
        )
        assert resp.status_code == 403

    async def test_export_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/workspaces/nonexistent-id/export", headers=headers
        )
        assert resp.status_code == 404

    async def test_import_workspace(self, client, admin_user, user):
        # Create and export a workspace
        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces",
            headers=headers,
            json={
                "name": "import-source",
                "default_command": "pi",
                "env": {"FOO": "bar"},
            },
        )
        ws = resp.json()

        import klangk_backend.workspaces as ws_mod

        home = ws_mod.home_path(user["id"], ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "work").mkdir(exist_ok=True)
        (home / "work" / "data.txt").write_text("test data")

        admin_headers = await self._admin_headers(client)
        export_resp = await client.get(
            f"/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert export_resp.status_code == 200

        # Import as regular user with a new name
        import_resp = await client.post(
            "/workspaces/import",
            headers=headers,
            params={"name": "imported-ws"},
            files={
                "file": (
                    "archive.tar.gz",
                    export_resp.content,
                    "application/gzip",
                )
            },
        )
        assert import_resp.status_code == 200
        imported = import_resp.json()
        assert imported["name"] == "imported-ws"

        # Verify the home dir was extracted
        new_home = ws_mod.home_path(user["id"], imported["id"])
        assert (new_home / "work" / "data.txt").exists()
        assert (new_home / "work" / "data.txt").read_text() == "test data"

    async def test_import_uses_archive_name(self, client, admin_user, user):
        # Build a minimal archive with workspace.json
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({"name": "from-archive"}).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "from-archive"

    async def test_import_duplicate_name(self, client, user):
        headers = await self._user_headers(client)
        await client.post(
            "/workspaces", headers=headers, json={"name": "taken"}
        )

        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({"name": "taken"}).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 409

    async def test_import_missing_metadata(self, client, user):
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"just some data"
            info = tarfile.TarInfo(name="random.txt")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400
        assert "workspace.json" in resp.json()["detail"]

    async def test_import_invalid_archive(self, client, user):
        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("bad.tar.gz", b"not a tarball", "application/gzip")
            },
        )
        assert resp.status_code == 400

    async def test_import_no_name(self, client, user):
        """Archive has no name and no name param → error."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({}).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400
        assert "No workspace name" in resp.json()["detail"]

    async def test_import_disallowed_image_falls_back(self, client, user):
        """Archive with disallowed image falls back to default."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                {"name": "img-fallback", "image": "evil:latest"}
            ).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "img-fallback"

    async def test_import_invalid_mounts_dropped(self, client, user):
        """Archive with invalid mounts drops them silently."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                {"name": "mount-drop", "mounts": ["bad-mount-spec"]}
            ).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "mount-drop"

    async def test_import_home_root_member_skipped(self, client, user):
        """The bare 'home/' directory entry is skipped during extraction."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({"name": "home-root-skip"}).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))

            # Add a "home/" directory entry (empty name after stripping)
            dir_info = tarfile.TarInfo(name="home/")
            dir_info.type = tarfile.DIRTYPE
            tar.addfile(dir_info)

            # Add a real file under home/
            data = b"content"
            file_info = tarfile.TarInfo(name="home/test.txt")
            file_info.size = len(data)
            tar.addfile(file_info, io.BytesIO(data))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200

        import klangk_backend.workspaces as ws_mod

        ws = resp.json()
        home = ws_mod.home_path(user["id"], ws["id"])
        assert (home / "test.txt").exists()

    async def test_export_streams_valid_tarball(
        self, client, admin_user, user
    ):
        """Export streams a valid .tar.gz with size estimate header."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "stream-test"}
        )
        ws = resp.json()

        import klangk_backend.workspaces as ws_mod

        home = ws_mod.home_path(user["id"], ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "work").mkdir(exist_ok=True)
        (home / "work" / "file.txt").write_text("streamed content")

        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/gzip"
        assert "x-estimated-size" in resp.headers
        assert int(resp.headers["x-estimated-size"]) > 0

        # Verify the streamed response is a valid tarball
        import tarfile

        buf = io.BytesIO(resp.content)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            names = tar.getnames()
            assert "workspace.json" in names
            assert any("file.txt" in n for n in names)

    async def test_export_large_file_chunks(self, client, admin_user, user):
        """Export with large files triggers the write buffer flush path."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "large-export"}
        )
        ws = resp.json()

        import klangk_backend.workspaces as ws_mod

        home = ws_mod.home_path(user["id"], ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "work").mkdir(exist_ok=True)
        # Write a large file with random data (incompressible, so gzip
        # passes it through in large writes that trigger buffer flushes)
        import random

        rng = random.Random(42)
        (home / "work" / "big.bin").write_bytes(
            bytes(rng.getrandbits(8) for _ in range(512 * 1024))
        )

        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 200

        import tarfile

        buf = io.BytesIO(resp.content)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            assert any("big.bin" in n for n in tar.getnames())

    async def test_export_du_failure_falls_back(
        self, client, admin_user, user, monkeypatch
    ):
        """If du fails, estimated size defaults to minimum."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "du-fail"}
        )
        ws = resp.json()

        import klangk_backend.workspaces as ws_mod

        home = ws_mod.home_path(user["id"], ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "work").mkdir(exist_ok=True)
        (home / "work" / "f.txt").write_text("data")

        import subprocess as subprocess_mod

        original_run = subprocess_mod.run

        def _failing_run(*args, **kwargs):
            if args and args[0] and args[0][0] == "du":
                raise OSError("du not found")
            return original_run(*args, **kwargs)

        monkeypatch.setattr(subprocess_mod, "run", _failing_run)

        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 200
        # Falls back to 0 * 0.4 = 0, clamped to 1
        assert resp.headers["x-estimated-size"] == "1"

    async def test_export_empty_workspace(self, client, admin_user, user):
        """Export of workspace with no home dir still works."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "empty-export"}
        )
        ws = resp.json()

        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 200
        # Estimated size is 0 * 0.4 = 0, clamped to 1
        assert resp.headers["x-estimated-size"] == "1"

    async def test_import_upload_error_cleans_tempfile(
        self, client, user, monkeypatch
    ):
        """If the upload write fails, the temp file is cleaned up."""
        import klangk_backend.api as api_mod

        headers = await self._user_headers(client)

        created_tmp = []
        original_ntf = tempfile.NamedTemporaryFile

        def _failing_ntf(*args, **kwargs):
            tmp = original_ntf(*args, **kwargs)
            created_tmp.append(tmp.name)

            def _bad_write(data):
                raise IOError("disk full")

            tmp.write = _bad_write
            return tmp

        monkeypatch.setattr(
            api_mod.tempfile, "NamedTemporaryFile", _failing_ntf
        )

        with pytest.raises(IOError, match="disk full"):
            await client.post(
                "/workspaces/import",
                headers=headers,
                files={
                    "file": (
                        "test.tar.gz",
                        b"some data",
                        "application/gzip",
                    )
                },
            )

        assert len(created_tmp) == 1
        assert not os.path.exists(created_tmp[0])

    async def test_export_strips_external_symlinks(
        self, client, admin_user, user
    ):
        """Symlinks pointing outside the home dir are excluded from export."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "symlink-export"}
        )
        ws = resp.json()

        import klangk_backend.workspaces as ws_mod

        home = ws_mod.home_path(user["id"], ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "work").mkdir(exist_ok=True)
        (home / "work" / "real.txt").write_text("real file")
        # Internal symlink (should be kept)
        (home / "work" / "internal_link").symlink_to("real.txt")
        # External symlink (should be stripped)
        (home / "work" / "external_link").symlink_to("/etc/passwd")

        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 200

        import tarfile

        buf = io.BytesIO(resp.content)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            names = tar.getnames()
            assert any("real.txt" in n for n in names)
            assert any("internal_link" in n for n in names)
            assert not any("external_link" in n for n in names)

    async def test_import_size_limit(self, client, user, monkeypatch):
        """Upload exceeding size limit is rejected."""
        import klangk_backend.api as api_mod

        monkeypatch.setattr(api_mod, "IMPORT_MAX_SIZE", 100)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={"file": ("big.tar.gz", b"x" * 200, "application/gzip")},
        )
        assert resp.status_code == 413

    async def test_import_sanitizes_env(self, client, user):
        """Dangerous env vars from archive are stripped."""
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                {
                    "name": "env-sanitize",
                    "env": {
                        "MY_VAR": "safe",
                        "KLANGK_BRIDGE_TOKEN": "stolen",
                        "LD_PRELOAD": "/evil.so",
                        "PATH": "/bad",
                        "NORMAL_VAR": "ok",
                    },
                }
            ).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200
        ws = resp.json()

        # Fetch the workspace to check env
        resp = await client.get("/workspaces", headers=headers)
        workspaces_list = resp.json()
        imported = next(w for w in workspaces_list if w["id"] == ws["id"])
        env = imported.get("env", {})
        assert "MY_VAR" in env
        assert "NORMAL_VAR" in env
        assert "KLANGK_BRIDGE_TOKEN" not in env
        assert "LD_PRELOAD" not in env
        assert "PATH" not in env

    async def test_import_cleanup_on_extraction_failure(
        self, client, user, monkeypatch
    ):
        """If tar extraction fails, the workspace is cleaned up."""
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({"name": "fail-extract"}).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))

            data = b"content"
            file_info = tarfile.TarInfo(name="home/test.txt")
            file_info.size = len(data)
            tar.addfile(file_info, io.BytesIO(data))
        buf.seek(0)

        import subprocess as subprocess_mod

        original_run = subprocess_mod.run
        call_count = [0]

        def _failing_run(args, **kwargs):
            call_count[0] += 1
            # Let the first calls (tar xzf -O workspace.json, tar tzf home/)
            # succeed, but fail on the extraction call (tar xzf ... -C ...)
            if "-C" in args:
                return subprocess_mod.CompletedProcess(
                    args=args, returncode=1, stdout=b"", stderr=b"failed"
                )
            return original_run(args, **kwargs)

        monkeypatch.setattr(subprocess_mod, "run", _failing_run)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400

        # Workspace should have been cleaned up
        resp = await client.get("/workspaces", headers=headers)
        names = [w["name"] for w in resp.json()]
        assert "fail-extract" not in names

    async def test_import_invalid_json_in_metadata(self, client, user):
        """If workspace.json contains invalid JSON, import fails cleanly."""
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            bad_json = b"not valid json {"
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(bad_json)
            tar.addfile(info, io.BytesIO(bad_json))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400
        assert "corrupt" in resp.json()["detail"].lower()

    async def test_import_timeout_cleans_up_workspace(
        self, client, user, monkeypatch
    ):
        """If tar extraction times out after workspace creation, cleanup occurs."""
        import json
        import tarfile
        import subprocess as subprocess_mod

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({"name": "timeout-test"}).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))

            data = b"content"
            file_info = tarfile.TarInfo(name="home/test.txt")
            file_info.size = len(data)
            tar.addfile(file_info, io.BytesIO(data))
        buf.seek(0)

        original_run = subprocess_mod.run

        def _timeout_run(args, **kwargs):
            if "-C" in args:
                raise subprocess_mod.TimeoutExpired(args, 300)
            return original_run(args, **kwargs)

        monkeypatch.setattr(subprocess_mod, "run", _timeout_run)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400

        resp = await client.get("/workspaces", headers=headers)
        names = [w["name"] for w in resp.json()]
        assert "timeout-test" not in names

    async def test_import_path_traversal_rejected(self, client, user):
        """GNU tar rejects members with '..' in their path."""
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({"name": "traversal-test"}).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))

            evil = b"pwned"
            info = tarfile.TarInfo(name="home/../../../etc/passwd")
            info.size = len(evil)
            tar.addfile(info, io.BytesIO(evil))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        # GNU tar refuses to extract members with '..' — returns non-zero
        assert resp.status_code == 400

        # Workspace should have been cleaned up
        resp = await client.get("/workspaces", headers=headers)
        names = [w["name"] for w in resp.json()]
        assert "traversal-test" not in names
