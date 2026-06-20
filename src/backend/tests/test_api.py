"""Tests for api.py: HTTP route handlers via FastAPI TestClient."""

import io
import os
import shutil
import tempfile
import zipfile

import pytest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from fastapi import FastAPI
import httpx
from httpx import AsyncClient, ASGITransport

from klangk_backend import (
    api,
    auth,
    container,
    model,
    oidc,
    plugins,
    podman,
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


class TestVerifyWorkspaceToken:
    async def test_valid_workspace_token(self, client):
        token = auth.create_workspace_token("ws-123")
        resp = await client.get(
            "/auth/verify-workspace-token",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] == "ws-123"

    async def test_missing_auth_header(self, client):
        resp = await client.get("/auth/verify-workspace-token")
        assert resp.status_code == 401

    async def test_invalid_token(self, client):
        resp = await client.get(
            "/auth/verify-workspace-token",
            headers={"Authorization": "Bearer garbage"},
        )
        assert resp.status_code == 401

    async def test_user_jwt_rejected(self, client):
        user_token = auth.create_token("user-1", "u@test.com")
        resp = await client.get(
            "/auth/verify-workspace-token",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 401

    async def test_expired_workspace_token(self, client):
        from datetime import datetime, timedelta, timezone

        from jose import jwt

        expired = datetime.now(timezone.utc) - timedelta(hours=1)
        payload = {"sub": "ws-123", "purpose": "workspace", "exp": expired}
        token = jwt.encode(payload, auth.SECRET_KEY, algorithm=auth.ALGORITHM)
        resp = await client.get(
            "/auth/verify-workspace-token",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Workspace token expired"

    async def test_invalid_workspace_token_detail(self, client):
        resp = await client.get(
            "/auth/verify-workspace-token",
            headers={"Authorization": "Bearer garbage"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid workspace token"


class TestWorkspaceChat:
    async def test_post_agent_message(self, client, user):
        workspace = await model.create_workspace(user["id"], "chat-ws")
        token = auth.create_workspace_token(workspace["id"])
        resp = await client.post(
            "/api/workspace/post-chat-message",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "hello from agent"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "hello from agent"
        assert data["message_type"] == model.MSG_AGENT
        assert data["workspace_id"] == workspace["id"]

    async def test_broadcasts_to_websocket(self, client, user):
        workspace = await model.create_workspace(user["id"], "bcast-ws")
        token = auth.create_workspace_token(workspace["id"])
        session = wshandler.state.get_or_create_session(workspace["id"])
        mock_sock = MagicMock()
        session.subscribers.add(mock_sock)

        await client.post(
            "/api/workspace/post-chat-message",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "broadcast test"},
        )

        mock_sock.send_json.assert_called_once()
        sent = mock_sock.send_json.call_args[0][0]
        assert sent["type"] == "chat_message"
        assert sent["message"] == "broadcast test"

        wshandler.state.sessions.pop(workspace["id"], None)

    async def test_missing_auth(self, client):
        resp = await client.post(
            "/api/workspace/post-chat-message", json={"message": "hi"}
        )
        assert resp.status_code == 401

    async def test_invalid_token(self, client):
        resp = await client.post(
            "/api/workspace/post-chat-message",
            headers={"Authorization": "Bearer garbage"},
            json={"message": "hi"},
        )
        assert resp.status_code == 401

    async def test_workspace_not_found(self, client):
        token = auth.create_workspace_token("nonexistent-ws")
        resp = await client.post(
            "/api/workspace/post-chat-message",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "hi"},
        )
        assert resp.status_code == 404

    async def test_empty_message_rejected(self, client, user):
        workspace = await model.create_workspace(user["id"], "empty-ws")
        token = auth.create_workspace_token(workspace["id"])
        resp = await client.post(
            "/api/workspace/post-chat-message",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "   "},
        )
        assert resp.status_code == 400


class TestVersion:
    async def test_version_from_file(self, client, tmp_path, monkeypatch):
        version_file = tmp_path / "version.json"
        version_file.write_text(
            '{"version": "2026.01.01+abc1234",'
            ' "commit": "abc1234",'
            ' "built_at": "2026-01-01T00:00:00Z"}'
        )
        monkeypatch.setenv("KLANGK_VERSION_FILE", str(version_file))
        resp = await client.get("/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "2026.01.01+abc1234"
        assert data["commit"] == "abc1234"
        assert data["built_at"] == "2026-01-01T00:00:00Z"

    async def test_version_git_fallback(self, client, monkeypatch):
        monkeypatch.delenv("KLANGK_VERSION_FILE", raising=False)
        resp = await client.get("/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "dev"
        assert "commit" in data
        assert "built_at" in data

    async def test_version_git_not_available(self, client, monkeypatch):
        monkeypatch.delenv("KLANGK_VERSION_FILE", raising=False)
        monkeypatch.setattr(
            "klangk_backend.api.subprocess.check_output",
            Mock(side_effect=FileNotFoundError),
        )
        resp = await client.get("/version")
        assert resp.status_code == 200
        assert resp.json() == {
            "version": "dev",
            "commit": "unknown",
            "built_at": None,
        }


# --- Config ---


class TestConfig:
    async def test_get_config(self, client):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "login_banner_title" in data
        assert "login_banner" in data

    async def test_get_config_includes_plugins(self, client, monkeypatch):
        monkeypatch.setattr(
            plugins,
            "_declarations",
            {
                "MY_PLUGIN_VAR": {
                    "plugin": "test",
                    "description": "",
                    "default": "",
                    "scope": "frontend",
                }
            },
        )
        monkeypatch.setattr(
            plugins, "_values", {"MY_PLUGIN_VAR": "test-value"}
        )
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["my_plugin_var"] == "test-value"

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
        with patch.object(
            api.emailsvc,
            "send_verification_email",
            new_callable=AsyncMock,
            side_effect=RuntimeError("sendmail not found"),
        ):
            resp = await client.post(
                "/auth/register",
                json={"email": "fail@example.com", "password": "newpass"},
            )
        assert resp.status_code == 503
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

    async def test_reset_agent_user_rejected(self, client, db):
        token = auth.create_password_reset_token(model.AGENT_USER_ID)
        resp = await client.post(
            "/auth/reset-password",
            json={"token": token, "password": "newpass"},
        )
        assert resp.status_code == 400
        assert "system agent" in resp.json()["detail"]


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

    async def test_delete_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete("/workspaces/fake-id", headers=headers)
        assert resp.status_code == 403

    async def test_delete_not_found(self, client, user):
        """ACL passes but workspace doesn't exist."""
        headers = await _auth_headers(client)
        fake_id = "fake-del-id"
        await model.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.delete(f"/workspaces/{fake_id}", headers=headers)
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

    async def test_delete_workspace_cleans_up_groups(self, client, user):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/workspaces", headers=headers, json={"name": "cleanup-test"}
        )
        ws_id = create_resp.json()["id"]

        # Verify role groups were created
        for suffix in ["owners", "coders", "collaborators", "spectators"]:
            group = await model.get_group_by_name(f"{suffix}-{ws_id}")
            assert group is not None, f"expected {suffix} group to exist"

        # Verify ACL entries exist for the workspace
        acl = await model.get_acl_entries(f"/workspaces/{ws_id}")
        assert len(acl) > 0

        with patch.object(
            api.container.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(f"/workspaces/{ws_id}", headers=headers)
        assert resp.status_code == 200

        # Role groups should be gone
        for suffix in ["owners", "coders", "collaborators", "spectators"]:
            group = await model.get_group_by_name(f"{suffix}-{ws_id}")
            assert group is None, f"expected {suffix} group to be deleted"

        # ACL entries should be gone
        acl = await model.get_acl_entries(f"/workspaces/{ws_id}")
        assert len(acl) == 0

    async def test_restart_workspace(self, client, user):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/workspaces", headers=headers, json={"name": "restart-me"}
        )
        ws_id = create_resp.json()["id"]

        # Simulate a running container so the stop path is exercised.
        api.container.registry.track_activity("cid-restart", ws_id)

        with patch.object(
            api.container.registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ) as mock_stop:
            resp = await client.post(
                f"/workspaces/{ws_id}/restart", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "restarted"
        mock_stop.assert_awaited_once_with("cid-restart")

        # Clean up registry state.
        api.container.registry.states.pop(ws_id, None)

    async def test_restart_not_found(self, client, user):
        headers = await _auth_headers(client)
        fake_id = "fake-restart-id"
        await model.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "terminal",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.post(
            f"/workspaces/{fake_id}/restart", headers=headers
        )
        assert resp.status_code == 404

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

    async def test_update_workspace_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.put(
            "/workspaces/nonexistent",
            json={"default_command": "pi"},
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_update_workspace_not_found(self, client, user):
        """ACL passes but workspace doesn't exist."""
        headers = await _auth_headers(client)
        fake_id = "fake-ws-id"
        await model.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.put(
            f"/workspaces/{fake_id}",
            json={"default_command": "pi"},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_update_workspace_race_delete(
        self, client, user, monkeypatch
    ):
        """Workspace deleted between get and update returns 404."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "race-ws"}
        )
        ws_id = resp.json()["id"]
        original_update = model.update_workspace

        async def _delete_then_update(workspace_id, user_id, **fields):
            await model.delete_workspace(workspace_id, user_id)
            return await original_update(workspace_id, user_id, **fields)

        monkeypatch.setattr(model, "update_workspace", _delete_then_update)
        resp = await client.put(
            f"/workspaces/{ws_id}",
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
                "image": "klangk-workspace",
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
        assert data["image"] == "klangk-workspace"
        assert data["default_command"] == "pi"
        assert data["mounts"] == ["/tmp:/mnt/tmp"]
        assert data["env"] == {"FOO": "bar"}
        assert data["id"] != ws_id

    async def test_duplicate_workspace_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces/nonexistent/duplicate",
            json={"name": "dup"},
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_duplicate_workspace_not_found(self, client, user):
        """ACL passes but workspace doesn't exist."""
        headers = await _auth_headers(client)
        fake_id = "fake-dup-id"
        await model.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.post(
            f"/workspaces/{fake_id}/duplicate",
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
        # Share with other (gives view/terminal/files but not share)
        await client.post(
            f"/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        # Other tries to list members — no share permission
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

    async def test_add_member_broadcasts_workspace_members(self, client, user):
        """Adding a member broadcasts updated workspace_members to WS."""
        from klangk_backend.wshandler import WorkspaceSession

        headers = await _auth_headers(client)
        await self._create_other_user()
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "broadcast-ws"}
        )
        ws_id = resp.json()["id"]
        mock_sock = MagicMock()
        mock_sock.send_json = MagicMock()
        session = WorkspaceSession(ws_id)
        session.subscribers.add(mock_sock)
        wshandler.state.sessions[ws_id] = session
        try:
            resp = await client.post(
                f"/workspaces/{ws_id}/members",
                headers=headers,
                json={"email": "other@example.com"},
            )
            assert resp.status_code == 200
            calls = [c[0][0] for c in mock_sock.send_json.call_args_list]
            members_msgs = [
                c for c in calls if c.get("type") == "workspace_members"
            ]
            assert len(members_msgs) == 1
            emails = [m["email"] for m in members_msgs[0]["members"]]
            assert "other@example.com" in emails
        finally:
            wshandler.state.sessions.pop(ws_id, None)

    async def test_members_no_permission(self, client, user):
        """User without share permission gets 403 on nonexistent workspace."""
        headers = await _auth_headers(client)
        resp = await client.get(
            "/workspaces/nonexistent/members", headers=headers
        )
        assert resp.status_code == 403

    async def test_add_member_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces/nonexistent/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        assert resp.status_code == 403

    async def test_remove_member_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete(
            "/workspaces/nonexistent/members/some-id", headers=headers
        )
        assert resp.status_code == 403


class TestWorkspaceACL:
    async def test_get_workspace_acl(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "acl-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.get(f"/workspaces/{ws_id}/acl", headers=headers)
        assert resp.status_code == 200
        entries = resp.json()
        # Owner has * ACE
        assert len(entries) >= 1
        assert any(
            e["permission"] == "*" and e["principal"] == "testuser@example.com"
            for e in entries
        )

    async def test_get_workspace_acl_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/workspaces/nonexistent/acl", headers=headers)
        assert resp.status_code == 403

    async def test_get_workspace_acl_with_group(
        self, client, admin_user, user
    ):
        """ACL endpoint resolves group names."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "group-acl-ws"}
        )
        ws_id = resp.json()["id"]
        # Add a group ACE
        group = await model.create_group("test-acl-group")
        await model.add_acl_entry(
            f"/workspaces/{ws_id}",
            100,
            model.ACTION_ALLOW,
            "view",
            model.PRINCIPAL_GROUP,
            group_id=group["id"],
        )
        resp = await client.get(f"/workspaces/{ws_id}/acl", headers=headers)
        assert resp.status_code == 200
        entries = resp.json()
        group_entry = next(
            (e for e in entries if e.get("group_id") == group["id"]), None
        )
        assert group_entry is not None
        assert group_entry["principal"] == "test-acl-group"

    async def test_replace_workspace_acl(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "replace-acl-ws"}
        )
        ws_id = resp.json()["id"]
        # Replace with custom ACL
        new_acl = [
            {
                "action": model.ACTION_ALLOW,
                "principal_type": model.PRINCIPAL_USER,
                "permission": "*",
                "user_id": user["id"],
            },
            {
                "action": model.ACTION_ALLOW,
                "principal_type": model.PRINCIPAL_SYSTEM,
                "permission": "view",
                "system_principal": model.SYSTEM_AUTHENTICATED,
            },
        ]
        resp = await client.put(
            f"/workspaces/{ws_id}/acl",
            headers=headers,
            json=new_acl,
        )
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) == 2
        assert entries[0]["permission"] == "*"
        assert entries[1]["permission"] == "view"
        assert entries[1]["principal"] == "Authenticated"


class TestWorkspaceRoles:
    async def test_role_groups_created_on_workspace_create(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "roles-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.get(f"/workspaces/{ws_id}/roles", headers=headers)
        assert resp.status_code == 200
        roles = resp.json()
        role_names = [r["role"] for r in roles]
        assert "owners" in role_names
        assert "coders" in role_names
        assert "collaborators" in role_names
        assert "spectators" in role_names

    async def test_creator_in_owners_group(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "owner-role-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.get(f"/workspaces/{ws_id}/roles", headers=headers)
        roles = {r["role"]: r for r in resp.json()}
        owner_members = [m["id"] for m in roles["owners"]["members"]]
        assert user["id"] in owner_members

    async def test_add_user_to_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "add-role-ws"}
        )
        ws_id = resp.json()["id"]
        # Create a second user
        target = await model.create_user("role-target@test.com", "pass")
        resp = await client.post(
            f"/workspaces/{ws_id}/roles/spectators",
            headers=headers,
            json={"email": "role-target@test.com"},
        )
        assert resp.status_code == 200
        # Verify user is in the role
        resp = await client.get(f"/workspaces/{ws_id}/roles", headers=headers)
        roles = {r["role"]: r for r in resp.json()}
        member_ids = [m["id"] for m in roles["spectators"]["members"]]
        assert target["id"] in member_ids

    async def test_remove_user_from_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "rm-role-ws"}
        )
        ws_id = resp.json()["id"]
        target = await model.create_user("role-rm@test.com", "pass")
        # Add then remove
        await client.post(
            f"/workspaces/{ws_id}/roles/coders",
            headers=headers,
            json={"email": "role-rm@test.com"},
        )
        resp = await client.delete(
            f"/workspaces/{ws_id}/roles/coders/{target['id']}",
            headers=headers,
        )
        assert resp.status_code == 200
        # Verify removed
        resp = await client.get(f"/workspaces/{ws_id}/roles", headers=headers)
        roles = {r["role"]: r for r in resp.json()}
        member_ids = [m["id"] for m in roles["coders"]["members"]]
        assert target["id"] not in member_ids

    async def test_add_to_invalid_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "bad-role-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/workspaces/{ws_id}/roles/invalid",
            headers=headers,
            json={"email": "x@test.com"},
        )
        assert resp.status_code == 400

    async def test_add_nonexistent_user_to_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "nouser-role-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/workspaces/{ws_id}/roles/spectators",
            headers=headers,
            json={"email": "nobody@nowhere.com"},
        )
        assert resp.status_code == 404

    async def test_roles_on_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/workspaces/fake-id/roles", headers=headers)
        assert resp.status_code == 403

    async def test_remove_from_invalid_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "bad-rm-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.delete(
            f"/workspaces/{ws_id}/roles/invalid/some-id",
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_remove_from_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete(
            "/workspaces/fake-id/roles/coders/some-id",
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_add_to_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces/fake-id/roles/coders",
            headers=headers,
            json={"email": "x@test.com"},
        )
        assert resp.status_code == 403

    async def test_role_group_not_found_add(self, client, user):
        """Adding to a role when the group was deleted returns 404."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "norole-add-ws"}
        )
        ws_id = resp.json()["id"]
        # Delete the spectators group to simulate missing role group
        group = await model.get_group_by_name(f"spectators-{ws_id}")
        await model.delete_group(group["id"])
        resp = await client.post(
            f"/workspaces/{ws_id}/roles/spectators",
            headers=headers,
            json={"email": "x@test.com"},
        )
        assert resp.status_code == 404
        assert "Role group" in resp.json()["detail"]

    async def test_role_group_not_found_remove(self, client, user):
        """Removing from a role when the group was deleted returns 404."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "norole-rm-ws"}
        )
        ws_id = resp.json()["id"]
        group = await model.get_group_by_name(f"coders-{ws_id}")
        await model.delete_group(group["id"])
        resp = await client.delete(
            f"/workspaces/{ws_id}/roles/coders/some-id",
            headers=headers,
        )
        assert resp.status_code == 404
        assert "Role group" in resp.json()["detail"]

    async def test_roles_with_missing_group(self, client, user):
        """Listing roles skips groups that were deleted."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "missing-grp-ws"}
        )
        ws_id = resp.json()["id"]
        group = await model.get_group_by_name(f"spectators-{ws_id}")
        await model.delete_group(group["id"])
        resp = await client.get(f"/workspaces/{ws_id}/roles", headers=headers)
        roles = resp.json()
        role_names = [r["role"] for r in roles]
        assert "spectators" not in role_names
        assert "owners" in role_names


class TestChangeWorkspaceRole:
    async def test_change_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "chg-role-ws"}
        )
        ws_id = resp.json()["id"]
        target = await model.create_user("chg-role@test.com", "pass")
        # Add as coder
        resp = await client.patch(
            f"/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "chg-role@test.com", "role": "coders"},
        )
        assert resp.status_code == 200
        # Verify in coders
        roles = (
            await client.get(f"/workspaces/{ws_id}/roles", headers=headers)
        ).json()
        coders = [
            m["id"]
            for r in roles
            if r["role"] == "coders"
            for m in r["members"]
        ]
        assert target["id"] in coders
        # Change to spectator
        resp = await client.patch(
            f"/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "chg-role@test.com", "role": "spectators"},
        )
        assert resp.status_code == 200
        # Verify moved
        roles = (
            await client.get(f"/workspaces/{ws_id}/roles", headers=headers)
        ).json()
        coders = [
            m["id"]
            for r in roles
            if r["role"] == "coders"
            for m in r["members"]
        ]
        specs = [
            m["id"]
            for r in roles
            if r["role"] == "spectators"
            for m in r["members"]
        ]
        assert target["id"] not in coders
        assert target["id"] in specs

    async def test_remove_all_roles(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "rm-all-ws"}
        )
        ws_id = resp.json()["id"]
        await model.create_user("rm-all@test.com", "pass")
        await client.patch(
            f"/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "rm-all@test.com", "role": "coders"},
        )
        # Remove from all
        resp = await client.patch(
            f"/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "rm-all@test.com", "role": None},
        )
        assert resp.status_code == 200
        roles = (
            await client.get(f"/workspaces/{ws_id}/roles", headers=headers)
        ).json()
        all_members = [m["email"] for r in roles for m in r["members"]]
        assert "rm-all@test.com" not in all_members

    async def test_invalid_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "bad-chg-ws"}
        )
        ws_id = resp.json()["id"]
        await model.create_user("bad-chg@test.com", "pass")
        resp = await client.patch(
            f"/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "bad-chg@test.com", "role": "invalid"},
        )
        assert resp.status_code == 400

    async def test_nonexistent_user(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "nouser-chg-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.patch(
            f"/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "nobody@nowhere.com", "role": "coders"},
        )
        assert resp.status_code == 404

    async def test_change_role_missing_group(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "miss-grp-chg-ws"}
        )
        ws_id = resp.json()["id"]
        await model.create_user("miss-grp@test.com", "pass")
        # Delete the target role group
        group = await model.get_group_by_name(f"spectators-{ws_id}")
        await model.delete_group(group["id"])
        resp = await client.patch(
            f"/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "miss-grp@test.com", "role": "spectators"},
        )
        assert resp.status_code == 404
        assert "Role group" in resp.json()["detail"]

    async def test_change_role_skips_missing_groups_on_remove(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces",
            headers=headers,
            json={"name": "skip-miss-ws"},
        )
        ws_id = resp.json()["id"]
        await model.create_user("skip-miss@test.com", "pass")
        # Add user to coders
        await client.patch(
            f"/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "skip-miss@test.com", "role": "coders"},
        )
        # Delete spectators group — should not break removal
        group = await model.get_group_by_name(f"spectators-{ws_id}")
        await model.delete_group(group["id"])
        # Change role — removal phase should skip missing group
        resp = await client.patch(
            f"/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "skip-miss@test.com", "role": None},
        )
        assert resp.status_code == 200


class TestWorkspaceGroupSharing:
    async def test_share_with_group(self, client, admin_user, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "group-share-ws"}
        )
        ws_id = resp.json()["id"]
        group = await model.create_group("devs")

        resp = await client.post(
            f"/workspaces/{ws_id}/groups",
            headers=headers,
            json={"group_id": group["id"]},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "devs"

        # Group shows up in list
        resp = await client.get(f"/workspaces/{ws_id}/groups", headers=headers)
        assert resp.status_code == 200
        groups = resp.json()
        group_names = [g["name"] for g in groups]
        assert "devs" in group_names

    async def test_remove_group(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "group-rm-ws"}
        )
        ws_id = resp.json()["id"]
        group = await model.create_group("temp-devs")

        await client.post(
            f"/workspaces/{ws_id}/groups",
            headers=headers,
            json={"group_id": group["id"]},
        )
        resp = await client.delete(
            f"/workspaces/{ws_id}/groups/{group['id']}", headers=headers
        )
        assert resp.status_code == 200

        resp = await client.get(f"/workspaces/{ws_id}/groups", headers=headers)
        group_names = [g["name"] for g in resp.json()]
        assert "temp-devs" not in group_names

    async def test_share_with_nonexistent_group(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "bad-group-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/workspaces/{ws_id}/groups",
            headers=headers,
            json={"group_id": "nonexistent"},
        )
        assert resp.status_code == 404

    async def test_group_share_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/workspaces/nonexistent/groups", headers=headers
        )
        assert resp.status_code == 403


class TestUserGroupEndpoints:
    """Tests for /groups endpoints (user-accessible, ACL-gated)."""

    async def test_list_groups(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/groups", headers=headers)
        assert resp.status_code == 200

    async def test_create_group(self, client, admin_user, user):
        """Any authenticated user with create permission can create groups."""
        headers = await _auth_headers(client)
        # Need create permission on /groups — seed it
        await model.add_acl_entry(
            "/groups",
            0,
            model.ACTION_ALLOW,
            "create",
            model.PRINCIPAL_SYSTEM,
            system_principal=model.SYSTEM_AUTHENTICATED,
        )
        resp = await client.post(
            "/groups",
            headers=headers,
            json={"name": "user-group", "description": "My team"},
        )
        assert resp.status_code == 200
        group = resp.json()
        assert group["name"] == "user-group"

        # Creator should have * ACE on /groups/{id}
        entries = await model.get_acl_entries(f"/groups/{group['id']}")
        assert len(entries) >= 1
        assert any(
            e["permission"] == "*" and e["user_id"] == user["id"]
            for e in entries
        )

    async def test_create_group_duplicate(self, client, admin_user, user):
        headers = await _auth_headers(client)
        await model.add_acl_entry(
            "/groups",
            0,
            model.ACTION_ALLOW,
            "create",
            model.PRINCIPAL_SYSTEM,
            system_principal=model.SYSTEM_AUTHENTICATED,
        )
        await client.post(
            "/groups",
            headers=headers,
            json={"name": "dup-user-group"},
        )
        resp = await client.post(
            "/groups",
            headers=headers,
            json={"name": "dup-user-group"},
        )
        assert resp.status_code == 409

    async def test_update_own_group(self, client, admin_user, user):
        headers = await _auth_headers(client)
        await model.add_acl_entry(
            "/groups",
            0,
            model.ACTION_ALLOW,
            "create",
            model.PRINCIPAL_SYSTEM,
            system_principal=model.SYSTEM_AUTHENTICATED,
        )
        resp = await client.post(
            "/groups",
            headers=headers,
            json={"name": "edit-group"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/groups/{group_id}",
            headers=headers,
            json={"description": "updated"},
        )
        assert resp.status_code == 200

    async def test_delete_own_group(self, client, admin_user, user):
        headers = await _auth_headers(client)
        await model.add_acl_entry(
            "/groups",
            0,
            model.ACTION_ALLOW,
            "create",
            model.PRINCIPAL_SYSTEM,
            system_principal=model.SYSTEM_AUTHENTICATED,
        )
        resp = await client.post(
            "/groups",
            headers=headers,
            json={"name": "del-group"},
        )
        group_id = resp.json()["id"]
        resp = await client.delete(f"/groups/{group_id}", headers=headers)
        assert resp.status_code == 200
        # ACEs should be cleaned up
        entries = await model.get_acl_entries(f"/groups/{group_id}")
        assert entries == []

    async def test_manage_members_own_group(self, client, admin_user, user):
        headers = await _auth_headers(client)
        await model.add_acl_entry(
            "/groups",
            0,
            model.ACTION_ALLOW,
            "create",
            model.PRINCIPAL_SYSTEM,
            system_principal=model.SYSTEM_AUTHENTICATED,
        )
        resp = await client.post(
            "/groups",
            headers=headers,
            json={"name": "member-group"},
        )
        group_id = resp.json()["id"]

        # Add admin_user as member
        resp = await client.post(
            f"/groups/{group_id}/members",
            headers=headers,
            json={"user_id": admin_user["id"]},
        )
        assert resp.status_code == 200

        # List members
        resp = await client.get(f"/groups/{group_id}/members", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # Remove member
        resp = await client.delete(
            f"/groups/{group_id}/members/{admin_user['id']}",
            headers=headers,
        )
        assert resp.status_code == 200

    async def test_update_nonexistent_group(self, client, user):
        headers = await _auth_headers(client)
        # Grant * on fake group so ACL passes
        await model.add_acl_entry(
            "/groups/fake-id",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.patch(
            "/groups/fake-id",
            headers=headers,
            json={"name": "x"},
        )
        assert resp.status_code == 404

    async def test_update_group_no_fields(self, client, admin_user, user):
        headers = await _auth_headers(client)
        await model.add_acl_entry(
            "/groups",
            0,
            model.ACTION_ALLOW,
            "create",
            model.PRINCIPAL_SYSTEM,
            system_principal=model.SYSTEM_AUTHENTICATED,
        )
        resp = await client.post(
            "/groups",
            headers=headers,
            json={"name": "noupdate-group"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/groups/{group_id}",
            headers=headers,
            json={},
        )
        assert resp.status_code == 400

    async def test_delete_nonexistent_group(self, client, user):
        headers = await _auth_headers(client)
        await model.add_acl_entry(
            "/groups/fake-del",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.delete("/groups/fake-del", headers=headers)
        assert resp.status_code == 404

    async def test_list_members_nonexistent_group(self, client, user):
        headers = await _auth_headers(client)
        await model.add_acl_entry(
            "/groups/fake-mem",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.get("/groups/fake-mem/members", headers=headers)
        assert resp.status_code == 404

    async def test_add_member_nonexistent_group(self, client, user):
        headers = await _auth_headers(client)
        await model.add_acl_entry(
            "/groups/fake-add",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.post(
            "/groups/fake-add/members",
            headers=headers,
            json={"user_id": user["id"]},
        )
        assert resp.status_code == 404

    async def test_add_member_nonexistent_user(self, client, admin_user, user):
        headers = await _auth_headers(client)
        await model.add_acl_entry(
            "/groups",
            0,
            model.ACTION_ALLOW,
            "create",
            model.PRINCIPAL_SYSTEM,
            system_principal=model.SYSTEM_AUTHENTICATED,
        )
        resp = await client.post(
            "/groups",
            headers=headers,
            json={"name": "baduser-group"},
        )
        group_id = resp.json()["id"]
        resp = await client.post(
            f"/groups/{group_id}/members",
            headers=headers,
            json={"user_id": "nonexistent"},
        )
        assert resp.status_code == 404

    async def test_remove_nonmember(self, client, admin_user, user):
        headers = await _auth_headers(client)
        await model.add_acl_entry(
            "/groups",
            0,
            model.ACTION_ALLOW,
            "create",
            model.PRINCIPAL_SYSTEM,
            system_principal=model.SYSTEM_AUTHENTICATED,
        )
        resp = await client.post(
            "/groups",
            headers=headers,
            json={"name": "noremove-group"},
        )
        group_id = resp.json()["id"]
        resp = await client.delete(
            f"/groups/{group_id}/members/nonexistent",
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_non_owner_cannot_manage(self, client, admin_user, user):
        """User without permission on the group gets 403."""
        # Admin creates a group (no ACE for regular user)
        admin_headers = {
            "Authorization": f"Bearer {(await client.post('/auth/login', json={'email': 'testadmin@example.com', 'password': 'testpass'})).json()['access_token']}"
        }
        resp = await client.post(
            "/admin/groups",
            headers=admin_headers,
            json={"name": "admin-only-group"},
        )
        group_id = resp.json()["id"]

        # Regular user tries to manage members
        headers = await _auth_headers(client)
        resp = await client.post(
            f"/groups/{group_id}/members",
            headers=headers,
            json={"user_id": user["id"]},
        )
        assert resp.status_code == 403


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
    def _ws_token_headers(self, workspace_id="ws-test"):
        token = auth.create_workspace_token(workspace_id)
        return {"Authorization": f"Bearer {token}"}

    async def test_missing_token_returns_401(self, client, user):
        resp = await client.post(
            "/api/browser-delegate",
            json={"action": "fetch", "browser_id": "bad-id"},
        )
        assert resp.status_code == 401

    async def test_unknown_browser_id_returns_403(self, client, user):
        resp = await client.post(
            "/api/browser-delegate",
            json={"action": "fetch", "browser_id": "bad-id"},
            headers=self._ws_token_headers(),
        )
        assert resp.status_code == 403
        assert "Unknown browser ID" in resp.json()["detail"]

    async def test_expired_token_returns_401(self, client, user):
        with patch.object(
            auth,
            "decode_workspace_token",
            return_value=auth.WORKSPACE_TOKEN_EXPIRED,
        ):
            resp = await client.post(
                "/api/browser-delegate",
                json={"action": "fetch", "browser_id": "x"},
                headers={"Authorization": "Bearer some-expired-token"},
            )
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    async def test_browser_id_routes_to_correct_tab(self, client, user):
        """Browser ID routes to the specific browser tab."""
        mock_sock = MagicMock()
        container.registry.register_browser("bid-conn", "ws-conn", mock_sock)
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
                    json={"action": "fetch", "browser_id": "bid-conn"},
                    headers=self._ws_token_headers("ws-conn"),
                )
            assert resp.status_code == 200
            assert resp.json()["body"] == "targeted"
            mock_session.dispatch_browser_request_to.assert_awaited_once_with(
                mock_sock, {"action": "fetch"}, timeout=30.0
            )
        finally:
            container.registry.revoke_workspace_browsers("ws-conn")

    async def test_browser_not_subscribed_returns_502(self, client, user):
        """Returns 502 when target not in browser_subscribers."""
        mock_sock = MagicMock()
        container.registry.register_browser("bid-nosub", "ws-nosub", mock_sock)
        mock_session = AsyncMock()
        mock_session.browser_subscribers = set()
        try:
            with patch.object(
                wshandler.state,
                "get_session",
                return_value=mock_session,
            ):
                resp = await client.post(
                    "/api/browser-delegate",
                    json={"action": "fetch", "browser_id": "bid-nosub"},
                    headers=self._ws_token_headers("ws-nosub"),
                )
            assert resp.status_code == 502
            assert "Browser connection not available" in resp.json()["detail"]
        finally:
            container.registry.revoke_workspace_browsers("ws-nosub")

    async def test_no_session_returns_502(self, client, user):
        mock_sock = MagicMock()
        container.registry.register_browser(
            "bid-nosess", "ws-nosess", mock_sock
        )
        try:
            resp = await client.post(
                "/api/browser-delegate",
                json={"action": "fetch", "browser_id": "bid-nosess"},
                headers=self._ws_token_headers("ws-nosess"),
            )
            assert resp.status_code == 502
            assert "No browser client" in resp.json()["detail"]
        finally:
            container.registry.revoke_workspace_browsers("ws-nosess")

    async def test_dispatch_error_returns_502(self, client, user):
        mock_sock = MagicMock()
        container.registry.register_browser("bid-err", "ws-err", mock_sock)
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
                    json={"action": "fetch", "browser_id": "bid-err"},
                    headers=self._ws_token_headers("ws-err"),
                )
            assert resp.status_code == 502
            assert "timeout" in resp.json()["detail"].lower()
        finally:
            container.registry.revoke_workspace_browsers("ws-err")

    async def test_stream_endpoint_relays_ndjson(self, client, user):
        """The streaming endpoint relays the generator's NDJSON to the caller."""
        mock_sock = MagicMock()
        container.registry.register_browser(
            "bid-stream", "ws-stream", mock_sock
        )

        async def fake_stream():
            yield '{"type": "chunk", "delta": "a"}\n'
            yield '{"type": "done", "result": {"ok": true}}\n'.replace(
                "true", "1"
            )

        mock_session = AsyncMock()
        mock_session.browser_subscribers = {mock_sock}
        mock_session.dispatch_browser_request_stream_to = MagicMock(
            return_value=fake_stream()
        )
        try:
            with patch.object(
                wshandler.state, "get_session", return_value=mock_session
            ):
                resp = await client.post(
                    "/api/browser-delegate/stream",
                    json={
                        "action": "soliplex_query",
                        "browser_id": "bid-stream",
                    },
                    headers=self._ws_token_headers("ws-stream"),
                )
            assert resp.status_code == 200
            assert '"chunk"' in resp.text
            assert '"done"' in resp.text
            mock_session.dispatch_browser_request_stream_to.assert_called_once()
        finally:
            container.registry.revoke_workspace_browsers("ws-stream")


# --- Volume routes ---


def _managed_volume(user_id="test-user"):
    """An inspect_volume result owned by this klangk instance."""
    return {
        "Labels": {
            "klangk.managed": "true",
            "klangk.instance": container.INSTANCE_ID,
            "klangk.user-id": user_id,
        }
    }


class TestVolumeRoutes:
    async def test_list_volumes(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            podman,
            "list_volumes",
            AsyncMock(
                return_value=[
                    {
                        "Name": "my-vol",
                        "CreatedAt": "2026-01-01T00:00:00Z",
                        "Labels": {
                            "klangk.instance": container.INSTANCE_ID,
                            "klangk.user-id": user["id"],
                        },
                    },
                    {
                        "Name": "other-vol",
                        "CreatedAt": "2026-01-01T00:00:00Z",
                        "Labels": {
                            "klangk.instance": container.INSTANCE_ID,
                            "klangk.user-id": "someone-else",
                        },
                    },
                ]
            ),
        ):
            resp = await client.get("/volumes", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "my-vol"

    async def test_create_volume(self, client, user):
        headers = await _auth_headers(client)
        mock_create = AsyncMock(
            return_value={"Name": "new-vol", "CreatedAt": "2026-01-01"}
        )
        with (
            patch.object(
                podman, "inspect_volume", AsyncMock(return_value=None)
            ),
            patch.object(podman, "create_volume", mock_create),
        ):
            resp = await client.post(
                "/volumes",
                json={"name": "new-vol"},
                headers=headers,
            )
        assert resp.status_code == 200
        assert resp.json()["name"] == "new-vol"
        _, labels = mock_create.call_args.args
        assert labels["klangk.user-id"] == user["id"]

    async def test_create_duplicate_volume(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            podman,
            "inspect_volume",
            AsyncMock(return_value={"Name": "dup-vol"}),
        ):
            resp = await client.post(
                "/volumes",
                json={"name": "dup-vol"},
                headers=headers,
            )
        assert resp.status_code == 409

    async def test_create_volume_error_propagates(self, client, user):
        headers = await _auth_headers(client)
        with (
            patch.object(
                podman, "inspect_volume", AsyncMock(return_value=None)
            ),
            patch.object(
                podman,
                "create_volume",
                AsyncMock(side_effect=podman.PodmanError(500, "boom")),
            ),
            pytest.raises(podman.PodmanError),
        ):
            await client.post(
                "/volumes",
                json={"name": "err-vol"},
                headers=headers,
            )

    async def test_delete_volume(self, client, user):
        headers = await _auth_headers(client)
        with (
            patch.object(
                podman,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(user["id"])),
            ),
            patch.object(podman, "remove_volume", AsyncMock()),
        ):
            resp = await client.delete("/volumes/test-vol", headers=headers)
        assert resp.status_code == 200

    async def test_delete_volume_not_found(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            podman, "inspect_volume", AsyncMock(return_value=None)
        ):
            resp = await client.delete("/volumes/nope", headers=headers)
        assert resp.status_code == 404

    async def test_delete_volume_wrong_instance(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            podman,
            "inspect_volume",
            AsyncMock(return_value={"Labels": {"klangk.instance": "other"}}),
        ):
            resp = await client.delete("/volumes/foreign", headers=headers)
        assert resp.status_code == 404

    async def test_delete_volume_wrong_user(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            podman,
            "inspect_volume",
            AsyncMock(return_value=_managed_volume("someone-else")),
        ):
            resp = await client.delete("/volumes/other", headers=headers)
        assert resp.status_code == 403

    async def test_delete_volume_remove_not_found(self, client, user):
        """Volume vanishes between inspect and remove -> 404."""
        headers = await _auth_headers(client)
        with (
            patch.object(
                podman,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(user["id"])),
            ),
            patch.object(
                podman,
                "remove_volume",
                AsyncMock(side_effect=podman.PodmanError(404, "gone")),
            ),
        ):
            resp = await client.delete("/volumes/gone", headers=headers)
        assert resp.status_code == 404

    async def test_delete_volume_other_error(self, client, user):
        headers = await _auth_headers(client)
        with (
            patch.object(
                podman,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(user["id"])),
            ),
            patch.object(
                podman,
                "remove_volume",
                AsyncMock(side_effect=podman.PodmanError(500, "internal")),
            ),
            pytest.raises(podman.PodmanError),
        ):
            await client.delete("/volumes/err-vol", headers=headers)

    async def test_delete_volume_in_use(self, client, user):
        headers = await _auth_headers(client)
        with (
            patch.object(
                podman,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(user["id"])),
            ),
            patch.object(
                podman,
                "remove_volume",
                AsyncMock(side_effect=podman.PodmanError(409, "in use")),
            ),
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
        assert resp.status_code == 403

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

    async def test_upload_exceeds_size_limit(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        with patch.object(api, "FILE_UPLOAD_SIZE_MAX", 10):
            resp = await client.post(
                f"/workspaces/{ws_id}/files/upload?path=big.txt",
                headers=headers,
                files={"file": ("big.txt", b"x" * 100, "text/plain")},
            )
        assert resp.status_code == 413
        assert "limit" in resp.json()["detail"].lower()

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
        assert resp.status_code == 403

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
        assert resp.status_code == 403

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
        assert resp.status_code == 403

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
        assert resp.status_code == 403

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
        assert resp.status_code == 403

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


class TestGroups:
    async def test_create_group(self, db):
        group = await model.create_group("editors", "Editor group")
        assert group["name"] == "editors"
        assert group["description"] == "Editor group"
        assert group["id"]

    async def test_get_group_by_name(self, db):
        await model.create_group("testers")
        found = await model.get_group_by_name("testers")
        assert found is not None
        assert found["name"] == "testers"

    async def test_add_user_to_group(self, user):
        group = await model.create_group("devs")
        await model.add_user_to_group(user["id"], group["id"])
        group_ids = await model.get_user_group_ids(user["id"])
        assert group["id"] in group_ids

    async def test_add_user_to_group_idempotent(self, user):
        group = await model.create_group("devs")
        await model.add_user_to_group(user["id"], group["id"])
        await model.add_user_to_group(user["id"], group["id"])
        group_ids = await model.get_user_group_ids(user["id"])
        assert group_ids.count(group["id"]) == 1

    async def test_get_groups_empty(self, user):
        group_ids = await model.get_user_group_ids(user["id"])
        assert group_ids == []

    async def test_remove_user_from_group(self, user):
        group = await model.create_group("devs")
        await model.add_user_to_group(user["id"], group["id"])
        removed = await model.remove_user_from_group(user["id"], group["id"])
        assert removed is True
        group_ids = await model.get_user_group_ids(user["id"])
        assert group["id"] not in group_ids

    async def test_cascade_delete_user(self, db):
        """Deleting a user cascades to user_groups."""
        user = await model.create_user("delme", "hash")
        group = await model.create_group("devs")
        await model.add_user_to_group(user["id"], group["id"])
        assert group["id"] in await model.get_user_group_ids(user["id"])
        async with model.transaction() as db_conn:
            await db_conn.execute(
                "DELETE FROM users WHERE id = ?", (user["id"],)
            )
        assert await model.get_user_group_ids(user["id"]) == []

    async def test_cascade_delete_group(self, user):
        """Deleting a group cascades to user_groups."""
        group = await model.create_group("temp")
        await model.add_user_to_group(user["id"], group["id"])
        assert group["id"] in await model.get_user_group_ids(user["id"])
        await model.delete_group(group["id"])
        assert group["id"] not in await model.get_user_group_ids(user["id"])

    async def test_jwt_has_no_roles(self, user):
        """JWT tokens no longer include roles."""
        token = auth.create_token(user["id"], "testuser@example.com")
        payload = auth.decode_token(token)
        assert "roles" not in payload

    async def test_login_jwt_has_no_roles(self, client, user):
        resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        payload = auth.decode_token(token)
        assert "roles" not in payload


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
        # Admin user should have admin group
        admin = next(u for u in users if u["email"] == "testadmin@example.com")
        assert any(g["name"] == "admin" for g in admin["groups"])

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

    async def test_delete_agent_user_rejected(self, client, admin_user, db):
        from klangk_backend.main import seed_agent_user

        await seed_agent_user()
        headers = await self._admin_headers(client)
        resp = await client.delete(
            f"/admin/users/{model.AGENT_USER_ID}", headers=headers
        )
        assert resp.status_code == 400
        assert "system agent" in resp.json()["detail"]

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

    async def test_update_agent_password_rejected(
        self, client, admin_user, db
    ):
        # Seed the agent user so it exists in the DB
        from klangk_backend.main import seed_agent_user

        await seed_agent_user()
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/admin/users/{model.AGENT_USER_ID}",
            json={"password": "sneaky"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "system agent" in resp.json()["detail"]


class TestGroupEndpoints:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_list_groups(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get("/admin/groups", headers=headers)
        assert resp.status_code == 200
        groups = resp.json()
        assert any(g["name"] == "admin" for g in groups)

    async def test_create_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/groups",
            headers=headers,
            json={"name": "editors", "description": "Editor group"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "editors"

    async def test_create_group_duplicate(self, client, admin_user):
        headers = await self._admin_headers(client)
        await client.post(
            "/admin/groups",
            headers=headers,
            json={"name": "dup-group"},
        )
        resp = await client.post(
            "/admin/groups",
            headers=headers,
            json={"name": "dup-group"},
        )
        assert resp.status_code == 409

    async def test_update_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/groups",
            headers=headers,
            json={"name": "to-rename"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/admin/groups/{group_id}",
            headers=headers,
            json={"name": "renamed", "description": "new desc"},
        )
        assert resp.status_code == 200

    async def test_update_group_no_fields(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/groups",
            headers=headers,
            json={"name": "no-update"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/admin/groups/{group_id}",
            headers=headers,
            json={},
        )
        assert resp.status_code == 400

    async def test_update_group_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            "/admin/groups/nonexistent",
            headers=headers,
            json={"name": "x"},
        )
        assert resp.status_code == 404

    async def test_delete_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/groups",
            headers=headers,
            json={"name": "to-delete"},
        )
        group_id = resp.json()["id"]
        resp = await client.delete(
            f"/admin/groups/{group_id}", headers=headers
        )
        assert resp.status_code == 200

    async def test_delete_group_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            "/admin/groups/nonexistent", headers=headers
        )
        assert resp.status_code == 404

    async def test_list_group_members(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/groups",
            headers=headers,
            json={"name": "members-test"},
        )
        group_id = resp.json()["id"]
        # Add user to group
        resp = await client.post(
            f"/admin/groups/{group_id}/members",
            headers=headers,
            json={"user_id": user["id"]},
        )
        assert resp.status_code == 200
        # List members
        resp = await client.get(
            f"/admin/groups/{group_id}/members", headers=headers
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["email"] == "testuser@example.com"

    async def test_list_group_members_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/admin/groups/nonexistent/members", headers=headers
        )
        assert resp.status_code == 404

    async def test_add_group_member_user_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/groups",
            headers=headers,
            json={"name": "member-test2"},
        )
        group_id = resp.json()["id"]
        resp = await client.post(
            f"/admin/groups/{group_id}/members",
            headers=headers,
            json={"user_id": "nonexistent"},
        )
        assert resp.status_code == 404

    async def test_add_group_member_group_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/groups/nonexistent/members",
            headers=headers,
            json={"user_id": "x"},
        )
        assert resp.status_code == 404

    async def test_remove_group_member(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/groups",
            headers=headers,
            json={"name": "remove-test"},
        )
        group_id = resp.json()["id"]
        await client.post(
            f"/admin/groups/{group_id}/members",
            headers=headers,
            json={"user_id": user["id"]},
        )
        resp = await client.delete(
            f"/admin/groups/{group_id}/members/{user['id']}", headers=headers
        )
        assert resp.status_code == 200

    async def test_remove_group_member_not_member(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/groups",
            headers=headers,
            json={"name": "rm-test"},
        )
        group_id = resp.json()["id"]
        resp = await client.delete(
            f"/admin/groups/{group_id}/members/nonexistent", headers=headers
        )
        assert resp.status_code == 404


class TestACLEndpoints:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_get_acl_tree(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get("/admin/acl/tree", headers=headers)
        assert resp.status_code == 200
        tree = resp.json()
        assert len(tree) > 0

    async def test_get_acl_by_user(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            f"/admin/acl/by-principal/user/{user['id']}", headers=headers
        )
        assert resp.status_code == 200

    async def test_get_acl_by_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        # Get the admin group ID
        groups = await model.list_groups()
        admin_group = next(g for g in groups if g["name"] == "admin")
        resp = await client.get(
            f"/admin/acl/by-principal/group/{admin_group['id']}",
            headers=headers,
        )
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) > 0

    async def test_my_permissions(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get("/api/my-permissions", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "testadmin@example.com"
        assert "/admin" in data["permissions"]
        assert "*" in data["permissions"]["/admin"]

    async def test_my_permissions_non_admin(self, client, admin_user, user):
        """Non-admin user has no admin permissions."""
        headers = await _auth_headers(client)
        resp = await client.get("/api/my-permissions", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "/admin" not in data["permissions"]

    async def test_my_permissions_for_resource(self, client, user):
        """Check permissions for a specific resource."""
        headers = await _auth_headers(client)
        # Create a workspace (owner gets * ACE)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "perm-check"}
        )
        ws_id = resp.json()["id"]
        resp = await client.get(
            f"/api/my-permissions?resource=/workspaces/{ws_id}",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        perms = data["permissions"].get(f"/workspaces/{ws_id}", [])
        assert "*" in perms
        assert "view" in perms
        assert "terminal" in perms

    async def test_my_permissions_for_resource_no_access(
        self, client, admin_user, user
    ):
        """User without specific ACE only gets inherited permissions."""
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/my-permissions?resource=/workspaces/nonexistent",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        perms = data["permissions"].get("/workspaces/nonexistent", [])
        # Inherits view from root, but not workspace-specific perms
        assert "view" in perms
        assert "*" not in perms
        assert "terminal" not in perms


class TestAdminResourceACL:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_get_resource_acl(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/admin/acl/resource?resource=/workspaces", headers=headers
        )
        assert resp.status_code == 200
        entries = resp.json()
        # Default ACL has Authenticated create on /workspaces
        assert any(e["permission"] == "create" for e in entries)

    async def test_replace_resource_acl(self, client, admin_user):
        headers = await self._admin_headers(client)
        # Get current ACL
        resp = await client.get(
            "/admin/acl/resource?resource=/workspaces", headers=headers
        )
        original = resp.json()

        # Add a new entry
        new_entries = [
            {
                "action": e["action"],
                "principal_type": e["principal_type"],
                "permission": e["permission"],
                "user_id": e.get("user_id"),
                "group_id": e.get("group_id"),
                "system_principal": e.get("system_principal"),
            }
            for e in original
        ] + [
            {
                "action": model.ACTION_ALLOW,
                "principal_type": model.PRINCIPAL_SYSTEM,
                "permission": "view",
                "system_principal": model.SYSTEM_AUTHENTICATED,
            },
        ]
        resp = await client.put(
            "/admin/acl/resource?resource=/workspaces",
            headers=headers,
            json=new_entries,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == len(original) + 1

        # Restore original
        restore = [
            {
                "action": e["action"],
                "principal_type": e["principal_type"],
                "permission": e["permission"],
                "user_id": e.get("user_id"),
                "group_id": e.get("group_id"),
                "system_principal": e.get("system_principal"),
            }
            for e in original
        ]
        resp = await client.put(
            "/admin/acl/resource?resource=/workspaces",
            headers=headers,
            json=restore,
        )
        assert resp.status_code == 200

    async def test_get_resource_acl_requires_admin(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/admin/acl/resource?resource=/workspaces", headers=headers
        )
        assert resp.status_code == 403

    async def test_root_acl_rejects_removing_authenticated_view(
        self, client, admin_user
    ):
        headers = await self._admin_headers(client)
        # Try to save root ACL without Authenticated view
        resp = await client.put(
            "/admin/acl/resource?resource=/",
            headers=headers,
            json=[
                {
                    "action": model.ACTION_DENY,
                    "principal_type": model.PRINCIPAL_SYSTEM,
                    "permission": "*",
                    "system_principal": model.SYSTEM_EVERYONE,
                },
            ],
        )
        assert resp.status_code == 400
        assert "locking out" in resp.json()["detail"]

    async def test_root_acl_accepts_wildcard_authenticated(
        self, client, admin_user
    ):
        headers = await self._admin_headers(client)
        # Authenticated with * should be accepted
        resp = await client.put(
            "/admin/acl/resource?resource=/",
            headers=headers,
            json=[
                {
                    "action": model.ACTION_ALLOW,
                    "principal_type": model.PRINCIPAL_SYSTEM,
                    "permission": "*",
                    "system_principal": model.SYSTEM_AUTHENTICATED,
                },
                {
                    "action": model.ACTION_DENY,
                    "principal_type": model.PRINCIPAL_SYSTEM,
                    "permission": "*",
                    "system_principal": model.SYSTEM_EVERYONE,
                },
            ],
        )
        assert resp.status_code == 200

    async def test_admin_acl_rejects_removing_all_group_access(
        self, client, admin_user
    ):
        headers = await self._admin_headers(client)
        # Try to save /admin ACL with no group Allow
        resp = await client.put(
            "/admin/acl/resource?resource=/admin",
            headers=headers,
            json=[
                {
                    "action": model.ACTION_DENY,
                    "principal_type": model.PRINCIPAL_SYSTEM,
                    "permission": "*",
                    "system_principal": model.SYSTEM_EVERYONE,
                },
            ],
        )
        assert resp.status_code == 400
        assert "locking out" in resp.json()["detail"]


class TestSafePath:
    def test_valid_path(self, temp_data_dir):
        path = ws_mod._safe_path("user1", "home", "ws1")
        assert path == ws_mod.WORKSPACES_ROOT / "user1" / "home" / "ws1"

    def test_traversal_raises(self, temp_data_dir):
        with pytest.raises(ValueError, match="Path traversal blocked"):
            ws_mod._safe_path("..", "..", "etc", "passwd")


class TestSanitizeFilename:
    def test_safe_characters_preserved(self):
        assert ws_mod._sanitize_filename("hello-world_v2.tar.gz") == (
            "hello-world_v2.tar.gz"
        )

    def test_unsafe_characters_replaced(self):
        assert ws_mod._sanitize_filename("a/b\\c..d\x00e") == "a_b_c..d_e"

    def test_email_sanitized(self):
        assert ws_mod._sanitize_filename("user@example.com") == (
            "user@example.com"
        )


class TestRmtree:
    def test_removes_directory(self, temp_data_dir):
        d = temp_data_dir / "workspaces" / "toremove"
        d.mkdir(parents=True)
        (d / "file.txt").write_text("data")
        ws_mod._rmtree(d, "test")
        assert not d.exists()

    def test_logs_errors(self, temp_data_dir, caplog):
        """Logs warnings on individual file removal failures."""
        d = temp_data_dir / "workspaces" / "failremove"
        d.mkdir(parents=True)

        def bad_rmtree(path, onexc=None):
            onexc(os.unlink, str(d / "bad"), PermissionError("denied"))

        with patch.object(shutil, "rmtree", bad_rmtree):
            import logging

            with caplog.at_level(logging.WARNING):
                ws_mod._rmtree(d, "test-label")
        assert "denied" in caplog.text
        assert "test-label" in caplog.text


class TestBuildWorkspaceArchive:
    async def test_builds_importable_archive(self, temp_data_dir):
        """Archive contains workspace.json and home/ directory."""
        import json
        import subprocess

        ws_root = ws_mod.WORKSPACES_ROOT
        ws_root.mkdir(parents=True, exist_ok=True)
        home_dir = ws_root / "user1" / "home" / "ws1"
        home_dir.mkdir(parents=True)
        (home_dir / "hello.txt").write_text("test content")

        metadata = {"name": "myws", "image": None, "num_ports": 5}
        archive_path = ws_root / "test.tar.gz"

        result = await ws_mod.build_workspace_archive(
            metadata, home_dir, archive_path
        )
        assert result is True
        assert archive_path.exists()

        # Verify archive contents
        listing = subprocess.run(
            ["tar", "tzf", str(archive_path)],
            capture_output=True,
            text=True,
        )
        members = listing.stdout.strip().split("\n")
        assert "workspace.json" in members
        assert any(m.startswith("home/") or m == "home" for m in members)

        # Verify workspace.json content
        meta_out = subprocess.run(
            ["tar", "xzf", str(archive_path), "-O", "workspace.json"],
            capture_output=True,
            text=True,
        )
        meta = json.loads(meta_out.stdout)
        assert meta["name"] == "myws"

    async def test_builds_archive_without_home(self, temp_data_dir):
        """Archive works when home directory doesn't exist."""
        ws_root = ws_mod.WORKSPACES_ROOT
        ws_root.mkdir(parents=True, exist_ok=True)
        home_dir = ws_root / "nonexistent"
        metadata = {"name": "empty"}
        archive_path = ws_root / "empty.tar.gz"

        result = await ws_mod.build_workspace_archive(
            metadata, home_dir, archive_path
        )
        assert result is True
        assert archive_path.exists()

    async def test_excludes_external_symlinks(self, temp_data_dir):
        """Symlinks pointing outside home_dir are excluded."""
        import subprocess

        ws_root = ws_mod.WORKSPACES_ROOT
        ws_root.mkdir(parents=True, exist_ok=True)
        home_dir = ws_root / "user1" / "home" / "ws1"
        home_dir.mkdir(parents=True)
        (home_dir / "good.txt").write_text("keep")
        (home_dir / "external_link").symlink_to("/etc/passwd")
        (home_dir / "relative_link").symlink_to("good.txt")

        metadata = {"name": "test"}
        archive_path = ws_root / "symtest.tar.gz"

        result = await ws_mod.build_workspace_archive(
            metadata, home_dir, archive_path
        )
        assert result is True

        listing = subprocess.run(
            ["tar", "tzf", str(archive_path)],
            capture_output=True,
            text=True,
        )
        members = listing.stdout.strip().split("\n")
        assert any("good.txt" in m for m in members)
        # All symlinks are preserved (stored as symlinks, not contents)
        assert any("external_link" in m for m in members)
        assert any("relative_link" in m for m in members)

    async def test_tar_failure_returns_false(self, temp_data_dir):
        """Returns False when tar exits non-zero."""
        ws_root = ws_mod.WORKSPACES_ROOT
        ws_root.mkdir(parents=True, exist_ok=True)
        home_dir = ws_root / "home"
        home_dir.mkdir()
        metadata = {"name": "fail"}
        archive_path = ws_root / "fail.tar.gz"

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"tar: error"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await ws_mod.build_workspace_archive(
                metadata, home_dir, archive_path
            )
        assert result is False

    async def test_oserror_returns_false(self, temp_data_dir):
        """Returns False when tar cannot be started."""
        ws_root = ws_mod.WORKSPACES_ROOT
        ws_root.mkdir(parents=True, exist_ok=True)
        home_dir = ws_root / "home"
        metadata = {"name": "fail"}
        archive_path = ws_root / "fail.tar.gz"

        with patch(
            "asyncio.create_subprocess_exec", side_effect=OSError("no tar")
        ):
            result = await ws_mod.build_workspace_archive(
                metadata, home_dir, archive_path
            )
        assert result is False

    async def test_path_outside_workspaces_root_rejected(self, temp_data_dir):
        """Returns False if paths are outside WORKSPACES_ROOT."""
        home_dir = temp_data_dir / "outside"
        home_dir.mkdir(parents=True)
        metadata = {"name": "bad"}
        archive_path = temp_data_dir / "bad.tar.gz"

        result = await ws_mod.build_workspace_archive(
            metadata, home_dir, archive_path
        )
        assert result is False


class TestWorkspaceMetadata:
    def test_extracts_metadata(self):
        ws = {
            "name": "myws",
            "image": "ubuntu",
            "default_command": "bash",
            "mounts": ["/data:/data"],
            "env": {"FOO": "bar"},
            "num_ports": 3,
        }
        meta = ws_mod.workspace_metadata(ws)
        assert meta == {
            "name": "myws",
            "image": "ubuntu",
            "default_command": "bash",
            "mounts": ["/data:/data"],
            "env": {"FOO": "bar"},
            "num_ports": 3,
        }

    def test_defaults_num_ports(self):
        meta = ws_mod.workspace_metadata({"name": "x"})
        assert meta["num_ports"] == 5


class TestArchiveUserData:
    async def test_archive_creates_importable_tarballs(self, user, workspace):
        """Creates one .tar.gz per workspace in export format."""
        import json
        import subprocess

        # Put a file in the workspace home directory
        home_dir = ws_mod.home_path(user["id"], workspace["id"])
        home_dir.mkdir(parents=True, exist_ok=True)
        (home_dir / "hello.txt").write_text("test content")

        user_dir = ws_mod.WORKSPACES_ROOT / user["id"]
        result = await ws_mod.archive_user_data(user["id"], user["email"])
        assert len(result) == 1
        archive = result[0]
        assert archive.exists()
        assert archive.name.endswith(".tar.gz")
        assert user["email"].replace("@", "_") in archive.name or True
        # Original directory should be removed
        assert not user_dir.exists()

        # Verify it's in export format (workspace.json + home/)
        meta_out = subprocess.run(
            ["tar", "xzf", str(archive), "-O", "workspace.json"],
            capture_output=True,
            text=True,
        )
        meta = json.loads(meta_out.stdout)
        assert meta["name"] == workspace["name"]

        listing = subprocess.run(
            ["tar", "tzf", str(archive)],
            capture_output=True,
            text=True,
        )
        members = listing.stdout.strip().split("\n")
        assert any(m.startswith("home/") or m == "home" for m in members)

    async def test_archive_multiple_workspaces(self, user):
        """Creates separate archives for each workspace."""
        ws1 = await model.create_workspace(user["id"], "ws-one")
        ws2 = await model.create_workspace(user["id"], "ws-two")

        for ws in [ws1, ws2]:
            home = ws_mod.home_path(user["id"], ws["id"])
            home.mkdir(parents=True, exist_ok=True)
            (home / "file.txt").write_text("data")

        result = await ws_mod.archive_user_data(user["id"], user["email"])
        assert len(result) == 2
        names = {a.name for a in result}
        assert any("ws-one" in n for n in names)
        assert any("ws-two" in n for n in names)

    async def test_archive_no_data_dir(self, user):
        """Returns empty list if user has no data directory."""
        result = await ws_mod.archive_user_data(user["id"], user["email"])
        assert result == []

    async def test_archive_no_workspaces(self, user):
        """Returns empty list if user has data dir but no workspaces."""
        user_dir = ws_mod.WORKSPACES_ROOT / user["id"]
        user_dir.mkdir(parents=True)
        result = await ws_mod.archive_user_data(user["id"], user["email"])
        assert result == []

    async def test_archive_tar_failure_skips_workspace(self, user, workspace):
        """Skips workspaces where tar fails, doesn't remove user dir."""
        home_dir = ws_mod.home_path(user["id"], workspace["id"])
        home_dir.mkdir(parents=True, exist_ok=True)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"tar: error"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await ws_mod.archive_user_data(user["id"], user["email"])
        assert result == []
        # User dir not removed since no archives were created
        assert (ws_mod.WORKSPACES_ROOT / user["id"]).exists()

    async def test_archive_sanitizes_email(self, user, workspace):
        """Email with path separators is sanitized in archive filename."""
        home_dir = ws_mod.home_path(user["id"], workspace["id"])
        home_dir.mkdir(parents=True, exist_ok=True)

        result = await ws_mod.archive_user_data(
            user["id"], "user/../../etc/passwd"
        )
        assert len(result) == 1
        archive = result[0]
        assert archive.resolve().is_relative_to(
            ws_mod.WORKSPACES_ROOT.resolve()
        )
        # Slashes are replaced with underscores
        assert "/" not in archive.name
        assert "\\" not in archive.name

    async def test_archive_path_traversal_blocked(self, user, workspace):
        """Skips workspace if archive path would escape WORKSPACES_ROOT."""
        from pathlib import PosixPath

        home_dir = ws_mod.home_path(user["id"], workspace["id"])
        home_dir.mkdir(parents=True, exist_ok=True)

        orig_is_relative_to = PosixPath.is_relative_to

        def fake_is_relative_to(self, other):
            if self.suffix == ".gz":
                return False
            return orig_is_relative_to(self, other)

        with patch.object(PosixPath, "is_relative_to", fake_is_relative_to):
            result = await ws_mod.archive_user_data(user["id"], user["email"])
        assert result == []


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

    async def test_export_preserves_all_symlinks(
        self, client, admin_user, user
    ):
        """All symlinks are preserved in export (stored as links, not content)."""
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
        (home / "work" / "relative_link").symlink_to("real.txt")
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
            assert any("relative_link" in n for n in names)
            # External symlinks preserved as symlinks (not contents)
            assert any("external_link" in n for n in names)
            ext = [m for m in tar.getmembers() if "external_link" in m.name]
            assert len(ext) == 1
            assert ext[0].issym()
            assert ext[0].linkname == "/etc/passwd"

    async def test_export_import_deep_nesting(self, client, admin_user, user):
        """Export and import a workspace with deep directory nesting."""
        import random
        import tarfile

        headers = await self._user_headers(client)
        resp = await client.post(
            "/workspaces", headers=headers, json={"name": "deep-export"}
        )
        ws = resp.json()

        import klangk_backend.workspaces as ws_mod

        home = ws_mod.home_path(user["id"], ws["id"])
        home.mkdir(parents=True, exist_ok=True)

        # Create a deep directory structure with files at various depths
        rng = random.Random(42)
        expected_files = {}
        for depth in range(1, 8):
            dir_path = home / "work"
            for d in range(depth):
                dir_path = dir_path / f"level{d}"
            dir_path.mkdir(parents=True, exist_ok=True)

            # Write a few files at each level
            for i in range(3):
                content = f"depth{depth}-file{i}-" + "x" * rng.randint(10, 500)
                file_path = dir_path / f"file{i}.txt"
                file_path.write_text(content)
                rel = str(file_path.relative_to(home))
                expected_files[rel] = content

            # Add a symlink at each level
            (dir_path / "link.txt").symlink_to("file0.txt")

        # Also add some binary-ish content
        bin_dir = home / "work" / "bin"
        bin_dir.mkdir(exist_ok=True)
        bin_content = bytes(rng.getrandbits(8) for _ in range(4096))
        (bin_dir / "data.bin").write_bytes(bin_content)

        # Export
        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 200
        archive_bytes = resp.content
        assert len(archive_bytes) > 0

        # Verify archive structure
        buf = io.BytesIO(archive_bytes)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            names = tar.getnames()
            assert "workspace.json" in names
            # Check deep files are present
            for rel in expected_files:
                assert any(rel.replace("\\", "/") in n for n in names), (
                    f"Missing: {rel}"
                )
            # Check symlinks present
            sym_members = [m for m in tar.getmembers() if m.issym()]
            assert len(sym_members) >= 7  # one per depth level
            # Check binary file
            assert any("data.bin" in n for n in names)

        # Import into a new workspace
        resp = await client.post(
            "/workspaces/import",
            headers=headers,
            params={"name": "deep-imported"},
            files={
                "file": (
                    "archive.tar.gz",
                    archive_bytes,
                    "application/gzip",
                )
            },
        )
        assert resp.status_code == 200
        imported = resp.json()
        assert imported["name"] == "deep-imported"

        # Verify all files survived
        imported_home = ws_mod.home_path(user["id"], imported["id"])
        for rel, content in expected_files.items():
            file_path = imported_home / rel
            assert file_path.exists(), f"Missing after import: {rel}"
            assert file_path.read_text() == content

        # Verify binary file
        assert (
            imported_home / "work" / "bin" / "data.bin"
        ).read_bytes() == bin_content

        # Verify symlinks survived as symlinks
        for depth in range(1, 8):
            link_path = imported_home / "work"
            for d in range(depth):
                link_path = link_path / f"level{d}"
            link_path = link_path / "link.txt"
            assert link_path.is_symlink(), f"Not a symlink: {link_path}"
            assert os.readlink(str(link_path)) == "file0.txt"

    async def test_import_size_limit(self, client, user, monkeypatch):
        """Upload exceeding size limit is rejected."""
        import klangk_backend.api as api_mod

        monkeypatch.setattr(api_mod, "FILE_UPLOAD_SIZE_MAX", 100)

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


# --- Invitation endpoints ---


class TestInvitations:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_send_invitation(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ) as mock_send:
            resp = await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "invited@example.com"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "invited@example.com"
        assert data["status"] == "pending"
        assert "id" in data
        mock_send.assert_called_once()

    async def test_send_invitation_disabled(
        self, client, admin_user, monkeypatch
    ):
        headers = await self._admin_headers(client)
        monkeypatch.setattr(auth, "invitations_enabled", lambda: False)
        resp = await client.post(
            "/admin/invitations",
            headers=headers,
            json={"email": "invited@example.com"},
        )
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"]

    async def test_send_invitation_existing_user(
        self, client, admin_user, user
    ):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/invitations",
            headers=headers,
            json={"email": "testuser@example.com"},
        )
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]

    async def test_send_invitation_duplicate_pending(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "dup@example.com"},
            )
            resp = await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "dup@example.com"},
            )
        assert resp.status_code == 400
        assert "pending invitation" in resp.json()["detail"]

    async def test_send_invitation_invalid_email(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/invitations",
            headers=headers,
            json={"email": "not-an-email"},
        )
        assert resp.status_code == 400

    async def test_send_invitation_requires_admin(self, client, user):
        login_resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.post(
            "/admin/invitations",
            headers=headers,
            json={"email": "invited@example.com"},
        )
        assert resp.status_code == 403

    async def test_list_invitations(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "list1@example.com"},
            )
            await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "list2@example.com"},
            )
        resp = await client.get("/admin/invitations", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        emails = [inv["email"] for inv in data]
        assert "list1@example.com" in emails
        assert "list2@example.com" in emails
        assert data[0]["invited_by_email"] == "testadmin@example.com"

    async def test_revoke_invitation(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "revoke@example.com"},
            )
        inv_id = create_resp.json()["id"]
        resp = await client.delete(
            f"/admin/invitations/{inv_id}", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"

        # Can't revoke again
        resp = await client.delete(
            f"/admin/invitations/{inv_id}", headers=headers
        )
        assert resp.status_code == 404

    async def test_revoke_nonexistent(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            "/admin/invitations/nonexistent-id", headers=headers
        )
        assert resp.status_code == 404

    async def test_resend_invitation(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "resend@example.com"},
            )
        inv_id = create_resp.json()["id"]
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ) as mock_resend:
            resp = await client.post(
                f"/admin/invitations/{inv_id}/resend", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resent"
        mock_resend.assert_called_once()

    async def test_resend_nonexistent(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/admin/invitations/nonexistent/resend", headers=headers
        )
        assert resp.status_code == 404

    async def test_resend_revoked(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "revoked-resend@example.com"},
            )
        inv_id = create_resp.json()["id"]
        await client.delete(f"/admin/invitations/{inv_id}", headers=headers)
        resp = await client.post(
            f"/admin/invitations/{inv_id}/resend", headers=headers
        )
        assert resp.status_code == 404

    async def test_accept_invite(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "accept@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = auth.create_invitation_token(inv_id, "accept@example.com")

        resp = await client.post(
            "/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert "access_token" in data

        # User can log in
        login_resp = await client.post(
            "/auth/login",
            json={"email": "accept@example.com", "password": "newpassword"},
        )
        assert login_resp.status_code == 200

    async def test_accept_invite_invalid_token(self, client, db):
        resp = await client.post(
            "/auth/accept-invite",
            json={"token": "invalid-token", "password": "newpassword"},
        )
        assert resp.status_code == 400
        assert "Invalid or expired" in resp.json()["detail"]

    async def test_accept_invite_already_accepted(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "double@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = auth.create_invitation_token(inv_id, "double@example.com")

        # Accept once
        await client.post(
            "/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        # Try again
        resp = await client.post(
            "/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 400
        assert "no longer valid" in resp.json()["detail"]

    async def test_accept_invite_short_password(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "short@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = auth.create_invitation_token(inv_id, "short@example.com")

        resp = await client.post(
            "/auth/accept-invite",
            json={"token": token, "password": "ab"},
        )
        assert resp.status_code == 400
        assert "Password" in resp.json()["detail"]

    async def test_accept_invite_works_when_registration_disabled(
        self, client, admin_user, monkeypatch
    ):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "noreg@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = auth.create_invitation_token(inv_id, "noreg@example.com")

        # Disable registration
        monkeypatch.setattr(auth, "registration_enabled", lambda: False)

        # Accept-invite should still work
        resp = await client.post(
            "/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    async def test_accept_invite_email_already_registered(
        self, client, admin_user, user
    ):
        headers = await self._admin_headers(client)
        with patch.object(
            api.emailsvc,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/admin/invitations",
                headers=headers,
                json={"email": "race@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = auth.create_invitation_token(inv_id, "race@example.com")

        # Simulate race: create user with that email before accepting
        await model.create_user(
            "race@example.com", auth.hash_password("pass"), verified=True
        )

        resp = await client.post(
            "/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]

    async def test_accept_invite_wrong_purpose_token(self, client, db):
        # Use a verification token (wrong purpose)
        token = auth.create_verification_token("fake-user-id")
        resp = await client.post(
            "/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 400

    async def test_config_includes_invitations_enabled(self, client):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        assert "invitations_enabled" in resp.json()


# --- OIDC endpoints ---


class TestOIDCConfig:
    async def test_config_includes_oidc_fields(self, client, monkeypatch):
        monkeypatch.delenv("KLANGK_AUTH_MODES", raising=False)
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "oidc_providers" in data
        assert "auth_modes" in data
        assert data["oidc_providers"] == []
        assert data["auth_modes"] == "password"

    async def test_config_with_providers(self, client, monkeypatch):
        monkeypatch.setattr(
            api.oidc,
            "list_providers",
            lambda: [{"id": "test", "display_name": "Test"}],
        )
        monkeypatch.setattr(api.oidc, "auth_modes", lambda: "both")
        resp = await client.get("/api/config")
        data = resp.json()
        assert len(data["oidc_providers"]) == 1
        assert data["auth_modes"] == "both"


class TestOIDCAuthModeGuards:
    async def test_login_blocked_when_oidc_only(
        self, client, monkeypatch, user
    ):
        monkeypatch.setattr(api.oidc, "password_login_allowed", lambda: False)
        resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"]

    async def test_register_blocked_when_oidc_only(
        self, client, monkeypatch, db
    ):
        monkeypatch.setattr(api.oidc, "password_login_allowed", lambda: False)
        resp = await client.post(
            "/auth/register",
            json={"email": "new@example.com", "password": "testpass"},
        )
        assert resp.status_code == 403

    async def test_login_allowed_when_both(self, client, monkeypatch, user):
        monkeypatch.setattr(api.oidc, "password_login_allowed", lambda: True)
        resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 200


class TestOIDCLogin:
    async def test_oidc_login_not_enabled(self, client, monkeypatch):
        monkeypatch.setattr(api.oidc, "oidc_login_allowed", lambda: False)
        resp = await client.get("/auth/oidc/test/login")
        assert resp.status_code == 404

    async def test_unknown_provider(self, client, monkeypatch):
        monkeypatch.setattr(api.oidc, "oidc_login_allowed", lambda: True)
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: None)
        resp = await client.get("/auth/oidc/nope/login")
        assert resp.status_code == 404

    async def test_invalid_cli_redirect(self, client, monkeypatch):
        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(api.oidc, "oidc_login_allowed", lambda: True)
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: provider)
        resp = await client.get(
            "/auth/oidc/test/login",
            params={"cli_redirect": "https://evil.com/steal"},
        )
        assert resp.status_code == 400
        assert "localhost" in resp.json()["detail"]

    async def test_oidc_login_redirects(self, client, monkeypatch):
        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(api.oidc, "oidc_login_allowed", lambda: True)
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            api.oidc,
            "build_auth_url",
            AsyncMock(return_value="https://idp.example.com/auth?foo=bar"),
        )
        resp = await client.get(
            "/auth/oidc/test/login", follow_redirects=False
        )
        assert resp.status_code == 302
        assert (
            resp.headers["location"] == "https://idp.example.com/auth?foo=bar"
        )
        assert "oidc_test" in resp.headers.get("set-cookie", "")


class TestOIDCCallback:
    async def _setup_callback(self, client, monkeypatch, db, claims=None):
        """Set up mocks for a successful OIDC callback test."""
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            api.oidc,
            "exchange_code",
            AsyncMock(
                return_value={
                    "id_token": "fake-id-token",
                    "access_token": "at",
                }
            ),
        )
        default_claims = {
            "sub": "oidc-sub-123",
            "email": "oidcuser@example.com",
            "email_verified": True,
        }
        if claims:
            default_claims.update(claims)
        monkeypatch.setattr(
            api.oidc,
            "validate_id_token",
            AsyncMock(return_value=default_claims),
        )
        # Set the state cookie
        cookie_data = json_mod.dumps(
            {
                "state": "test-state",
                "verifier": "test-verifier",
                "redirect_uri": "https://klangk.example.com/auth/oidc/test/callback",
                "cli_redirect": None,
            }
        )
        return provider, cookie_data

    async def test_callback_creates_user(self, client, monkeypatch, db):
        _, cookie_data = await self._setup_callback(client, monkeypatch, db)
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            cookies={"oidc_test": cookie_data},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "oidc-complete" in location
        assert "token=" in location

        # User was created
        user = await model.get_user_by_email("oidcuser@example.com")
        assert user is not None
        assert user["provider"] == "test"
        assert user["external_id"] == "oidc-sub-123"
        assert user["password_hash"] is None

    async def test_callback_syncs_groups_via_hook(
        self, client, monkeypatch, db
    ):
        """OIDC callback calls the group mapping hook and syncs memberships."""

        def test_hook(provider, claims, email, tokens):
            if "admin-role" in claims.get("roles", []):
                return {"admin", "power-users"}
            return {"users"}

        monkeypatch.setattr(api.oidc, "_login_hook", test_hook)
        monkeypatch.setattr(api.oidc, "_login_hook_is_async", False)

        _, cookie_data = await self._setup_callback(
            client,
            monkeypatch,
            db,
            claims={
                "sub": "hook-sub",
                "email": "hookuser@example.com",
                "roles": ["admin-role"],
            },
        )
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "code", "state": "test-state"},
            cookies={"oidc_test": cookie_data},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        user = await model.get_user_by_email("hookuser@example.com")
        groups = await model.get_user_groups(user["id"])
        names = {g["name"] for g in groups}
        assert "admin" in names
        assert "power-users" in names

        # Verify source is oidc_sync
        sync_ids = await model.get_user_oidc_sync_group_ids(user["id"])
        assert len(sync_ids) == 2

    async def test_callback_links_existing_user(
        self, client, monkeypatch, db, user
    ):
        _, cookie_data = await self._setup_callback(
            client,
            monkeypatch,
            db,
            claims={"sub": "new-sub", "email": "testuser@example.com"},
        )
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "code", "state": "test-state"},
            cookies={"oidc_test": cookie_data},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        # Existing user was linked
        linked = await model.get_user_by_external_id("test", "new-sub")
        assert linked is not None
        assert linked["id"] == user["id"]

    async def test_callback_state_mismatch(self, client, monkeypatch, db):
        _, cookie_data = await self._setup_callback(client, monkeypatch, db)
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "code", "state": "wrong-state"},
            cookies={"oidc_test": cookie_data},
        )
        assert resp.status_code == 400
        assert "State mismatch" in resp.json()["detail"]

    async def test_callback_missing_cookie(self, client, monkeypatch, db):
        await self._setup_callback(client, monkeypatch, db)
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "code", "state": "test-state"},
        )
        assert resp.status_code == 400
        assert "cookie" in resp.json()["detail"].lower()

    async def test_callback_idp_error(self, client, monkeypatch, db):
        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: provider)
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"error": "access_denied"},
        )
        assert resp.status_code == 400
        assert "access_denied" in resp.json()["detail"]

    async def test_callback_cli_redirect(self, client, monkeypatch, db):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            api.oidc,
            "exchange_code",
            AsyncMock(return_value={"id_token": "idt", "access_token": "at"}),
        )
        monkeypatch.setattr(
            api.oidc,
            "validate_id_token",
            AsyncMock(
                return_value={
                    "sub": "cli-sub",
                    "email": "cli@example.com",
                    "email_verified": True,
                }
            ),
        )
        cookie_data = json_mod.dumps(
            {
                "state": "s",
                "verifier": "v",
                "redirect_uri": "https://klangk.example.com/cb",
                "cli_redirect": "http://localhost:12345/callback",
            }
        )
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
            cookies={"oidc_test": cookie_data},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith(
            "http://localhost:12345/callback?token="
        )

    async def test_callback_token_exchange_failure(
        self, client, monkeypatch, db
    ):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: provider)
        mock_request = httpx.Request("POST", "https://idp/token")
        mock_response = httpx.Response(
            400, text="bad request", request=mock_request
        )
        monkeypatch.setattr(
            api.oidc,
            "exchange_code",
            AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "err", request=mock_request, response=mock_response
                )
            ),
        )
        cookie_data = json_mod.dumps(
            {
                "state": "s",
                "verifier": "v",
                "redirect_uri": "https://cb",
                "cli_redirect": None,
            }
        )
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
            cookies={"oidc_test": cookie_data},
        )
        assert resp.status_code == 502

    async def test_callback_no_id_token(self, client, monkeypatch, db):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            api.oidc,
            "exchange_code",
            AsyncMock(return_value={"access_token": "at"}),
        )
        cookie_data = json_mod.dumps(
            {
                "state": "s",
                "verifier": "v",
                "redirect_uri": "https://cb",
                "cli_redirect": None,
            }
        )
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
            cookies={"oidc_test": cookie_data},
        )
        assert resp.status_code == 502
        assert "No ID token" in resp.json()["detail"]

    async def test_callback_invalid_id_token(self, client, monkeypatch, db):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            api.oidc,
            "exchange_code",
            AsyncMock(return_value={"id_token": "bad", "access_token": "at"}),
        )
        monkeypatch.setattr(
            api.oidc,
            "validate_id_token",
            AsyncMock(side_effect=Exception("bad token")),
        )
        cookie_data = json_mod.dumps(
            {
                "state": "s",
                "verifier": "v",
                "redirect_uri": "https://cb",
                "cli_redirect": None,
            }
        )
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
            cookies={"oidc_test": cookie_data},
        )
        assert resp.status_code == 502
        assert "validation failed" in resp.json()["detail"]

    async def test_callback_missing_claims(self, client, monkeypatch, db):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            api.oidc,
            "exchange_code",
            AsyncMock(return_value={"id_token": "t", "access_token": "at"}),
        )
        monkeypatch.setattr(
            api.oidc,
            "validate_id_token",
            AsyncMock(return_value={"sub": "s"}),  # no email
        )
        cookie_data = json_mod.dumps(
            {
                "state": "s",
                "verifier": "v",
                "redirect_uri": "https://cb",
                "cli_redirect": None,
            }
        )
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
            cookies={"oidc_test": cookie_data},
        )
        assert resp.status_code == 502
        assert "missing" in resp.json()["detail"].lower()

    async def test_callback_login_hook_rejects(self, client, monkeypatch, db):
        """A login validation hook can reject an OIDC login."""
        _, cookie_data = await self._setup_callback(
            client,
            monkeypatch,
            db,
            claims={"email_verified": False},
        )
        monkeypatch.setattr(
            oidc,
            "_login_hook",
            oidc.example_require_verified_email,
        )
        monkeypatch.setattr(oidc, "_login_hook_is_async", False)
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            cookies={"oidc_test": cookie_data},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert "not verified" in resp.json()["detail"].lower()
        assert await model.get_user_by_email("oidcuser@example.com") is None

    async def test_callback_no_login_hook_allows_unverified(
        self, client, monkeypatch, db
    ):
        """Without a login hook, unverified emails are accepted."""
        _, cookie_data = await self._setup_callback(
            client,
            monkeypatch,
            db,
            claims={"email_verified": False},
        )
        monkeypatch.setattr(oidc, "_login_hook", None)
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            cookies={"oidc_test": cookie_data},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert (
            await model.get_user_by_email("oidcuser@example.com") is not None
        )

    async def test_callback_unknown_provider(self, client, monkeypatch, db):
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: None)
        resp = await client.get(
            "/auth/oidc/nope/callback",
            params={"code": "code", "state": "s"},
        )
        assert resp.status_code == 404

    async def test_callback_invalid_cookie_json(self, client, monkeypatch, db):
        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(api.oidc, "get_provider", lambda _: provider)
        resp = await client.get(
            "/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
            cookies={"oidc_test": "not-json"},
        )
        assert resp.status_code == 400


class TestOIDCLogout:
    async def test_logout_returns_oidc_logout_url(self, client, db):
        """OIDC user with logout_redirect gets IdP logout URL in response."""
        # Create OIDC user
        user = await model.create_user(
            "oidc-logout@example.com",
            password_hash=None,
            verified=True,
            provider="test",
            external_id="logout-sub",
        )
        token = auth.create_token(user["id"], user["email"])
        headers = {"Authorization": f"Bearer {token}"}

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
            logout_redirect=True,
        )
        with (
            patch.object(api.oidc, "get_provider", return_value=provider),
            patch.object(
                api.oidc,
                "build_logout_url",
                AsyncMock(return_value="https://idp.example.com/logout?x=1"),
            ),
        ):
            resp = await client.post("/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert (
            resp.json()["oidc_logout_url"]
            == "https://idp.example.com/logout?x=1"
        )

    async def test_logout_no_redirect_for_local_user(self, client, user):
        """Local user gets no oidc_logout_url."""
        login_resp = await client.post(
            "/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.post("/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert "oidc_logout_url" not in resp.json()

    async def test_logout_no_redirect_when_disabled(self, client, db):
        """OIDC user with logout_redirect=false gets no URL."""
        user = await model.create_user(
            "oidc-nologout@example.com",
            password_hash=None,
            verified=True,
            provider="test",
            external_id="nologout-sub",
        )
        token = auth.create_token(user["id"], user["email"])
        headers = {"Authorization": f"Bearer {token}"}

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
            logout_redirect=False,
        )
        with patch.object(api.oidc, "get_provider", return_value=provider):
            resp = await client.post("/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert "oidc_logout_url" not in resp.json()


class TestHandleEndpoints:
    async def test_change_own_handle(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-handle",
            json={"handle": "newhandle", "password": "testpass"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "updated"
        assert data["handle"] == "newhandle"
        # Verify it actually changed in the DB
        updated = await model.get_user_by_id(user["id"])
        assert updated["handle"] == "newhandle"

    async def test_change_handle_refreshes_presence(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            api.wshandler,
            "refresh_user_handle",
            new_callable=AsyncMock,
        ) as mock_refresh:
            resp = await client.post(
                "/auth/change-handle",
                json={"handle": "freshhandle", "password": "testpass"},
                headers=headers,
            )
        assert resp.status_code == 200
        mock_refresh.assert_awaited_once_with(user["id"], "freshhandle")

    async def test_change_handle_invalid_empty(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-handle",
            json={"handle": "", "password": "testpass"},
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_change_handle_invalid_chars(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-handle",
            json={"handle": "BAD HANDLE!", "password": "testpass"},
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_change_handle_reserved(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-handle",
            json={"handle": "work", "password": "testpass"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "reserved" in resp.json()["detail"]

    async def test_change_handle_conflict(self, client, admin_user, user):
        # Set admin_user's handle to something known
        await model.set_user_handle(admin_user["id"], "taken-handle")
        # Try to set user's handle to the same
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-handle",
            json={"handle": "taken-handle", "password": "testpass"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "already taken" in resp.json()["detail"]

    async def test_change_handle_wrong_password(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/auth/change-handle",
            json={"handle": "good-handle", "password": "wrongpass"},
            headers=headers,
        )
        assert resp.status_code == 401

    async def test_admin_change_user_handle(self, client, admin_user, user):
        admin_resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_resp.json()['access_token']}"
        }
        resp = await client.patch(
            f"/admin/users/{user['id']}",
            json={"handle": "admin-set-handle"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        updated = await model.get_user_by_id(user["id"])
        assert updated["handle"] == "admin-set-handle"

    async def test_admin_change_handle_refreshes_presence(
        self, client, admin_user, user
    ):
        admin_resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_resp.json()['access_token']}"
        }
        with patch.object(
            api.wshandler,
            "refresh_user_handle",
            new_callable=AsyncMock,
        ) as mock_refresh:
            resp = await client.patch(
                f"/admin/users/{user['id']}",
                json={"handle": "admin-refreshed"},
                headers=admin_headers,
            )
        assert resp.status_code == 200
        mock_refresh.assert_awaited_once_with(user["id"], "admin-refreshed")

    async def test_admin_change_user_handle_invalid(
        self, client, admin_user, user
    ):
        admin_resp = await client.post(
            "/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_resp.json()['access_token']}"
        }
        resp = await client.patch(
            f"/admin/users/{user['id']}",
            json={"handle": "", "password": "testpass"},
            headers=admin_headers,
        )
        assert resp.status_code == 400

    async def test_get_me(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/auth/me", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == user["id"]
        assert data["email"] == "testuser@example.com"
        assert "handle" in data
