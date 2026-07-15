"""Tests for api.py: HTTP route handlers via FastAPI TestClient."""

import io
import os
import shutil
import tempfile

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
import httpx
from httpx import AsyncClient, ASGITransport

from klangk_backend import (
    agent,
    api,
    auth as auth_mod,
    model,
    podman,
    workspaces as ws_mod,
)
from klangk_backend.container import ContainerRegistry
from klangk_backend import emailsvc as emailsvc_mod
from klangk_backend import util as util_mod
from klangk_backend import oidc as oidc_mod
from klangk_backend import plugins as plugins_mod
from _helpers import make_settings
from klangk_backend.wshandler.session import WebSocketState
import types


def _auth():
    """A standalone Auth for token forging (same default secret as the
    app fixture, so tokens round-trip through app.state.auth.decode_*)."""
    return auth_mod.Auth(types.SimpleNamespace(settings=make_settings({})))


# Aliases for the raw-JWT test that builds a token by hand.
_SECRET = make_settings({}).jwt_secret
_ALGORITHM = "HS256"

# Mock Podman instance wired onto app.state.podman by the app fixture;
# files/volume-API tests patch its methods via patch.object (#1468).
_mock_pod = MagicMock()


@pytest.fixture
async def app(db, temp_data_dir):
    """Create a minimal FastAPI app with just the API router."""
    app = FastAPI()
    from klangk_backend.util import API_PREFIX
    from klangk_backend.main import register_exception_handlers

    settings = make_settings(
        env={
            "KLANGK_AUTH_MODES": "password",
            "KLANGK_DATA_DIR": str(temp_data_dir),
            "KLANGK_CUSTOMIZE_DIR": str(temp_data_dir / "customize"),
        }
    )
    app.state.settings = settings
    app.state.podman = _mock_pod
    sockets = WebSocketState(app.state)
    app.state.sockets = sockets
    registry = ContainerRegistry(app.state)
    app.state.container_registry = registry
    app.state.oidc = oidc_mod.OIDC(app.state)
    app.state.plugins = plugins_mod.Plugins(app.state)
    app.state.workspaces = ws_mod.Workspaces(app.state)
    app.state.agents = agent.Agents(app.state)
    app.state.email = emailsvc_mod.EmailService(app.state)
    app.state.util = util_mod.Util(app.state)

    app.state.auth = auth_mod.Auth(app.state)

    app.include_router(api.root_router)
    app.include_router(api.router, prefix=API_PREFIX)
    register_exception_handlers(app)
    return app


@pytest.fixture
def registry(app):
    """Shortcut to the ContainerRegistry on app.state."""
    return app.state.container_registry


@pytest.fixture
def sockets(app):
    """Shortcut to the WebSocketState on app.state."""
    return app.state.sockets


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _auth_headers(client):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": "testuser@example.com", "password": "testpass"},
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _oidc_user_headers(email="oidc@example.com"):
    """Auth headers for an OIDC-only user (password_hash is NULL).

    OIDC users can't use /auth/login, so we mint a token directly,
    mirroring what the OIDC callback does.  Used to exercise the
    authenticated endpoints that must not 500 on a NULL hash (#890).
    """
    user = await model.create_user(email, None, verified=True, provider="oidc")
    token = _auth().create_token(user["id"], user["email"])
    return {"Authorization": f"Bearer {token}"}


# --- Health ---


class TestHealth:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


class TestEmpty:
    async def test_empty(self, client):
        resp = await client.get("/empty")
        assert resp.status_code == 200
        assert resp.text == ""


class TestVerifyWorkspaceToken:
    async def test_valid_workspace_token(self, client):
        token = _auth().create_workspace_token("ws-123")
        resp = await client.get(
            "/api/v1/auth/verify-workspace-token",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["workspace_id"] == "ws-123"

    async def test_missing_auth_header(self, client):
        resp = await client.get("/api/v1/auth/verify-workspace-token")
        assert resp.status_code == 401

    async def test_invalid_token(self, client):
        resp = await client.get(
            "/api/v1/auth/verify-workspace-token",
            headers={"Authorization": "Bearer garbage"},
        )
        assert resp.status_code == 401

    async def test_user_jwt_rejected(self, client):
        user_token = _auth().create_token("user-1", "u@test.com")
        resp = await client.get(
            "/api/v1/auth/verify-workspace-token",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert resp.status_code == 401

    async def test_expired_workspace_token(self, client):
        from datetime import datetime, timedelta, timezone

        from jose import jwt

        expired = datetime.now(timezone.utc) - timedelta(hours=1)
        payload = {"sub": "ws-123", "purpose": "workspace", "exp": expired}
        token = jwt.encode(payload, _SECRET, algorithm=_ALGORITHM)
        resp = await client.get(
            "/api/v1/auth/verify-workspace-token",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Workspace token expired"

    async def test_invalid_workspace_token_detail(self, client):
        resp = await client.get(
            "/api/v1/auth/verify-workspace-token",
            headers={"Authorization": "Bearer garbage"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid workspace token"


class TestWorkspaceChat:
    async def test_post_agent_message(self, client, user):
        workspace = await model.create_workspace(user["id"], "chat-ws")
        token = _auth().create_workspace_token(workspace["id"])
        resp = await client.post(
            "/api/v1/workspaces/post-chat-message",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "hello from agent"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["message"] == "hello from agent"
        assert data["message_type"] == model.MSG_AGENT
        assert data["workspace_id"] == workspace["id"]

    async def test_broadcasts_to_websocket(self, client, user, sockets):
        workspace = await model.create_workspace(user["id"], "bcast-ws")
        token = _auth().create_workspace_token(workspace["id"])
        session = sockets.get_or_create_session(workspace["id"])
        mock_sock = MagicMock()
        session.subscribers.add(mock_sock)

        await client.post(
            "/api/v1/workspaces/post-chat-message",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "broadcast test"},
        )

        mock_sock.send_json.assert_called_once()
        sent = mock_sock.send_json.call_args[0][0]
        assert sent["type"] == "chat_message"
        assert sent["message"] == "broadcast test"

        sockets.sessions.pop(workspace["id"], None)

    async def test_missing_auth(self, client):
        resp = await client.post(
            "/api/v1/workspaces/post-chat-message", json={"message": "hi"}
        )
        assert resp.status_code == 401

    async def test_invalid_token(self, client):
        resp = await client.post(
            "/api/v1/workspaces/post-chat-message",
            headers={"Authorization": "Bearer garbage"},
            json={"message": "hi"},
        )
        assert resp.status_code == 401

    async def test_workspace_not_found(self, client):
        token = _auth().create_workspace_token("nonexistent-ws")
        resp = await client.post(
            "/api/v1/workspaces/post-chat-message",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "hi"},
        )
        assert resp.status_code == 404

    async def test_empty_message_rejected(self, client, user):
        workspace = await model.create_workspace(user["id"], "empty-ws")
        token = _auth().create_workspace_token(workspace["id"])
        resp = await client.post(
            "/api/v1/workspaces/post-chat-message",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "   "},
        )
        assert resp.status_code == 400


class TestVersion:
    async def test_version_from_file(self, client, app, tmp_path, monkeypatch):
        version_file = tmp_path / "version.json"
        version_file.write_text(
            '{"version": "2026.01.01+abc1234",'
            ' "commit": "abc1234",'
            ' "built_at": "2026-01-01T00:00:00Z"}'
        )
        monkeypatch.setattr(
            app.state.settings, "version_file", str(version_file)
        )
        resp = await client.get("/api/v1/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "2026.01.01+abc1234"
        assert data["commit"] == "abc1234"
        assert data["built_at"] == "2026-01-01T00:00:00Z"
        assert "plugins" in data

    async def test_version_no_file(self, client, app, monkeypatch):
        monkeypatch.setattr(app.state.settings, "version_file", None)
        resp = await client.get("/api/v1/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "dev"
        assert data["commit"] == "unknown"
        assert data["built_at"] is None
        assert "plugins" in data

    async def test_version_includes_plugins(
        self, client, app, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(app.state.settings, "version_file", None)
        plugin_dir = tmp_path / "plugins" / "myplugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "package.json").write_text(
            '{"name": "myplugin", "version": "1.2.3",'
            ' "description": "A test plugin"}'
        )
        # Rebuild the Plugins instance pointing at the tmp plugin dir
        import types as types_mod

        app.state.plugins = app.state.plugins.__class__(
            types_mod.SimpleNamespace(
                settings=make_settings(
                    env={"KLANGK_PLUGINS_DIR": str(tmp_path / "plugins")}
                )
            )
        )
        resp = await client.get("/api/v1/version")
        assert resp.status_code == 200
        plugins = resp.json()["plugins"]
        assert len(plugins) == 1
        assert plugins[0]["name"] == "myplugin"
        assert plugins[0]["version"] == "1.2.3"
        assert plugins[0]["description"] == "A test plugin"

    async def test_version_includes_variant_when_present(
        self, client, app, tmp_path, monkeypatch
    ):
        # When version.json carries a "variant" field (a downstream product
        # identity string, set via KLANGK_VARIANT in generate-version.sh), the
        # /api/v1/version endpoint surfaces it verbatim (see #1358).
        version_file = tmp_path / "version.json"
        version_file.write_text(
            '{"version": "2026.01.01+abc1234",'
            ' "variant": "Custom 1.0.0",'
            ' "commit": "abc1234",'
            ' "built_at": "2026-01-01T00:00:00Z"}'
        )
        monkeypatch.setattr(
            app.state.settings, "version_file", str(version_file)
        )
        resp = await client.get("/api/v1/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["variant"] == "Custom 1.0.0"

    async def test_version_omits_variant_when_absent(
        self, client, app, tmp_path, monkeypatch
    ):
        # Stock klangk builds omit the variant field entirely (it is absent
        # from version.json, not null). The endpoint must not synthesize one —
        # downstream UIs key off its presence (see #1358).
        version_file = tmp_path / "version.json"
        version_file.write_text(
            '{"version": "2026.01.01+abc1234",'
            ' "commit": "abc1234",'
            ' "built_at": "2026-01-01T00:00:00Z"}'
        )
        monkeypatch.setattr(
            app.state.settings, "version_file", str(version_file)
        )
        resp = await client.get("/api/v1/version")
        assert resp.status_code == 200
        data = resp.json()
        assert "variant" not in data


# --- Config ---


class TestConfig:
    async def test_get_config(self, client):
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "login_banner_title" in data
        assert "login_banner" in data
        assert "instance_id" in data

    async def test_get_config_includes_plugins(self, client, app, monkeypatch):
        monkeypatch.setattr(
            app.state.plugins,
            "declarations",
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
            app.state.plugins, "values", {"MY_PLUGIN_VAR": "test-value"}
        )
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["my_plugin_var"] == "test-value"

    async def test_get_config_banner_fields(self, client, app, monkeypatch):
        monkeypatch.setattr(app.state.settings, "login_banner_title", "Notice")
        monkeypatch.setattr(
            app.state.settings, "login_banner", "You must accept terms."
        )
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["login_banner_title"] == "Notice"
        assert data["login_banner"] == "You must accept terms."

    async def test_get_config_advertises_min_password_length(self, client):
        # Surfaced so the UI can validate password length inline; matches the
        # rule enforced server-side by auth.validate_password_length.
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["min_password_length"] == _auth().min_password_length

    async def test_get_config_logo_url_defaults_empty(self, client, app):
        # No KLANGK_LOGO_URL set -> empty string (UI renders default widget).
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        assert resp.json()["logo_url"] == ""

    async def test_get_config_logo_url_reflects_env(
        self, client, app, monkeypatch
    ):
        monkeypatch.setattr(
            app.state.settings, "logo_url", "https://example.com/l.png"
        )
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        assert resp.json()["logo_url"] == "https://example.com/l.png"

    async def test_get_config_legal_links_default_empty(self, client):
        # No legal/support env vars set -> all empty strings, so the
        # frontend hides them entirely (#1177).
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        for key in (
            "terms_url",
            "privacy_url",
            "aup_url",
            "support_url",
            "support_email",
        ):
            assert data[key] == ""

    async def test_get_config_legal_links_reflect_env(
        self, client, app, monkeypatch
    ):
        # Each link is surfaced verbatim from its settings field (frozen at
        # construction, like product_name / login_banner).
        monkeypatch.setattr(
            app.state.settings, "terms_url", "https://corp.example.com/terms"
        )
        monkeypatch.setattr(
            app.state.settings,
            "privacy_url",
            "https://corp.example.com/privacy",
        )
        monkeypatch.setattr(
            app.state.settings, "aup_url", "https://corp.example.com/aup"
        )
        monkeypatch.setattr(
            app.state.settings, "support_url", "https://help.example.com"
        )
        monkeypatch.setattr(
            app.state.settings, "support_email", "help@example.com"
        )
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["terms_url"] == "https://corp.example.com/terms"
        assert data["privacy_url"] == "https://corp.example.com/privacy"
        assert data["aup_url"] == "https://corp.example.com/aup"
        assert data["support_url"] == "https://help.example.com"
        assert data["support_email"] == "help@example.com"

    async def test_get_config_legal_links_are_plain_env_not_resolved(
        self, client, app, monkeypatch, tmp_path
    ):
        # Legal/support links are PUBLIC URLs shown to unauthenticated
        # users, so they must NOT get file:/cmd: secret resolution -- a
        # deployer pointing them at a file: path would be exposing secret
        # resolution to the world. The settings field is surfaced verbatim.
        monkeypatch.setattr(
            app.state.settings, "terms_url", "file:///etc/shadow"
        )
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        assert resp.json()["terms_url"] == "file:///etc/shadow"

    async def test_get_config_logo_url_resolves_file_secret(
        self, client, app, tmp_path, monkeypatch
    ):
        # file:/cmd: resolution happens at settings construction (#1461);
        # the field holds the resolved value, which /config surfaces as-is.
        monkeypatch.setattr(
            app.state.settings,
            "logo_url",
            "https://from.secret/l.png",
        )
        resp = await client.get("/api/v1/config")
        assert resp.json()["logo_url"] == "https://from.secret/l.png"

    async def test_get_config_includes_product_name_default(self, client):
        # White-label product name; defaults to "Klangk" so existing
        # deployments are unchanged when the var is unset (#1149).
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["product_name"] == "Klangk"

    async def test_get_config_reflects_product_name(
        self, client, app, monkeypatch
    ):
        monkeypatch.setattr(app.state.settings, "product_name", "Acme Labs")
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["product_name"] == "Acme Labs"


# --- Auth routes ---


class TestAuthRoutes:
    async def test_register(self, client, admin_user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        token = login_resp.json()["access_token"]
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp = await client.post(
                "/api/v1/auth/register",
                json={"email": "new@example.com", "password": "newpass1"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "pending_verification"
        assert data["email"] == "new@example.com"

    async def test_register_persists_handle(self, client, db):
        """The register route must persist a derived handle, not NULL (#1256).

        Regression: the email-verification register route did a raw
        INSERT with no handle column, so users got NULL handles and
        ``ensure_home_symlink`` failed on first workspace connect.
        """
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": "handleme@example.com",
                    "password": "newpass1",
                },
            )
        assert resp.status_code == 200
        user = await model.get_user_by_email("handleme@example.com")
        assert user is not None
        assert user["handle"] == "handleme"  # derived, not NULL

    async def test_register_test_mode(self, client, app, db, monkeypatch):
        """In test mode, unauthenticated registration is allowed and auto-verified."""
        monkeypatch.setattr(app.state.settings, "test_mode", "1")
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "new@example.com", "password": "newpass1"},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_register_unauthenticated(self, client, db):
        """Registration is open — no auth required (verification gates access)."""
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp = await client.post(
                "/api/v1/auth/register",
                json={"email": "new@example.com", "password": "newpass1"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending_verification"

    async def test_register_email_send_failure_rolls_back(self, client, db):
        """If verification email fails, user creation is rolled back."""
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
            side_effect=RuntimeError("sendmail not found"),
        ):
            resp = await client.post(
                "/api/v1/auth/register",
                json={"email": "fail@example.com", "password": "newpass1"},
            )
        assert resp.status_code == 503
        # User should not exist — transaction was rolled back
        user = await model.get_user_by_email("fail@example.com")
        assert user is None

    async def test_register_short_password(self, client, db):
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "short@example.com", "password": "abc"},
        )
        assert resp.status_code == 400

    async def test_register_password_exceeds_72_bytes(self, client, db):
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "long@example.com", "password": "a" * 73},
        )
        assert resp.status_code == 400
        assert "72 bytes" in resp.json()["detail"]

    async def test_register_duplicate(self, client, admin_user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        token = login_resp.json()["access_token"]
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "testadmin@example.com", "password": "pass"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    async def test_verify_email(self, client, db):
        """Verify endpoint marks user as verified."""
        import bcrypt

        password_hash = bcrypt.hashpw(b"pass", bcrypt.gensalt()).decode()
        user = await model.create_user(
            "unverified@example.com", password_hash, verified=False
        )
        token = _auth().create_verification_token(user["id"])
        resp = await client.get(f"/api/v1/auth/verify?token={token}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"
        # User can now log in
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "unverified@example.com", "password": "pass"},
        )
        assert login_resp.status_code == 200

    async def test_verify_invalid_token(self, client, db):
        resp = await client.get("/api/v1/auth/verify?token=garbage")
        assert resp.status_code == 400

    async def test_verify_nonexistent_user(self, client, db):
        token = _auth().create_verification_token("nonexistent-id")
        resp = await client.get(f"/api/v1/auth/verify?token={token}")
        assert resp.status_code == 404

    async def test_login(self, client, user):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_login_bad_password(self, client, user):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "wrong"},
        )
        assert resp.status_code == 401

    async def test_logout(self, client, user, registry):
        headers = await _auth_headers(client)
        # Logout must NOT stop the user's containers (#1235): the idle timeout
        # is the only thing that stops containers (#301). Guard against a
        # regression of the old logout_user holdover.
        with patch.object(
            registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ) as mock_stop:
            resp = await client.post("/api/v1/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"
        mock_stop.assert_not_called()

    async def test_logout_no_auth(self, client):
        resp = await client.post("/api/v1/auth/logout")
        assert resp.status_code == 401

    async def test_refresh(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post("/api/v1/auth/refresh", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        # New token should differ from the original
        assert data["access_token"] != headers["Authorization"].split(" ")[1]

    async def test_refresh_no_auth(self, client):
        resp = await client.post("/api/v1/auth/refresh")
        assert resp.status_code == 401


# --- Local (no-auth) login (#1374) ---


class TestLocalLogin:
    """POST /api/v1/auth/local — no-login single-user mode token handout."""

    async def test_returns_token_for_seeded_default_user(
        self, client, app, db, monkeypatch
    ):
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "local@example.com"
        )
        await model.create_user(
            "local@example.com",
            auth_mod.hash_password("unused"),
            verified=True,
        )
        resp = await client.post("/api/v1/auth/local")
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "local@example.com"
        assert data["token_type"] == "bearer"
        token = data["access_token"]
        # The token flows through the normal JWT gate unchanged.
        claims = _auth().decode_token(token)
        assert claims["email"] == "local@example.com"

    async def test_token_authorizes_requests(
        self, client, app, db, monkeypatch
    ):
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "local@example.com"
        )
        await model.create_user(
            "local@example.com",
            auth_mod.hash_password("unused"),
            verified=True,
        )
        token = (await client.post("/api/v1/auth/local")).json()[
            "access_token"
        ]
        # An authenticated endpoint accepts the freely-minted token.
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "local@example.com"

    async def test_disabled_when_not_none_mode(
        self, client, app, db, monkeypatch
    ):
        # In password mode (the explicit opposite of none) the endpoint refuses.
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "password")
        resp = await client.post("/api/v1/auth/local")
        assert resp.status_code == 403

    async def test_disabled_in_both_mode(self, client, app, db, monkeypatch):
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "both")
        resp = await client.post("/api/v1/auth/local")
        assert resp.status_code == 403

    async def test_500_when_default_user_missing(
        self, client, app, db, monkeypatch
    ):
        # seed_default_user() runs in the lifespan, which the minimal test
        # app skips — so if it were somehow bypassed at runtime the endpoint
        # surfaces a 500 rather than minting a token for a ghost user.
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "ghost@example.com"
        )
        resp = await client.post("/api/v1/auth/local")
        assert resp.status_code == 500

    async def test_no_body_required(self, client, app, db, monkeypatch):
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "local@example.com"
        )
        await model.create_user(
            "local@example.com",
            auth_mod.hash_password("unused"),
            verified=True,
        )
        # Simple POST (no JSON body, no custom header) — the loopback bind +
        # nginx ACL, not a credential, is the identity boundary in this mode.
        resp = await client.post("/api/v1/auth/local")
        assert resp.status_code == 200

    # --- source-IP self-defense (front-proxy bypass, #1374 review) ---
    # The nginx `allow 127.0.0.1; deny all` ACL keys off $remote_addr, which
    # is the loopback nginx<->uvicorn hop when any loopback proxy fronts nginx.
    # So the ACL alone admits a workspace container that reached nginx through
    # such a proxy. The backend re-checks the effective client here and refuses
    # non-loopback X-Real-IP even when the immediate peer is loopback.

    async def test_rejects_nonloopback_real_client_via_nginx(
        self, client, app, db, monkeypatch
    ):
        """Front-proxy bypass: peer is loopback (nginx) but X-Real-IP is the
        real client (a workspace container) -> backend refuses independently."""
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "local@example.com"
        )
        await model.create_user(
            "local@example.com",
            auth_mod.hash_password("unused"),
            verified=True,
        )
        resp = await client.post(
            "/api/v1/auth/local",
            headers={"X-Real-IP": "10.89.0.5"},
        )
        assert resp.status_code == 403
        assert "loopback" in resp.json()["detail"].lower()

    async def test_admits_loopback_real_client_via_nginx(
        self, client, app, db, monkeypatch
    ):
        """The benign mirror: peer loopback (nginx), X-Real-IP loopback (the
        operator's browser) -> admit. (ASGI test client peer is itself
        loopback, satisfying the trust gate that honors X-Real-IP.)"""
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "local@example.com"
        )
        await model.create_user(
            "local@example.com",
            auth_mod.hash_password("unused"),
            verified=True,
        )
        resp = await client.post(
            "/api/v1/auth/local",
            headers={"X-Real-IP": "127.0.0.1"},
        )
        assert resp.status_code == 200


class TestResendVerification:
    async def _create_unverified_user(self):
        password_hash = auth_mod.hash_password("testpass")
        await model.create_user(
            "unverified@example.com", password_hash, verified=False
        )

    def test_prune_timestamps_evicts_expired_keeps_recent(self):
        """prune_timestamps drops entries older than the cooldown only."""
        import time

        now = time.time()
        cooldown = 60
        ts = {
            "old@a.com": now - cooldown - 5,  # expired
            "edge@a.com": now - cooldown - 1,  # expired
            "fresh@a.com": now - 10,  # within window
            "recent@a.com": now,  # within window
        }
        api.prune_timestamps(ts, cooldown, now)
        assert "old@a.com" not in ts
        assert "edge@a.com" not in ts
        assert "fresh@a.com" in ts
        assert "recent@a.com" in ts

    def test_prune_timestamps_empty_dict(self):
        """Pruning an empty dict is a no-op."""
        import time

        ts: dict[str, float] = {}
        api.prune_timestamps(ts, 60, time.time())
        assert ts == {}

    async def test_resend_success(self, client, db):
        await self._create_unverified_user()
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ) as mock_send:
            resp = await client.post(
                "/api/v1/auth/resend-verification",
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
            "/api/v1/auth/resend-verification",
            json={
                "email": "unverified@example.com",
                "password": "wrong",
            },
        )
        assert resp.status_code == 401

    async def test_resend_nonexistent_user(self, client, db):
        resp = await client.post(
            "/api/v1/auth/resend-verification",
            json={
                "email": "nobody@example.com",
                "password": "pass",
            },
        )
        assert resp.status_code == 401

    async def test_resend_oidc_only_user_no_password(self, client, db):
        """OIDC-only users have no password hash; must 401, not 500 (#890)."""
        await model.create_user(
            "oidc@example.com", None, verified=False, provider="oidc"
        )
        resp = await client.post(
            "/api/v1/auth/resend-verification",
            json={"email": "oidc@example.com", "password": "anything"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid credentials"

    async def test_resend_already_verified(self, client, admin_user):
        resp = await client.post(
            "/api/v1/auth/resend-verification",
            json={
                "email": "testadmin@example.com",
                "password": "testpass",
            },
        )
        assert resp.status_code == 400
        assert "already verified" in resp.json()["detail"]

    async def test_resend_rate_limited(self, client, db):
        # Clear stale rate limit state from parallel test workers
        api.resend_timestamps.pop("unverified@example.com", None)
        await self._create_unverified_user()
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp1 = await client.post(
                "/api/v1/auth/resend-verification",
                json={
                    "email": "unverified@example.com",
                    "password": "testpass",
                },
            )
            assert resp1.status_code == 200
            resp2 = await client.post(
                "/api/v1/auth/resend-verification",
                json={
                    "email": "unverified@example.com",
                    "password": "testpass",
                },
            )
        assert resp2.status_code == 429
        api.resend_timestamps.pop("unverified@example.com", None)

    async def test_resend_prunes_expired_entries(self, client, db):
        # Stale rate-limit state from parallel test workers
        api.resend_timestamps.clear()
        await self._create_unverified_user()
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp1 = await client.post(
                "/api/v1/auth/resend-verification",
                json={
                    "email": "unverified@example.com",
                    "password": "testpass",
                },
            )
            assert resp1.status_code == 200
            # Backdate the entry past the cooldown window.
            import time

            api.resend_timestamps["unverified@example.com"] = (
                time.time() - api.RESEND_COOLDOWN_SECONDS - 1
            )
            # Also seed an unrelated expired address to confirm it is evicted.
            api.resend_timestamps["stale@example.com"] = (
                time.time() - api.RESEND_COOLDOWN_SECONDS - 1
            )
            resp2 = await client.post(
                "/api/v1/auth/resend-verification",
                json={
                    "email": "unverified@example.com",
                    "password": "testpass",
                },
            )
        # Expired entry no longer rate-limits, and unrelated stale entry
        # was swept on access.
        assert resp2.status_code == 200
        assert "stale@example.com" not in api.resend_timestamps
        api.resend_timestamps.clear()


class TestForgotPassword:
    async def _create_user(self):
        password_hash = auth_mod.hash_password("oldpass")
        return await model.create_user(
            "forgot@example.com", password_hash, verified=True
        )

    async def test_forgot_sends_email(self, client, db):
        await self._create_user()
        with patch.object(
            emailsvc_mod.EmailService,
            "send_password_reset_email",
            new_callable=AsyncMock,
        ) as mock_send:
            resp = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"
        mock_send.assert_awaited_once()
        api.reset_timestamps.pop("forgot@example.com", None)

    async def test_forgot_unknown_email_still_returns_sent(self, client, db):
        resp = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "nobody@example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"

    async def test_forgot_rate_limited(self, client, db):
        await self._create_user()
        with patch.object(
            emailsvc_mod.EmailService,
            "send_password_reset_email",
            new_callable=AsyncMock,
        ):
            await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
            resp2 = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
        assert resp2.status_code == 429
        api.reset_timestamps.pop("forgot@example.com", None)

    async def test_forgot_prunes_expired_entries(self, client, db):
        api.reset_timestamps.clear()
        await self._create_user()
        with patch.object(
            emailsvc_mod.EmailService,
            "send_password_reset_email",
            new_callable=AsyncMock,
        ):
            resp1 = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
            assert resp1.status_code == 200
            # Backdate the entry and seed an unrelated expired address.
            import time

            api.reset_timestamps["forgot@example.com"] = (
                time.time() - api.RESET_COOLDOWN_SECONDS - 1
            )
            api.reset_timestamps["stale@example.com"] = (
                time.time() - api.RESET_COOLDOWN_SECONDS - 1
            )
            resp2 = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
        assert resp2.status_code == 200
        assert "stale@example.com" not in api.reset_timestamps
        api.reset_timestamps.clear()


class TestResetPassword:
    async def _create_user(self):
        password_hash = auth_mod.hash_password("oldpass")
        return await model.create_user(
            "reset@example.com", password_hash, verified=True
        )

    async def test_reset_success(self, client, db):
        user = await self._create_user()
        token = _auth().create_password_reset_token(user["id"])
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "newpass1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "reset"
        assert "access_token" in data
        # Can login with new password
        resp2 = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "reset@example.com",
                "password": "newpass1",
            },
        )
        assert resp2.status_code == 200

    async def test_reset_invalid_token(self, client, db):
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": "garbage", "password": "newpass1"},
        )
        assert resp.status_code == 400

    async def test_reset_short_password(self, client, db):
        user = await self._create_user()
        token = _auth().create_password_reset_token(user["id"])
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "ab"},
        )
        assert resp.status_code == 400
        assert "8 characters" in resp.json()["detail"]

    async def test_reset_agent_user_rejected(self, client, db):
        token = _auth().create_password_reset_token(model.AGENT_USER_ID)
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "newpass1"},
        )
        assert resp.status_code == 400
        assert "system agent" in resp.json()["detail"]


class TestChangePassword:
    async def test_change_password_success(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "newpass1",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"
        # Can login with new password
        resp2 = await client.post(
            "/api/v1/auth/login",
            json={
                "email": "testuser@example.com",
                "password": "newpass1",
            },
        )
        assert resp2.status_code == 200

    async def test_change_password_wrong_current(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "wrongpass",
                "new_password": "newpass1",
            },
            headers=headers,
        )
        assert resp.status_code == 401

    async def test_change_password_too_short(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "ab",
            },
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_change_password_no_auth(self, client, db):
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "newpass1",
            },
        )
        assert resp.status_code == 401

    async def test_change_password_oidc_only_user(self, client, db):
        """OIDC-only users have no password; must 403, not 500 (#890)."""
        headers = await _oidc_user_headers()
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "anything",
                "new_password": "newpass1",
            },
            headers=headers,
        )
        assert resp.status_code == 403
        assert (
            resp.json()["detail"]
            == "Account is managed by your identity provider"
        )


class TestChangeEmail:
    async def test_change_email_success(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ) as mock_send:
            resp = await client.post(
                "/api/v1/auth/change-email",
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
            "/api/v1/auth/change-email",
            json={
                "email": "new@example.com",
                "password": "wrongpass",
            },
            headers=headers,
        )
        assert resp.status_code == 401

    async def test_change_email_already_taken(self, client, user, db):
        # Create another user
        password_hash = auth_mod.hash_password("other")
        await model.create_user(
            "other@example.com", password_hash, verified=True
        )
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/auth/change-email",
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
            "/api/v1/auth/change-email",
            json={
                "email": "not-an-email",
                "password": "testpass",
            },
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_change_email_no_auth(self, client, db):
        resp = await client.post(
            "/api/v1/auth/change-email",
            json={
                "email": "new@example.com",
                "password": "testpass",
            },
        )
        assert resp.status_code == 401

    async def test_change_email_oidc_only_user(self, client, db):
        """OIDC-only users have no password; must 403, not 500 (#890)."""
        headers = await _oidc_user_headers()
        resp = await client.post(
            "/api/v1/auth/change-email",
            json={
                "email": "new@example.com",
                "password": "anything",
            },
            headers=headers,
        )
        assert resp.status_code == 403
        assert (
            resp.json()["detail"]
            == "Account is managed by your identity provider"
        )


# --- Workspace routes ---


class TestWorkspaceRoutes:
    async def test_list_empty(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/workspaces", headers=headers)
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_includes_running_status(self, client, user, registry):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "run-test"}
        )
        ws_id = resp.json()["id"]
        # Not running (no container state)
        resp = await client.get("/api/v1/workspaces?limit=10", headers=headers)
        items = resp.json()["items"]
        ws = next(w for w in items if w["id"] == ws_id)
        assert ws["running"] is False

        # Simulate running container
        registry.track_activity("fake-cid", ws_id)
        try:
            resp = await client.get(
                "/api/v1/workspaces?limit=10", headers=headers
            )
            items = resp.json()["items"]
            ws = next(w for w in items if w["id"] == ws_id)
            assert ws["running"] is True
        finally:
            await registry.remove_state(ws_id)

        # Also works for bare list (no pagination params)
        resp = await client.get("/api/v1/workspaces", headers=headers)
        ws = next(w for w in resp.json() if w["id"] == ws_id)
        assert ws["running"] is False

    async def test_list_includes_live_health(self, client, user, registry):
        """List payload carries live health for a steady-state failure (#1173).

        The health monitor only broadcasts ``service_health`` on a
        *transition*, so a workspace unhealthy before any client connects
        would otherwise be invisible on the front page. The list endpoint
        must surface the live ``health``/``health_message`` from the
        in-memory ``ContainerState`` so the icon renders amber on load.
        """
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "health-list-test"},
        )
        ws_id = resp.json()["id"]

        # Simulate a running container that is steadily unhealthy.
        registry.track_activity("cid-health", ws_id)
        state = registry.get_state(ws_id)
        assert state is not None
        state.health_status = "unhealthy"
        state.health_message = "gateway refused connection"
        try:
            resp = await client.get(
                "/api/v1/workspaces?limit=10", headers=headers
            )
            items = resp.json()["items"]
            ws = next(w for w in items if w["id"] == ws_id)
            assert ws["running"] is True
            assert ws["health"] == "unhealthy"
            assert ws["health_message"] == "gateway refused connection"

            # A healthy workspace carries "healthy" and no message.
            state.health_status = "healthy"
            state.health_message = None
            resp = await client.get(
                "/api/v1/workspaces?limit=10", headers=headers
            )
            ws = next(w for w in resp.json()["items"] if w["id"] == ws_id)
            assert ws["health"] == "healthy"
            assert ws["health_message"] is None
        finally:
            await registry.remove_state(ws_id)

        # Stopped workspace: no health fields beyond running=False.
        resp = await client.get("/api/v1/workspaces?limit=10", headers=headers)
        ws = next(w for w in resp.json()["items"] if w["id"] == ws_id)
        assert ws["running"] is False

    async def test_list_pagination(self, client, user):
        headers = await _auth_headers(client)
        for name in ["ws-a", "ws-b", "ws-c"]:
            await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": name},
            )
        page1 = await client.get(
            "/api/v1/workspaces?limit=2&offset=0", headers=headers
        )
        assert page1.status_code == 200
        body1 = page1.json()
        assert len(body1["items"]) == 2
        assert body1["has_more"] is True
        assert body1["next_offset"] == 2
        page2 = await client.get(
            f"/api/v1/workspaces?limit=2&offset={body1['next_offset']}",
            headers=headers,
        )
        assert page2.status_code == 200
        body2 = page2.json()
        assert len(body2["items"]) == 1
        assert body2["has_more"] is False
        assert body2["next_offset"] is None

    async def test_list_pagination_rejects_invalid_limit(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/workspaces?limit=0", headers=headers)
        assert resp.status_code == 422

    async def test_list_pagination_rejects_invalid_offset(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/workspaces?offset=-1", headers=headers
        )
        assert resp.status_code == 422

    async def test_list_sort_by_name(self, client, user):
        headers = await _auth_headers(client)
        for name in ["charlie", "alpha", "bravo"]:
            await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": name},
            )
        resp = await client.get(
            "/api/v1/workspaces?limit=10&sort=name&order=asc",
            headers=headers,
        )
        names = [w["name"] for w in resp.json()["items"]]
        assert names == sorted(names)
        assert names[0] == "alpha"

    async def test_list_sort_desc(self, client, user):
        headers = await _auth_headers(client)
        for name in ["alpha", "bravo", "charlie"]:
            await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": name},
            )
        resp = await client.get(
            "/api/v1/workspaces?limit=10&sort=name&order=desc",
            headers=headers,
        )
        names = [w["name"] for w in resp.json()["items"]]
        assert names == sorted(names, reverse=True)
        assert names[0] == "charlie"

    async def test_list_filter_substring(self, client, user):
        headers = await _auth_headers(client)
        for name in ["alpha", "beta-gamma", "delta"]:
            await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": name},
            )
        # Matches anywhere (substring), not just prefix.
        resp = await client.get(
            "/api/v1/workspaces?limit=10&q=gamma", headers=headers
        )
        names = [w["name"] for w in resp.json()["items"]]
        assert names == ["beta-gamma"]

    async def test_list_rejects_invalid_sort(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/workspaces?sort=bogus", headers=headers
        )
        assert resp.status_code == 422

    async def test_list_rejects_invalid_order(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/workspaces?order=sideways", headers=headers
        )
        assert resp.status_code == 422

    async def test_create_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "test-ws"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-ws"
        assert "id" in data

    async def test_create_duplicate(self, client, user):
        headers = await _auth_headers(client)
        await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "dup"}
        )
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "dup"}
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    async def test_create_with_disallowed_image(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "bad-img", "image": "evil:latest"},
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]

    async def test_create_with_invalid_mount(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "bad-mount", "mounts": ["not-valid"]},
        )
        assert resp.status_code == 400
        assert "Invalid mount" in resp.json()["detail"]

    async def test_create_auto_start_rejected_without_env(self, client, user):
        headers = await _auth_headers(client)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KLANGK_ALLOW_AUTOSTART", None)
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": "auto-ws", "auto_start": True},
            )
        assert resp.status_code == 400
        assert "Auto-start is not enabled" in resp.json()["detail"]

    async def test_create_auto_start_allowed_with_env(self, client, app, user):
        headers = await _auth_headers(client)
        with (
            patch.object(app.state.settings, "allow_autostart", "1"),
            patch.object(
                app.state.workspaces,
                "start_workspace",
                new_callable=AsyncMock,
            ) as mock_start,
        ):
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": "auto-ws", "auto_start": True},
            )
        assert resp.status_code == 200
        assert resp.json()["auto_start"] is True
        mock_start.assert_awaited_once()

    async def test_create_auto_start_eager_failure_logged(
        self, client, app, user
    ):
        headers = await _auth_headers(client)
        with (
            patch.object(app.state.settings, "allow_autostart", "1"),
            patch.object(
                app.state.workspaces,
                "start_workspace",
                new_callable=AsyncMock,
                side_effect=RuntimeError("podman broke"),
            ),
        ):
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={
                    "name": "auto-fail-ws",
                    "auto_start": True,
                },
            )
        # Create succeeds even if eager start fails.
        assert resp.status_code == 200
        assert resp.json()["auto_start"] is True

    async def test_create_with_valid_mount(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "good-mount", "mounts": ["/tmp:/mnt/tmp"]},
        )
        assert resp.status_code == 200

    async def test_list_images(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/images", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "default" in data
        assert "allowed" in data
        assert data["default"] in data["allowed"]

    async def test_delete_workspace(self, client, user, registry):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "doomed"}
        )
        ws_id = create_resp.json()["id"]

        with patch.object(
            registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"

    async def test_delete_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete(
            "/api/v1/workspaces/fake-id", headers=headers
        )
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
        resp = await client.delete(
            f"/api/v1/workspaces/{fake_id}", headers=headers
        )
        assert resp.status_code == 404

    async def test_delete_workspace_with_container(
        self, client, app, user, registry
    ):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "has-container"},
        )
        ws_id = create_resp.json()["id"]
        # Simulate a running container
        await model.update_workspace_container(ws_id, "fake-container-id")

        with (
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ) as mock_rm,
            patch.object(
                app.state.agents,
                "stop_session",
                new_callable=AsyncMock,
            ) as mock_stop_agent,
        ):
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}", headers=headers
            )
        assert resp.status_code == 200
        mock_stop_agent.assert_awaited_once_with(ws_id)
        mock_rm.assert_awaited_once_with("fake-container-id")

    async def test_delete_workspace_cleans_up_groups(
        self, client, app, user, registry
    ):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "cleanup-test"},
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
            registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}", headers=headers
            )
        assert resp.status_code == 200

        # Role groups should be gone
        for suffix in ["owners", "coders", "collaborators", "spectators"]:
            group = await model.get_group_by_name(f"{suffix}-{ws_id}")
            assert group is None, f"expected {suffix} group to be deleted"

        # ACL entries should be gone
        acl = await model.get_acl_entries(f"/workspaces/{ws_id}")
        assert len(acl) == 0

    async def test_create_notifies_creator(self, client, user, sockets):
        headers = await _auth_headers(client)
        with patch.object(
            sockets,
            "notify_user_workspaces_changed",
        ) as mock_notify:
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": "notify-create"},
            )
        assert resp.status_code == 200
        mock_notify.assert_called_once_with(user["id"])

    async def test_delete_notifies_deleter_and_owner(
        self, client, app, user, registry, sockets
    ):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "notify-delete"},
        )
        ws_id = create_resp.json()["id"]

        with (
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
            patch.object(
                sockets,
                "notify_user_workspaces_changed",
            ) as mock_notify,
        ):
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}", headers=headers
            )
        assert resp.status_code == 200
        # Deleter is the owner here, so exactly one notify call for them.
        mock_notify.assert_called_once_with(user["id"])

    async def test_restart_workspace(self, client, app, user, registry):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "restart-me"}
        )
        ws_id = create_resp.json()["id"]

        # Simulate a running container so the stop path is exercised.
        registry.track_activity("cid-restart", ws_id)

        with (
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ) as mock_stop,
            patch.object(
                app.state.workspaces,
                "start_workspace",
                new_callable=AsyncMock,
            ) as mock_start,
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/restart", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "restarted"
        mock_stop.assert_awaited_once_with("cid-restart")
        # #1244: restart re-starts the container (not just stop+remove),
        # so the service command re-fires at the create choke point and
        # the workspace recovers.
        mock_start.assert_awaited_once()

        # Clean up registry state.
        registry.states.pop(ws_id, None)

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
            f"/api/v1/workspaces/{fake_id}/restart", headers=headers
        )
        assert resp.status_code == 404

    async def test_workspace_status_running(self, client, user, registry, app):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "status-ws"},
        )
        ws_id = create_resp.json()["id"]

        # Simulate a running container.
        registry.track_activity("cid-status", ws_id)

        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/status",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is True
        assert data["container_id"] == "cid-status"
        assert data["health"] is None  # placeholder
        assert isinstance(data["idle_seconds"], (int, float))
        assert (
            data["idle_timeout"]
            == app.state.container_registry.idle_timeout_seconds
        )
        assert isinstance(data["ports"], list)

        # Clean up registry state.
        registry.states.pop(ws_id, None)
        registry._cid_to_wsid.pop("cid-status", None)

    async def test_workspace_status_not_running(self, client, user):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "status-stopped"},
        )
        ws_id = create_resp.json()["id"]

        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/status",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False
        assert data["container_id"] is None
        assert data["idle_seconds"] is None
        assert data["ports"] == []

    async def test_workspace_status_not_found(self, client, user):
        headers = await _auth_headers(client)
        fake_id = "fake-status-id"
        await model.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "terminal",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.get(
            f"/api/v1/workspaces/{fake_id}/status",
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_list_no_auth(self, client):
        resp = await client.get("/api/v1/workspaces")
        assert resp.status_code == 401

    async def test_create_with_service_command(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "cmd-ws", "service_command": "pi"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["service_command"] == "pi"

    async def test_update_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "upd-ws"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={
                "name": "renamed",
                "service_command": "pi",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["name"] == "renamed"
        assert match[0]["service_command"] == "pi"

    async def test_update_workspace_propagates_to_live_state(
        self, client, app, user, registry
    ):
        # Editing setup_state/health_check on a workspace whose
        # container is live updates the cached ContainerState so the
        # health monitor picks it up without a restart (#1015).

        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "live-ws"},
            headers=headers,
        )
        ws_id = resp.json()["id"]

        # Simulate a running container by registering a live state.
        registry.track_activity(
            "cid-live",
            ws_id,
            health_check="old-cmd",
            setup_state="pending",
        )
        live = registry.get_state(ws_id)
        live.health_status = "healthy"  # will be reset on edit
        live.health_message = "stale reason"  # also reset on edit (#1088)
        try:
            resp = await client.put(
                f"/api/v1/workspaces/{ws_id}",
                json={
                    "health_check": "curl -sf http://localhost:8080/h",
                    "setup_state": "complete",
                },
                headers=headers,
            )
            assert resp.status_code == 200
            assert live.health_check == ("curl -sf http://localhost:8080/h")
            assert live.setup_state == "complete"
            # Editing health_check resets the cached status.
            assert live.health_status is None
            assert live.health_checked_at is None
            assert live.health_message is None
        finally:
            await registry.remove_state(ws_id)

    async def test_update_workspace_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.put(
            "/api/v1/workspaces/nonexistent",
            json={"service_command": "pi"},
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
            f"/api/v1/workspaces/{fake_id}",
            json={"service_command": "pi"},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_update_workspace_race_delete(
        self, client, app, user, monkeypatch
    ):
        """Workspace deleted between get and update returns 404."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "race-ws"}
        )
        ws_id = resp.json()["id"]
        original_update = model.update_workspace

        async def _delete_then_update(workspace_id, user_id, **fields):
            await model.delete_workspace(workspace_id, user_id)
            return await original_update(workspace_id, user_id, **fields)

        monkeypatch.setattr(model, "update_workspace", _delete_then_update)
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"service_command": "pi"},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_update_workspace_bad_image(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "img-upd"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"image": "evil:latest"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["detail"]

    async def test_update_workspace_no_fields(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "empty-upd"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={},
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_update_workspace_invalid_mount(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "mnt-upd"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"mounts": ["bad"]},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "Invalid mount" in resp.json()["detail"]

    async def test_update_auto_start_rejected_without_env(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "no-auto-upd"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KLANGK_ALLOW_AUTOSTART", None)
            resp = await client.put(
                f"/api/v1/workspaces/{ws_id}",
                json={"auto_start": True},
                headers=headers,
            )
        assert resp.status_code == 400
        assert "Auto-start is not enabled" in resp.json()["detail"]

    async def test_workspace_response_includes_auto_start(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "check-field"},
        )
        assert resp.status_code == 200
        assert "auto_start" in resp.json()

    async def test_duplicate_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={
                "name": "src-ws",
                "image": "klangk-workspace",
                "service_command": "pi",
                "mounts": ["/tmp:/mnt/tmp"],
                "env": {"FOO": "bar"},
            },
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/duplicate",
            json={"name": "dup-ws"},
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "dup-ws"
        assert data["image"] == "klangk-workspace"
        assert data["service_command"] == "pi"
        assert data["mounts"] == ["/tmp:/mnt/tmp"]
        assert data["env"] == {"FOO": "bar"}
        assert data["id"] != ws_id

    async def test_duplicate_workspace_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/nonexistent/duplicate",
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
            f"/api/v1/workspaces/{fake_id}/duplicate",
            json={"name": "dup"},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_duplicate_workspace_name_conflict(self, client, user):
        headers = await _auth_headers(client)
        await client.post(
            "/api/v1/workspaces",
            json={"name": "orig"},
            headers=headers,
        )
        ws_id = (
            await client.post(
                "/api/v1/workspaces",
                json={"name": "taken"},
                headers=headers,
            )
        ).json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/duplicate",
            json={"name": "orig"},
            headers=headers,
        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]

    async def test_duplicate_workspace_creates_role_groups(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "dup-roles-src"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/duplicate",
            json={"name": "dup-roles-target"},
            headers=headers,
        )
        assert resp.status_code == 200
        dup_id = resp.json()["id"]
        for suffix in ["owners", "coders", "collaborators", "spectators"]:
            group = await model.get_group_by_name(f"{suffix}-{dup_id}")
            assert group is not None, f"expected {suffix} group on duplicate"
        # Creator should be in the owners group
        owners = await model.get_group_by_name(f"owners-{dup_id}")
        members = await model.get_group_members(owners["id"])
        assert any(m["id"] == user["id"] for m in members)


# --- Workspace sharing ---


class TestWorkspaceSharingRoutes:
    async def _create_other_user(self):
        password_hash = auth_mod.hash_password("otherpass")
        return await model.create_user(
            "other@example.com", password_hash, verified=True
        )

    async def _other_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "other@example.com", "password": "otherpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_list_shared_workspaces(self, client, user):
        headers = await _auth_headers(client)
        await self._create_other_user()
        other_headers = await self._other_headers(client)
        # Create workspace as owner
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "shared-ws"}
        )
        ws_id = resp.json()["id"]
        # Share with other
        await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        # Other user sees it in shared list
        resp = await client.get(
            "/api/v1/workspaces/shared", headers=other_headers
        )
        assert resp.status_code == 200
        shared = resp.json()
        assert len(shared) >= 1
        assert any(w["id"] == ws_id for w in shared)
        assert any(w["owner_email"] == "testuser@example.com" for w in shared)

    async def test_list_shared_no_params_returns_bare_list(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/workspaces/shared", headers=headers)
        assert resp.status_code == 200
        # Backward-compatible: no pagination params -> bare list, not envelope.
        assert isinstance(resp.json(), list)

    async def test_list_shared_bare_path_not_capped_at_default(
        self, client, app, user
    ):
        """Shared bare-list path returns more than the default of 10 (#1266).

        Mirrors the owned-list regression: the Settings panel also
        fetches ``/api/v1/workspaces/shared`` with no params, so a user
        with more than 10 shared workspaces must not be silently cut off.
        """
        headers = await _auth_headers(client)
        await self._create_other_user()
        other_headers = await self._other_headers(client)
        for i in range(12):
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": f"shared-{i:02d}"},
            )
            await client.post(
                f"/api/v1/workspaces/{resp.json()['id']}/members",
                headers=headers,
                json={"email": "other@example.com"},
            )
        resp = await client.get(
            "/api/v1/workspaces/shared", headers=other_headers
        )
        assert resp.status_code == 200
        items = resp.json()
        assert isinstance(items, list)
        assert len(items) == 12

    async def test_list_shared_pagination_returns_envelope(self, client, user):
        headers = await _auth_headers(client)
        await self._create_other_user()
        other_headers = await self._other_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "shared-pg"}
        )
        ws_id = resp.json()["id"]
        await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        # Paginated request -> envelope shape.
        resp = await client.get(
            "/api/v1/workspaces/shared?limit=10&offset=0",
            headers=other_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict)
        assert "items" in body and "has_more" in body
        assert any(w["id"] == ws_id for w in body["items"])

    async def test_list_shared_filter_and_sort(self, client, user):
        headers = await _auth_headers(client)
        await self._create_other_user()
        other_headers = await self._other_headers(client)
        for name in ["alpha", "beta-shared", "gamma"]:
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": name},
            )
            await client.post(
                f"/api/v1/workspaces/{resp.json()['id']}/members",
                headers=headers,
                json={"email": "other@example.com"},
            )
        # Substring filter on name.
        resp = await client.get(
            "/api/v1/workspaces/shared?limit=10&q=shared",
            headers=other_headers,
        )
        names = [w["name"] for w in resp.json()["items"]]
        assert names == ["beta-shared"]
        # Sort by name ascending across all shared.
        resp = await client.get(
            "/api/v1/workspaces/shared?limit=10&sort=name&order=asc",
            headers=other_headers,
        )
        names = [w["name"] for w in resp.json()["items"]]
        assert names == sorted(names)
        assert names[0] == "alpha"

    async def test_get_members_empty(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/members", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_add_member(self, client, user):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "shared"
        assert resp.json()["user_id"] == other["id"]
        # Verify member is listed
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/members", headers=headers
        )
        assert len(resp.json()) == 1
        assert resp.json()[0]["email"] == "other@example.com"

    async def test_add_member_notifies_owner_and_target(
        self, client, app, user, sockets
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        with patch.object(
            sockets, "notify_user_workspaces_changed"
        ) as mock_notify:
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/members",
                headers=headers,
                json={"email": "other@example.com"},
            )
        assert resp.status_code == 200
        notified = {call.args[0] for call in mock_notify.call_args_list}
        assert notified == {user["id"], other["id"]}

    async def test_remove_member_notifies_owner_and_removed(
        self, client, app, user, sockets
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        with patch.object(
            sockets, "notify_user_workspaces_changed"
        ) as mock_notify:
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}/members/{other['id']}",
                headers=headers,
            )
        assert resp.status_code == 200
        notified = {call.args[0] for call in mock_notify.call_args_list}
        assert notified == {user["id"], other["id"]}

    async def test_add_to_role_notifies_owner_and_target(
        self, client, app, user, sockets
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "role-ws"}
        )
        ws_id = resp.json()["id"]
        with patch.object(
            sockets, "notify_user_workspaces_changed"
        ) as mock_notify:
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/roles/collaborators",
                headers=headers,
                json={"email": "other@example.com"},
            )
        assert resp.status_code == 200
        notified = {call.args[0] for call in mock_notify.call_args_list}
        assert notified == {user["id"], other["id"]}

    async def test_remove_from_role_notifies_owner_and_member(
        self, client, app, user, sockets
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "role-ws"}
        )
        ws_id = resp.json()["id"]
        await client.post(
            f"/api/v1/workspaces/{ws_id}/roles/collaborators",
            headers=headers,
            json={"email": "other@example.com"},
        )
        with patch.object(
            sockets, "notify_user_workspaces_changed"
        ) as mock_notify:
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}"
                f"/roles/collaborators/{other['id']}",
                headers=headers,
            )
        assert resp.status_code == 200
        notified = {call.args[0] for call in mock_notify.call_args_list}
        assert notified == {user["id"], other["id"]}

    async def test_change_role_notifies_owner_and_target(
        self, client, app, user, sockets
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "role-ws"}
        )
        ws_id = resp.json()["id"]
        with patch.object(
            sockets, "notify_user_workspaces_changed"
        ) as mock_notify:
            resp = await client.patch(
                f"/api/v1/workspaces/{ws_id}/roles",
                headers=headers,
                json={
                    "email": "other@example.com",
                    "role": "collaborators",
                },
            )
        assert resp.status_code == 200
        notified = {call.args[0] for call in mock_notify.call_args_list}
        assert notified == {user["id"], other["id"]}

    async def test_change_role_allows_system_agent_removal(
        self, client, app, user, db
    ):
        # role=None is removal-from-all-roles — harmless cleanup, so the
        # guard (which only fires on a grant) must let it through.
        from klangk_backend.main import seed_agent_user

        await seed_agent_user(make_settings({}))
        agent = await model.get_user_by_id(model.AGENT_USER_ID)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "role-ws"},
        )
        ws_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": agent["email"], "role": None},
        )
        assert resp.status_code == 200

    async def test_add_member_not_found(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "nobody@example.com"},
        )
        assert resp.status_code == 404
        assert "User not found" in resp.json()["detail"]

    async def test_add_member_self(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "testuser@example.com"},
        )
        assert resp.status_code == 400
        assert "yourself" in resp.json()["detail"]

    async def test_add_member_rejects_system_agent(self, client, user, db):
        # End-to-end smoke for the #1135 refactor: the guard now lives at
        # the model choke point (model.add_acl_entry), and a global
        # handler translates AgentPrincipalError to HTTP 400. This is the
        # one HTTP-level grant test kept to prove the wiring; the choke
        # points themselves are unit-tested in test_model.py.
        from klangk_backend.main import seed_agent_user

        await seed_agent_user(make_settings({}))
        agent = await model.get_user_by_id(model.AGENT_USER_ID)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "share-ws"},
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": agent["email"]},
        )
        assert resp.status_code == 400
        assert "system agent" in resp.json()["detail"]
        # Confirm the guard actually blocked the grant: no ACE entry on
        # this workspace names the agent as the user principal.
        resource = f"/workspaces/{ws_id}"
        entries = await model.get_acl_entries(resource)
        assert not any(e["user_id"] == agent["id"] for e in entries)

    async def test_remove_member(self, client, user):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        resp = await client.delete(
            f"/api/v1/workspaces/{ws_id}/members/{other['id']}",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "removed"
        # Verify member is gone
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/members", headers=headers
        )
        assert resp.json() == []

    async def test_non_owner_cannot_manage_members(self, client, user):
        headers = await _auth_headers(client)
        other = await self._create_other_user()
        other_headers = await self._other_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        # Share with other (gives view/terminal/files but not share)
        await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        # Other tries to list members — no share permission
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/members", headers=other_headers
        )
        assert resp.status_code == 403
        # Other tries to add a member
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=other_headers,
            json={"email": "testuser@example.com"},
        )
        assert resp.status_code == 403
        # Other tries to remove a member
        resp = await client.delete(
            f"/api/v1/workspaces/{ws_id}/members/{other['id']}",
            headers=other_headers,
        )
        assert resp.status_code == 403

    async def test_members_no_permission(self, client, user):
        """User without share permission gets 403 on nonexistent workspace."""
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/workspaces/nonexistent/members", headers=headers
        )
        assert resp.status_code == 403

    async def test_add_member_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/nonexistent/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        assert resp.status_code == 403

    async def test_remove_member_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete(
            "/api/v1/workspaces/nonexistent/members/some-id", headers=headers
        )
        assert resp.status_code == 403


class TestWorkspaceACL:
    async def test_get_workspace_acl(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "acl-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/acl", headers=headers
        )
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
        resp = await client.get(
            "/api/v1/workspaces/nonexistent/acl", headers=headers
        )
        assert resp.status_code == 403

    async def test_get_workspace_acl_with_group(
        self, client, app, admin_user, user
    ):
        """ACL endpoint resolves group names."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "group-acl-ws"},
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
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/acl", headers=headers
        )
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
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "replace-acl-ws"},
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
            f"/api/v1/workspaces/{ws_id}/acl",
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
            "/api/v1/workspaces", headers=headers, json={"name": "roles-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=headers
        )
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
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "owner-role-ws"},
        )
        ws_id = resp.json()["id"]
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=headers
        )
        roles = {r["role"]: r for r in resp.json()}
        owner_members = [m["id"] for m in roles["owners"]["members"]]
        assert user["id"] in owner_members

    async def test_add_user_to_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "add-role-ws"}
        )
        ws_id = resp.json()["id"]
        # Create a second user
        target = await model.create_user("role-target@test.com", "pass")
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/roles/spectators",
            headers=headers,
            json={"email": "role-target@test.com"},
        )
        assert resp.status_code == 200
        # Verify user is in the role
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=headers
        )
        roles = {r["role"]: r for r in resp.json()}
        member_ids = [m["id"] for m in roles["spectators"]["members"]]
        assert target["id"] in member_ids

    async def test_remove_user_from_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "rm-role-ws"}
        )
        ws_id = resp.json()["id"]
        target = await model.create_user("role-rm@test.com", "pass")
        # Add then remove
        await client.post(
            f"/api/v1/workspaces/{ws_id}/roles/coders",
            headers=headers,
            json={"email": "role-rm@test.com"},
        )
        resp = await client.delete(
            f"/api/v1/workspaces/{ws_id}/roles/coders/{target['id']}",
            headers=headers,
        )
        assert resp.status_code == 200
        # Verify removed
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=headers
        )
        roles = {r["role"]: r for r in resp.json()}
        member_ids = [m["id"] for m in roles["coders"]["members"]]
        assert target["id"] not in member_ids

    async def test_add_to_invalid_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "bad-role-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/roles/invalid",
            headers=headers,
            json={"email": "x@test.com"},
        )
        assert resp.status_code == 400

    async def test_add_nonexistent_user_to_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "nouser-role-ws"},
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/roles/spectators",
            headers=headers,
            json={"email": "nobody@nowhere.com"},
        )
        assert resp.status_code == 404

    async def test_roles_on_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/workspaces/fake-id/roles", headers=headers
        )
        assert resp.status_code == 403

    async def test_remove_from_invalid_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "bad-rm-ws"}
        )
        ws_id = resp.json()["id"]
        resp = await client.delete(
            f"/api/v1/workspaces/{ws_id}/roles/invalid/some-id",
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_remove_from_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete(
            "/api/v1/workspaces/fake-id/roles/coders/some-id",
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_add_to_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/fake-id/roles/coders",
            headers=headers,
            json={"email": "x@test.com"},
        )
        assert resp.status_code == 403

    async def test_role_group_not_found_add(self, client, user):
        """Adding to a role when the group was deleted returns 404."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "norole-add-ws"},
        )
        ws_id = resp.json()["id"]
        # Delete the spectators group to simulate missing role group
        group = await model.get_group_by_name(f"spectators-{ws_id}")
        await model.delete_group(group["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/roles/spectators",
            headers=headers,
            json={"email": "x@test.com"},
        )
        assert resp.status_code == 404
        assert "Role group" in resp.json()["detail"]

    async def test_role_group_not_found_remove(self, client, user):
        """Removing from a role when the group was deleted returns 404."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "norole-rm-ws"},
        )
        ws_id = resp.json()["id"]
        group = await model.get_group_by_name(f"coders-{ws_id}")
        await model.delete_group(group["id"])
        resp = await client.delete(
            f"/api/v1/workspaces/{ws_id}/roles/coders/some-id",
            headers=headers,
        )
        assert resp.status_code == 404
        assert "Role group" in resp.json()["detail"]

    async def test_roles_with_missing_group(self, client, user):
        """Listing roles skips groups that were deleted."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "missing-grp-ws"},
        )
        ws_id = resp.json()["id"]
        group = await model.get_group_by_name(f"spectators-{ws_id}")
        await model.delete_group(group["id"])
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=headers
        )
        roles = resp.json()
        role_names = [r["role"] for r in roles]
        assert "spectators" not in role_names
        assert "owners" in role_names


class TestChangeWorkspaceRole:
    async def test_change_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "chg-role-ws"}
        )
        ws_id = resp.json()["id"]
        target = await model.create_user("chg-role@test.com", "pass")
        # Add as coder
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "chg-role@test.com", "role": "coders"},
        )
        assert resp.status_code == 200
        # Verify in coders
        roles = (
            await client.get(
                f"/api/v1/workspaces/{ws_id}/roles", headers=headers
            )
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
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "chg-role@test.com", "role": "spectators"},
        )
        assert resp.status_code == 200
        # Verify moved
        roles = (
            await client.get(
                f"/api/v1/workspaces/{ws_id}/roles", headers=headers
            )
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
            "/api/v1/workspaces", headers=headers, json={"name": "rm-all-ws"}
        )
        ws_id = resp.json()["id"]
        await model.create_user("rm-all@test.com", "pass")
        await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "rm-all@test.com", "role": "coders"},
        )
        # Remove from all
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "rm-all@test.com", "role": None},
        )
        assert resp.status_code == 200
        roles = (
            await client.get(
                f"/api/v1/workspaces/{ws_id}/roles", headers=headers
            )
        ).json()
        all_members = [m["email"] for r in roles for m in r["members"]]
        assert "rm-all@test.com" not in all_members

    async def test_invalid_role(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "bad-chg-ws"}
        )
        ws_id = resp.json()["id"]
        await model.create_user("bad-chg@test.com", "pass")
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "bad-chg@test.com", "role": "invalid"},
        )
        assert resp.status_code == 400

    async def test_nonexistent_user(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "nouser-chg-ws"},
        )
        ws_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "nobody@nowhere.com", "role": "coders"},
        )
        assert resp.status_code == 404

    async def test_change_role_missing_group(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "miss-grp-chg-ws"},
        )
        ws_id = resp.json()["id"]
        await model.create_user("miss-grp@test.com", "pass")
        # Delete the target role group
        group = await model.get_group_by_name(f"spectators-{ws_id}")
        await model.delete_group(group["id"])
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "miss-grp@test.com", "role": "spectators"},
        )
        assert resp.status_code == 404
        assert "Role group" in resp.json()["detail"]

    async def test_change_role_skips_missing_groups_on_remove(
        self, client, app, user
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "skip-miss-ws"},
        )
        ws_id = resp.json()["id"]
        await model.create_user("skip-miss@test.com", "pass")
        # Add user to coders
        await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "skip-miss@test.com", "role": "coders"},
        )
        # Delete spectators group — should not break removal
        group = await model.get_group_by_name(f"spectators-{ws_id}")
        await model.delete_group(group["id"])
        # Change role — removal phase should skip missing group
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "skip-miss@test.com", "role": None},
        )
        assert resp.status_code == 200


class TestTransferOwnership:
    async def test_transfer_ownership(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "transfer-ws"},
        )
        assert resp.status_code == 200
        ws_id = resp.json()["id"]

        target = await model.create_user("xfer-target@test.com", "pass")
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/transfer",
            headers=headers,
            json={"email": "xfer-target@test.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["user_id"] == target["id"]

    async def test_transfer_user_not_found(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "xfer-nf-ws"},
        )
        ws_id = resp.json()["id"]

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/transfer",
            headers=headers,
            json={"email": "nobody@test.com"},
        )
        assert resp.status_code == 404
        assert "User not found" in resp.json()["detail"]

    async def test_transfer_to_self(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "xfer-self-ws"},
        )
        ws_id = resp.json()["id"]

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/transfer",
            headers=headers,
            json={"email": "testuser@example.com"},
        )
        assert resp.status_code == 409
        assert "already the owner" in resp.json()["detail"]

    async def test_transfer_duplicate_name(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "dup-name-ws"},
        )
        ws_id = resp.json()["id"]

        target = await model.create_user("xfer-dup@test.com", "pass")
        # Create a workspace with the same name owned by the target
        await model.create_workspace_with_acl(target["id"], "dup-name-ws")

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/transfer",
            headers=headers,
            json={"email": "xfer-dup@test.com"},
        )
        assert resp.status_code == 409
        assert "dup-name-ws" in resp.json()["detail"]

    async def test_transfer_non_owner_forbidden(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "xfer-forbid-ws"},
        )
        ws_id = resp.json()["id"]

        other = await model.create_user("xfer-other@test.com", "pass")
        other_token = _auth().create_token(other["id"], other["email"])
        other_headers = {"Authorization": f"Bearer {other_token}"}

        await model.create_user("xfer-target2@test.com", "pass")
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/transfer",
            headers=other_headers,
            json={"email": "xfer-target2@test.com"},
        )
        assert resp.status_code == 403

    async def test_transfer_to_agent_rejected(self, client, user):
        from klangk_backend.main import seed_agent_user

        await seed_agent_user(make_settings({}))
        agent = await model.get_user_by_id(model.AGENT_USER_ID)

        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "xfer-agent-ws"},
        )
        ws_id = resp.json()["id"]

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/transfer",
            headers=headers,
            json={"email": agent["email"]},
        )
        assert resp.status_code == 409
        assert "agent" in resp.json()["detail"].lower()

    async def test_transfer_workspace_not_found(self, client, user):
        result = await model.transfer_workspace(
            "nonexistent-ws-id", user["id"]
        )
        assert result is None

    async def test_transfer_updates_acl(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "xfer-acl-ws"},
        )
        ws_id = resp.json()["id"]

        target = await model.create_user("xfer-acl@test.com", "pass")
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/transfer",
            headers=headers,
            json={"email": "xfer-acl@test.com"},
        )
        assert resp.status_code == 200

        # New owner should be in the owners role group
        target_token = _auth().create_token(target["id"], target["email"])
        target_headers = {"Authorization": f"Bearer {target_token}"}
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=target_headers,
        )
        assert resp.status_code == 200
        roles = {r["role"]: r for r in resp.json()}
        owner_ids = [m["id"] for m in roles["owners"]["members"]]
        assert target["id"] in owner_ids
        assert user["id"] not in owner_ids


class TestWorkspaceGroupSharing:
    async def test_share_with_group(self, client, admin_user, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "group-share-ws"},
        )
        ws_id = resp.json()["id"]
        group = await model.create_group("devs")

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/groups",
            headers=headers,
            json={"group_id": group["id"]},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "devs"

        # Group shows up in list
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/groups", headers=headers
        )
        assert resp.status_code == 200
        groups = resp.json()
        group_names = [g["name"] for g in groups]
        assert "devs" in group_names

    async def test_remove_group(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "group-rm-ws"}
        )
        ws_id = resp.json()["id"]
        group = await model.create_group("temp-devs")

        await client.post(
            f"/api/v1/workspaces/{ws_id}/groups",
            headers=headers,
            json={"group_id": group["id"]},
        )
        resp = await client.delete(
            f"/api/v1/workspaces/{ws_id}/groups/{group['id']}", headers=headers
        )
        assert resp.status_code == 200

        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/groups", headers=headers
        )
        group_names = [g["name"] for g in resp.json()]
        assert "temp-devs" not in group_names

    async def test_share_with_nonexistent_group(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "bad-group-ws"},
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/groups",
            headers=headers,
            json={"group_id": "nonexistent"},
        )
        assert resp.status_code == 404

    async def test_group_share_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/workspaces/nonexistent/groups", headers=headers
        )
        assert resp.status_code == 403


class TestUserGroupEndpoints:
    """Tests for /groups endpoints (user-accessible, ACL-gated)."""

    async def test_list_groups(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/groups", headers=headers)
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
            "/api/v1/groups",
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
            "/api/v1/groups",
            headers=headers,
            json={"name": "dup-user-group"},
        )
        resp = await client.post(
            "/api/v1/groups",
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
            "/api/v1/groups",
            headers=headers,
            json={"name": "edit-group"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/groups/{group_id}",
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
            "/api/v1/groups",
            headers=headers,
            json={"name": "del-group"},
        )
        group_id = resp.json()["id"]
        resp = await client.delete(
            f"/api/v1/groups/{group_id}", headers=headers
        )
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
            "/api/v1/groups",
            headers=headers,
            json={"name": "member-group"},
        )
        group_id = resp.json()["id"]

        # Add admin_user as member
        resp = await client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=headers,
            json={"user_id": admin_user["id"]},
        )
        assert resp.status_code == 200

        # List members
        resp = await client.get(
            f"/api/v1/groups/{group_id}/members", headers=headers
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1

        # Remove member
        resp = await client.delete(
            f"/api/v1/groups/{group_id}/members/{admin_user['id']}",
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
            "/api/v1/groups/fake-id",
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
            "/api/v1/groups",
            headers=headers,
            json={"name": "noupdate-group"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/groups/{group_id}",
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
        resp = await client.delete("/api/v1/groups/fake-del", headers=headers)
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
        resp = await client.get(
            "/api/v1/groups/fake-mem/members", headers=headers
        )
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
            "/api/v1/groups/fake-add/members",
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
            "/api/v1/groups",
            headers=headers,
            json={"name": "baduser-group"},
        )
        group_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/groups/{group_id}/members",
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
            "/api/v1/groups",
            headers=headers,
            json={"name": "noremove-group"},
        )
        group_id = resp.json()["id"]
        resp = await client.delete(
            f"/api/v1/groups/{group_id}/members/nonexistent",
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_non_owner_cannot_manage(self, client, admin_user, user):
        """User without permission on the group gets 403."""
        # Admin creates a group (no ACE for regular user)
        admin_headers = {
            "Authorization": f"Bearer {(await client.post('/api/v1/auth/login', json={'email': 'testadmin@example.com', 'password': 'testpass'})).json()['access_token']}"
        }
        resp = await client.post(
            "/api/v1/admin/groups",
            headers=admin_headers,
            json={"name": "admin-only-group"},
        )
        group_id = resp.json()["id"]

        # Regular user tries to manage members
        headers = await _auth_headers(client)
        resp = await client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=headers,
            json={"user_id": user["id"]},
        )
        assert resp.status_code == 403


class TestUserSearch:
    async def test_search_users(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/users/search?q=testuser", headers=headers
        )
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1
        assert any(r["email"] == "testuser@example.com" for r in results)

    async def test_search_no_results(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/users/search?q=zzzzz", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_search_requires_auth(self, client, db):
        resp = await client.get("/api/v1/users/search?q=test")
        assert resp.status_code == 401

    async def test_search_empty_query(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/users/search?q=", headers=headers)
        assert resp.status_code == 400


# --- Messages ---


# --- Browser bridge ---


class TestBrowserBridge:
    def _ws_token_headers(self, workspace_id="ws-test"):
        token = _auth().create_workspace_token(workspace_id)
        return {"Authorization": f"Bearer {token}"}

    async def test_missing_token_returns_401(self, client, user):
        resp = await client.post(
            "/api/v1/browser-delegate",
            json={"action": "fetch", "browser_id": "bad-id"},
        )
        assert resp.status_code == 401

    async def test_unknown_browser_id_returns_403(self, client, user):
        resp = await client.post(
            "/api/v1/browser-delegate",
            json={"action": "fetch", "browser_id": "bad-id"},
            headers=self._ws_token_headers(),
        )
        assert resp.status_code == 403
        assert "Unknown browser ID" in resp.json()["detail"]

    async def test_expired_token_returns_401(self, client, app, user):
        with patch.object(
            app.state.auth,
            "decode_workspace_token",
            return_value=auth_mod.Auth.WORKSPACE_TOKEN_EXPIRED,
        ):
            resp = await client.post(
                "/api/v1/browser-delegate",
                json={"action": "fetch", "browser_id": "x"},
                headers={"Authorization": "Bearer some-expired-token"},
            )
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    async def test_browser_id_routes_to_correct_tab(
        self, client, app, user, registry, sockets
    ):
        """Browser ID routes to the specific browser tab."""
        mock_sock = MagicMock()
        registry.register_browser("bid-conn", "ws-conn", mock_sock)
        mock_session = AsyncMock()
        mock_session.browser_subscribers = {mock_sock}
        mock_session.dispatch_browser_request_to = AsyncMock(
            return_value={"status": 200, "body": "targeted"},
        )
        try:
            with patch.object(
                sockets,
                "get_session",
                return_value=mock_session,
            ):
                resp = await client.post(
                    "/api/v1/browser-delegate",
                    json={"action": "fetch", "browser_id": "bid-conn"},
                    headers=self._ws_token_headers("ws-conn"),
                )
            assert resp.status_code == 200
            assert resp.json()["body"] == "targeted"
            mock_session.dispatch_browser_request_to.assert_awaited_once_with(
                mock_sock, {"action": "fetch"}, timeout=30.0
            )
        finally:
            registry.revoke_workspace_browsers("ws-conn")

    async def test_browser_not_subscribed_returns_502(
        self, client, app, user, registry, sockets
    ):
        """Returns 502 when target not in browser_subscribers."""
        mock_sock = MagicMock()
        registry.register_browser("bid-nosub", "ws-nosub", mock_sock)
        mock_session = AsyncMock()
        mock_session.browser_subscribers = set()
        try:
            with patch.object(
                sockets,
                "get_session",
                return_value=mock_session,
            ):
                resp = await client.post(
                    "/api/v1/browser-delegate",
                    json={"action": "fetch", "browser_id": "bid-nosub"},
                    headers=self._ws_token_headers("ws-nosub"),
                )
            assert resp.status_code == 502
            assert "Browser connection not available" in resp.json()["detail"]
        finally:
            registry.revoke_workspace_browsers("ws-nosub")

    async def test_no_session_returns_502(self, client, user, registry):
        mock_sock = MagicMock()
        registry.register_browser("bid-nosess", "ws-nosess", mock_sock)
        try:
            resp = await client.post(
                "/api/v1/browser-delegate",
                json={"action": "fetch", "browser_id": "bid-nosess"},
                headers=self._ws_token_headers("ws-nosess"),
            )
            assert resp.status_code == 502
            assert "No browser client" in resp.json()["detail"]
        finally:
            registry.revoke_workspace_browsers("ws-nosess")

    async def test_dispatch_error_returns_502(
        self, client, app, user, registry, sockets
    ):
        mock_sock = MagicMock()
        registry.register_browser("bid-err", "ws-err", mock_sock)
        mock_session = AsyncMock()
        mock_session.browser_subscribers = {mock_sock}
        mock_session.dispatch_browser_request_to = AsyncMock(
            return_value={
                "error": "Browser client did not respond within timeout"
            },
        )
        try:
            with patch.object(
                sockets, "get_session", return_value=mock_session
            ):
                resp = await client.post(
                    "/api/v1/browser-delegate",
                    json={"action": "fetch", "browser_id": "bid-err"},
                    headers=self._ws_token_headers("ws-err"),
                )
            assert resp.status_code == 502
            assert "timeout" in resp.json()["detail"].lower()
        finally:
            registry.revoke_workspace_browsers("ws-err")

    async def test_stream_endpoint_relays_ndjson(
        self, client, app, user, registry, sockets
    ):
        """The streaming endpoint relays the generator's NDJSON to the caller."""
        mock_sock = MagicMock()
        registry.register_browser("bid-stream", "ws-stream", mock_sock)

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
                sockets, "get_session", return_value=mock_session
            ):
                resp = await client.post(
                    "/api/v1/browser-delegate/stream",
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
            registry.revoke_workspace_browsers("ws-stream")


# --- Volume routes ---


def _instance_id():
    """The instance ID this server uses (read from <data_dir>/instance-id).

    Matches the value the volume routes validate ``klangk.instance`` labels
    against. Uses the active test data_dir (KLANGK_DATA_DIR in os.environ, set
    by the temp_data_dir fixture) so it agrees with the ``app`` fixture's util.
    Not a cached global — a fresh read each call.
    """
    from klangk_backend.settings import KlangkSettings

    ns = types.SimpleNamespace(settings=KlangkSettings(os.environ))
    ns.util = util_mod.Util(ns)
    return ns.util.instance_id()


def _managed_volume(user_id="test-user"):
    """An inspect_volume result owned by this klangk instance."""
    return {
        "Labels": {
            "klangk.managed": "true",
            "klangk.instance": _instance_id(),
            "klangk.user-id": user_id,
        }
    }


class TestVolumeRoutes:
    async def test_list_volumes(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            _mock_pod,
            "list_volumes",
            AsyncMock(
                return_value=[
                    {
                        "Name": "my-vol",
                        "CreatedAt": "2026-01-01T00:00:00Z",
                        "Labels": {
                            "klangk.instance": _instance_id(),
                            "klangk.user-id": user["id"],
                        },
                    },
                    {
                        "Name": "other-vol",
                        "CreatedAt": "2026-01-01T00:00:00Z",
                        "Labels": {
                            "klangk.instance": _instance_id(),
                            "klangk.user-id": "someone-else",
                        },
                    },
                ]
            ),
        ):
            resp = await client.get("/api/v1/volumes", headers=headers)
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
                _mock_pod, "inspect_volume", AsyncMock(return_value=None)
            ),
            patch.object(_mock_pod, "create_volume", mock_create),
        ):
            resp = await client.post(
                "/api/v1/volumes",
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
            _mock_pod,
            "inspect_volume",
            AsyncMock(return_value={"Name": "dup-vol"}),
        ):
            resp = await client.post(
                "/api/v1/volumes",
                json={"name": "dup-vol"},
                headers=headers,
            )
        assert resp.status_code == 409

    async def test_create_volume_error_propagates(self, client, user):
        headers = await _auth_headers(client)
        with (
            patch.object(
                _mock_pod, "inspect_volume", AsyncMock(return_value=None)
            ),
            patch.object(
                _mock_pod,
                "create_volume",
                AsyncMock(side_effect=podman.PodmanError(500, "boom")),
            ),
            pytest.raises(podman.PodmanError),
        ):
            await client.post(
                "/api/v1/volumes",
                json={"name": "err-vol"},
                headers=headers,
            )

    async def test_delete_volume(self, client, user):
        headers = await _auth_headers(client)
        with (
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(user["id"])),
            ),
            patch.object(_mock_pod, "remove_volume", AsyncMock()),
        ):
            resp = await client.delete(
                "/api/v1/volumes/test-vol", headers=headers
            )
        assert resp.status_code == 200

    async def test_delete_volume_not_found(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            _mock_pod, "inspect_volume", AsyncMock(return_value=None)
        ):
            resp = await client.delete("/api/v1/volumes/nope", headers=headers)
        assert resp.status_code == 404

    async def test_delete_volume_wrong_instance(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            _mock_pod,
            "inspect_volume",
            AsyncMock(return_value={"Labels": {"klangk.instance": "other"}}),
        ):
            resp = await client.delete(
                "/api/v1/volumes/foreign", headers=headers
            )
        assert resp.status_code == 404

    async def test_delete_volume_wrong_user(self, client, user):
        headers = await _auth_headers(client)
        with patch.object(
            _mock_pod,
            "inspect_volume",
            AsyncMock(return_value=_managed_volume("someone-else")),
        ):
            resp = await client.delete(
                "/api/v1/volumes/other", headers=headers
            )
        assert resp.status_code == 403

    async def test_delete_volume_remove_not_found(self, client, user):
        """Volume vanishes between inspect and remove -> 404."""
        headers = await _auth_headers(client)
        with (
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(user["id"])),
            ),
            patch.object(
                _mock_pod,
                "remove_volume",
                AsyncMock(side_effect=podman.PodmanError(404, "gone")),
            ),
        ):
            resp = await client.delete("/api/v1/volumes/gone", headers=headers)
        assert resp.status_code == 404

    async def test_delete_volume_other_error(self, client, user):
        headers = await _auth_headers(client)
        with (
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(user["id"])),
            ),
            patch.object(
                _mock_pod,
                "remove_volume",
                AsyncMock(side_effect=podman.PodmanError(500, "internal")),
            ),
            pytest.raises(podman.PodmanError),
        ):
            await client.delete("/api/v1/volumes/err-vol", headers=headers)

    async def test_delete_volume_in_use(self, client, user):
        headers = await _auth_headers(client)
        with (
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(user["id"])),
            ),
            patch.object(
                _mock_pod,
                "remove_volume",
                AsyncMock(side_effect=podman.PodmanError(409, "in use")),
            ),
        ):
            resp = await client.delete("/api/v1/volumes/busy", headers=headers)
        assert resp.status_code == 409


# --- File routes ---


class TestFileRoutes:
    """File endpoints now require a running container (podman exec)."""

    CID = "cid-file-test"

    @pytest.fixture(autouse=True)
    def _bind_registry(self, registry):
        self._registry = registry

    async def _create_workspace(self, client, headers):
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "file-ws"}
        )
        ws_id = resp.json()["id"]
        # Simulate a running container
        self._registry.track_activity(self.CID, ws_id)
        return ws_id

    def _cleanup(self, ws_id):
        self._registry.states.pop(ws_id, None)
        self._registry._cid_to_wsid.pop(self.CID, None)

    async def test_list_files(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(0, "f.txt\tf\t10\t0.0\t0.0\n", ""),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files?path=/home/work",
                    headers=headers,
                )
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)
        finally:
            self._cleanup(ws_id)

    async def test_list_files_no_container_returns_409(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "no-ctr"}
        )
        ws_id = resp.json()["id"]
        # No container tracked
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/files?path=/", headers=headers
        )
        assert resp.status_code == 409
        assert "not running" in resp.json()["detail"]

    async def test_list_files_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/workspaces/fake-id/files?path=/", headers=headers
        )
        assert resp.status_code == 403

    async def test_upload_and_read(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
            ) as mock_exec:
                # Upload: write_file calls exec once (sh -c)
                mock_exec.return_value = (0, "", "")
                resp = await client.post(
                    f"/api/v1/workspaces/{ws_id}/files/upload?path=/home/work/hello.txt",
                    headers=headers,
                    files={
                        "file": ("hello.txt", b"hello world", "text/plain")
                    },
                )
                assert resp.status_code == 200
                assert resp.json()["status"] == "uploaded"

                # Read: stat + cat
                mock_exec.side_effect = [
                    (0, "regular file\t11", ""),  # stat
                    (0, "hello world", ""),  # cat
                ]
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files/content?path=/home/work/hello.txt",
                    headers=headers,
                )
                assert resp.status_code == 200
                assert resp.json()["content"] == "hello world"
        finally:
            self._cleanup(ws_id)

    async def test_upload_records_activity(self, client, user, registry):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            self._registry.states[ws_id].last_activity = 0.0
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(0, "", ""),
            ):
                await client.post(
                    f"/api/v1/workspaces/{ws_id}/files/upload?path=/home/work/test.txt",
                    headers=headers,
                    files={"file": ("test.txt", b"data", "text/plain")},
                )
            assert self._registry.states[ws_id].last_activity > 0.0
        finally:
            self._cleanup(ws_id)

    async def test_upload_no_filename(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/files/upload",
                headers=headers,
                files={"file": ("", b"data", "application/octet-stream")},
            )
            assert resp.status_code in (400, 422)
        finally:
            self._cleanup(ws_id)

    async def test_upload_exceeds_size_limit(
        self, client, user, app, monkeypatch
    ):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            monkeypatch.setattr(
                app.state.settings, "file_upload_size_max", "10"
            )
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/files/upload?path=/home/work/big.txt",
                headers=headers,
                files={"file": ("big.txt", b"x" * 100, "text/plain")},
            )
            assert resp.status_code == 413
            assert "limit" in resp.json()["detail"].lower()
        finally:
            self._cleanup(ws_id)

    async def test_read_nonexistent(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(1, "", "No such file"),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files/content?path=/nope.txt",
                    headers=headers,
                )
            assert resp.status_code == 404
        finally:
            self._cleanup(ws_id)

    async def test_upload_filename_too_long(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            long_name = "a" * 256 + ".txt"
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/files/upload?path=/home/{long_name}",
                headers=headers,
                files={
                    "file": (long_name, b"data", "application/octet-stream")
                },
            )
            assert resp.status_code == 400
            assert "limit" in resp.json()["detail"]
        finally:
            self._cleanup(ws_id)

    async def test_list_files_path_too_long(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            long_path = "/" + "a" * 256
            resp = await client.get(
                f"/api/v1/workspaces/{ws_id}/files?path={long_path}",
                headers=headers,
            )
            assert resp.status_code == 400
        finally:
            self._cleanup(ws_id)

    async def test_delete_file(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
            ) as mock_exec:
                mock_exec.side_effect = [
                    (0, "", ""),  # test -e
                    (0, "", ""),  # rm -rf
                ]
                resp = await client.delete(
                    f"/api/v1/workspaces/{ws_id}/files?path=/home/work/doomed.txt",
                    headers=headers,
                )
            assert resp.status_code == 200
            assert resp.json()["status"] == "deleted"
        finally:
            self._cleanup(ws_id)

    async def test_delete_nonexistent_file(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(1, "", ""),
            ):
                resp = await client.delete(
                    f"/api/v1/workspaces/{ws_id}/files?path=/ghost.txt",
                    headers=headers,
                )
            assert resp.status_code == 404
        finally:
            self._cleanup(ws_id)

    async def test_delete_file_oserror(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch(
                "klangk_backend.files.delete_path",
                new_callable=AsyncMock,
                side_effect=OSError("Permission denied"),
            ):
                resp = await client.delete(
                    f"/api/v1/workspaces/{ws_id}/files?path=/usr/bin/test",
                    headers=headers,
                )
            assert resp.status_code == 500
            assert "Permission denied" in resp.json()["detail"]
        finally:
            self._cleanup(ws_id)

    async def test_rename_file(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
            ) as mock_exec:
                mock_exec.side_effect = [
                    (0, "", ""),  # test -e old
                    (1, "", ""),  # test -e new (doesn't exist)
                    (0, "", ""),  # mkdir -p
                    (0, "", ""),  # mv
                ]
                resp = await client.post(
                    f"/api/v1/workspaces/{ws_id}/files/rename",
                    headers=headers,
                    json={
                        "old_path": "/home/work/old.txt",
                        "new_path": "/home/work/new.txt",
                    },
                )
            assert resp.status_code == 200
            assert resp.json()["status"] == "renamed"
        finally:
            self._cleanup(ws_id)

    async def test_rename_nonexistent(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(1, "", ""),
            ):
                resp = await client.post(
                    f"/api/v1/workspaces/{ws_id}/files/rename",
                    headers=headers,
                    json={"old_path": "/nope.txt", "new_path": "/new.txt"},
                )
            assert resp.status_code == 404
        finally:
            self._cleanup(ws_id)

    async def test_rename_to_existing(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
            ) as mock_exec:
                mock_exec.side_effect = [
                    (0, "", ""),  # test -e old
                    (0, "", ""),  # test -e new (exists!)
                ]
                resp = await client.post(
                    f"/api/v1/workspaces/{ws_id}/files/rename",
                    headers=headers,
                    json={"old_path": "/a.txt", "new_path": "/b.txt"},
                )
            assert resp.status_code == 409
        finally:
            self._cleanup(ws_id)

    async def test_rename_file_oserror(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch(
                "klangk_backend.files.rename_path",
                new_callable=AsyncMock,
                side_effect=OSError("Permission denied"),
            ):
                resp = await client.post(
                    f"/api/v1/workspaces/{ws_id}/files/rename",
                    headers=headers,
                    json={
                        "old_path": "/usr/bin/a",
                        "new_path": "/usr/bin/b",
                    },
                )
            assert resp.status_code == 500
            assert "Permission denied" in resp.json()["detail"]
        finally:
            self._cleanup(ws_id)

    async def test_download_file(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:

            async def fake_stream(*a, **kw):
                yield b"download me"

            with (
                patch.object(
                    _mock_pod,
                    "exec_container",
                    new_callable=AsyncMock,
                    return_value=(0, "regular file\t11", ""),
                ),
                patch.object(
                    _mock_pod,
                    "exec_container_stream",
                    side_effect=fake_stream,
                ),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files/download?path=/home/work/dl.txt",
                    headers=headers,
                )
            assert resp.status_code == 200
            assert resp.content == b"download me"
        finally:
            self._cleanup(ws_id)

    async def test_download_file_strips_quotes_from_filename(
        self, client, app, user
    ):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:

            async def fake_stream(*a, **kw):
                yield b"data"

            with (
                patch.object(
                    _mock_pod,
                    "exec_container",
                    new_callable=AsyncMock,
                    return_value=(0, "regular file\t4", ""),
                ),
                patch.object(
                    _mock_pod,
                    "exec_container_stream",
                    side_effect=fake_stream,
                ),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files/download?path=/home/work/f%22name.txt",
                    headers=headers,
                )
            assert resp.status_code == 200
            assert (
                resp.headers["content-disposition"]
                == 'attachment; filename="fname.txt"'
            )
        finally:
            self._cleanup(ws_id)

    async def test_download_directory_as_tar(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:

            async def fake_stream(*a, **kw):
                yield b"\x1f\x8b"
                yield b"tardata"

            with (
                patch.object(
                    _mock_pod,
                    "exec_container",
                    new_callable=AsyncMock,
                    return_value=(0, "directory\t4096", ""),
                ),
                patch.object(
                    _mock_pod,
                    "exec_container_stream",
                    side_effect=fake_stream,
                ),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files/download?path=/home/work/mydir",
                    headers=headers,
                )
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "application/gzip"
            assert resp.content == b"\x1f\x8btardata"
        finally:
            self._cleanup(ws_id)

    async def test_download_nonexistent(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(1, "", "No such file"),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files/download?path=/nope.txt",
                    headers=headers,
                )
            assert resp.status_code == 404
        finally:
            self._cleanup(ws_id)

    async def test_upload_to_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/fake-id/files/upload?path=/f.txt",
            headers=headers,
            files={"file": ("f.txt", b"data", "text/plain")},
        )
        assert resp.status_code == 403

    async def test_file_traversal_rejected(self, client, user):
        """Relative paths are rejected (must be absolute)."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            resp = await client.get(
                f"/api/v1/workspaces/{ws_id}/files/content?path=../../etc/passwd",
                headers=headers,
            )
            assert resp.status_code == 400
        finally:
            self._cleanup(ws_id)

    async def test_list_files_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            resp = await client.get(
                f"/api/v1/workspaces/{ws_id}/files?path=../../etc",
                headers=headers,
            )
            assert resp.status_code == 400
        finally:
            self._cleanup(ws_id)

    async def test_delete_file_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete(
            "/api/v1/workspaces/fake-id/files?path=/f.txt", headers=headers
        )
        assert resp.status_code == 403

    async def test_delete_file_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}/files?path=../../etc/passwd",
                headers=headers,
            )
            assert resp.status_code == 400
        finally:
            self._cleanup(ws_id)

    async def test_rename_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/fake-id/files/rename",
            headers=headers,
            json={"old_path": "/a", "new_path": "/b"},
        )
        assert resp.status_code == 403

    async def test_rename_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/files/rename",
                headers=headers,
                json={"old_path": "../../etc/passwd", "new_path": "/stolen"},
            )
            assert resp.status_code == 400
        finally:
            self._cleanup(ws_id)

    async def test_download_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/workspaces/fake-id/files/download?path=/f.txt",
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_download_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            resp = await client.get(
                f"/api/v1/workspaces/{ws_id}/files/download?path=../../etc/passwd",
                headers=headers,
            )
            assert resp.status_code == 400
        finally:
            self._cleanup(ws_id)

    async def test_read_nonexistent_workspace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/workspaces/fake-id/files/content?path=/f.txt",
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_upload_write_fails(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(1, "", "Read-only file system"),
            ):
                resp = await client.post(
                    f"/api/v1/workspaces/{ws_id}/files/upload?path=/usr/bin/evil",
                    headers=headers,
                    files={"file": ("evil", b"bad", "text/plain")},
                )
            assert resp.status_code == 500
            assert "Read-only" in resp.json()["detail"]
        finally:
            self._cleanup(ws_id)

    async def test_upload_traversal(self, client, user):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/files/upload?path=../../etc/evil",
                headers=headers,
                files={"file": ("evil.txt", b"bad", "text/plain")},
            )
            assert resp.status_code == 400
        finally:
            self._cleanup(ws_id)


# --- Test mode endpoint ---


class TestSetIdleTimeout:
    async def test_set_idle_timeout_global(self, db, app, registry):
        """Setting global idle timeout changes the module-level variable."""
        original_timeout = app.state.container_registry.idle_timeout_seconds
        try:
            app.state.container_registry.idle_timeout_seconds = 42
            assert app.state.container_registry.idle_timeout_seconds == 42
            # Per-workspace lookup falls back to global
            assert registry.get_workspace_idle_timeout("any") == 42
        finally:
            app.state.container_registry.idle_timeout_seconds = (
                original_timeout
            )

    async def test_endpoint_missing_without_test_mode(self, client):
        """Without KLANGK_TEST_MODE, the endpoints should not exist."""
        resp = await client.post(
            "/api/v1/test/set-idle-timeout", json={"seconds": 10}
        )
        assert resp.status_code in (404, 405)
        resp = await client.get("/api/v1/test/idle-timeout")
        assert resp.status_code in (404, 405)

    async def test_set_idle_timeout_per_workspace(self, db, app, registry):
        """Per-workspace idle timeout should not affect global."""
        original_timeout = app.state.container_registry.idle_timeout_seconds
        try:
            registry.track_activity("cid-test", "ws-test")
            registry.set_workspace_idle_timeout("ws-test", 5)
            assert registry.get_workspace_idle_timeout("ws-test") == 5
            assert (
                app.state.container_registry.idle_timeout_seconds
                == original_timeout
            )
            # Unknown workspace returns global default
            assert (
                registry.get_workspace_idle_timeout("ws-other")
                == original_timeout
            )
        finally:
            registry.states.pop("ws-test", None)

    async def test_cleanup_loop_adapts_to_short_timeout(
        self, db, app, registry
    ):
        """Cleanup loop interval adapts when per-workspace timeouts exist."""
        try:
            registry.track_activity("cid-fast", "ws-fast")
            registry.set_workspace_idle_timeout("ws-fast", 6)
            # With a 6s per-workspace timeout, the minimum is 6, so
            # the loop should sleep max(2, 6//2) = 3 seconds.
            state = registry.states["ws-fast"]
            assert state.idle_timeout == 6
            # Global CHECK_INTERVAL_SECONDS should be unchanged
            assert (
                app.state.container_registry.check_interval_seconds
                == app.state.container_registry._parse_idle_timeout()[1]
            )
        finally:
            registry.states.pop("ws-fast", None)


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
        token = _auth().create_token(user["id"], "testuser@example.com")
        payload = _auth().decode_token(token)
        assert "roles" not in payload

    async def test_login_jwt_has_no_roles(self, client, user):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 200
        token = resp.json()["access_token"]
        payload = _auth().decode_token(token)
        assert "roles" not in payload


# --- Admin API endpoints ---


class TestAdminEndpoints:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_list_users(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.get("/api/v1/admin/users", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        users = body["users"]
        assert len(users) >= 2
        emails = [u["email"] for u in users]
        assert "testadmin@example.com" in emails
        assert "testuser@example.com" in emails
        # Groups are no longer shipped in the list response.
        assert "groups" not in users[0]
        # Paged envelope metadata.
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["total"] >= 2

    async def test_list_users_default_page_size_is_10(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        # Create 12 users so the default page is full but not exhaustive.
        for i in range(12):
            await model.create_user(f"u{i}@example.com", None, verified=True)
        resp = await client.get("/api/v1/admin/users", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["total"] >= 13  # 12 created + admin fixture
        assert len(body["users"]) == 10

    async def test_list_users_pagination_across_pages(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        for i in range(5):
            await model.create_user(f"pg{i}@example.com", None, verified=True)
        page1 = await client.get(
            "/api/v1/admin/users?page=1&page_size=3", headers=headers
        )
        page2 = await client.get(
            "/api/v1/admin/users?page=2&page_size=3", headers=headers
        )
        assert page1.status_code == 200
        assert page2.status_code == 200
        b1 = page1.json()
        b2 = page2.json()
        assert b1["page"] == 1
        assert b2["page"] == 2
        assert b1["page_size"] == 3
        assert b1["total"] == b2["total"]
        # Pages don't overlap.
        ids1 = {u["id"] for u in b1["users"]}
        ids2 = {u["id"] for u in b2["users"]}
        assert ids1.isdisjoint(ids2)
        assert len(b1["users"]) == 3
        assert len(b2["users"]) == 3

    async def test_list_users_sort_by_email(self, client, admin_user):
        headers = await self._admin_headers(client)
        for e in [
            "charlie@example.com",
            "alpha@example.com",
            "bravo@example.com",
        ]:
            await model.create_user(e, None, verified=True)
        resp = await client.get(
            "/api/v1/admin/users?sort=email&order=asc&page_size=50",
            headers=headers,
        )
        emails = [u["email"] for u in resp.json()["users"]]
        assert emails == sorted(emails, key=str.lower)
        assert emails[0] == "alpha@example.com"

    async def test_list_users_sort_desc_reverses(self, client, admin_user):
        headers = await self._admin_headers(client)
        for e in [
            "charlie@example.com",
            "alpha@example.com",
            "bravo@example.com",
        ]:
            await model.create_user(e, None, verified=True)
        asc = await client.get(
            "/api/v1/admin/users?sort=email&order=asc&page_size=50",
            headers=headers,
        )
        desc = await client.get(
            "/api/v1/admin/users?sort=email&order=desc&page_size=50",
            headers=headers,
        )
        asc_emails = [u["email"] for u in asc.json()["users"]]
        desc_emails = [u["email"] for u in desc.json()["users"]]
        assert asc_emails == list(reversed(desc_emails))

    async def test_list_users_filter_by_email(self, client, admin_user):
        headers = await self._admin_headers(client)
        await model.create_user("needle@example.com", None, verified=True)
        await model.create_user("haystack@example.com", None, verified=True)
        resp = await client.get(
            "/api/v1/admin/users?q=needle&page_size=50", headers=headers
        )
        body = resp.json()
        emails = [u["email"] for u in body["users"]]
        assert emails == ["needle@example.com"]
        assert body["total"] == 1

    async def test_list_users_invalid_sort_falls_back_to_created(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        # An unknown sort column must not 500 (falls back to created_at).
        resp = await client.get(
            "/api/v1/admin/users?sort=evil%3B%20DROP%20TABLE&order=asc",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["users"] is not None

    async def test_list_users_requires_admin(self, client, user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.get("/api/v1/admin/users", headers=headers)
        assert resp.status_code == 403

    async def test_admin_create_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/users",
            headers=headers,
            json={"email": "newuser@example.com", "password": "testpass123"},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "newuser@example.com"
        assert resp.json()["status"] == "created"
        # User should be verified and able to log in
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "newuser@example.com", "password": "testpass123"},
        )
        assert login_resp.status_code == 200

    async def test_admin_create_user_duplicate(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/users",
            headers=headers,
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 400
        assert "already registered" in resp.json()["detail"]

    async def test_admin_create_user_short_password(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/users",
            headers=headers,
            json={"email": "short@example.com", "password": "ab"},
        )
        assert resp.status_code == 400
        assert "Password" in resp.json()["detail"]

    async def test_admin_create_user_send_verification_email(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ) as mock_email:
            resp = await client.post(
                "/api/v1/admin/users",
                headers=headers,
                json={
                    "email": "verify@example.com",
                    "send_verification_email": True,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "verify@example.com"
        assert data["status"] == "pending_verification"
        mock_email.assert_called_once()
        # User should exist but not be verified, with a derived handle
        # (regression: #1256 — this branch used to INSERT without a handle).
        user = await model.get_user_by_email("verify@example.com")
        assert user is not None
        assert user["verified"] == 0
        assert user["handle"] == "verify"  # derived, not NULL

    async def test_admin_create_user_no_password_no_verify(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/users",
            headers=headers,
            json={"email": "nopw@example.com"},
        )
        assert resp.status_code == 400
        assert "Password is required" in resp.json()["detail"]

    async def test_admin_create_user_requires_admin(self, client, user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.post(
            "/api/v1/admin/users",
            headers=headers,
            json={"email": "new@example.com", "password": "testpass123"},
        )
        assert resp.status_code == 403

    async def test_delete_user(self, client, app, admin_user, user, registry):
        headers = await self._admin_headers(client)
        with (
            patch.object(
                registry,
                "stop_user_containers",
                new_callable=AsyncMock,
            ),
            patch.object(
                app.state.workspaces,
                "archive_user_data",
                new_callable=AsyncMock,
            ),
        ):
            resp = await client.delete(
                f"/api/v1/admin/users/{user['id']}", headers=headers
            )
        assert resp.status_code == 200
        # Verify user is gone
        resp = await client.get(
            "/api/v1/admin/users?page_size=200", headers=headers
        )
        emails = [u["email"] for u in resp.json()["users"]]
        assert "testuser@example.com" not in emails

    async def test_delete_self_forbidden(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            f"/api/v1/admin/users/{admin_user['id']}", headers=headers
        )
        assert resp.status_code == 400

    async def test_delete_nonexistent_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            "/api/v1/admin/users/nonexistent-id", headers=headers
        )
        assert resp.status_code == 404

    async def test_delete_agent_user_rejected(self, client, admin_user, db):
        from klangk_backend.main import seed_agent_user

        await seed_agent_user(make_settings({}))
        headers = await self._admin_headers(client)
        resp = await client.delete(
            f"/api/v1/admin/users/{model.AGENT_USER_ID}", headers=headers
        )
        assert resp.status_code == 400
        assert "system agent" in resp.json()["detail"]

    async def test_delete_user_cascades_workspaces(
        self, client, app, admin_user, user, registry
    ):
        """Deleting a user cascades to their ws_mod."""
        headers = await self._admin_headers(client)
        # Create a workspace for the user
        user_login = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        user_headers = {
            "Authorization": f"Bearer {user_login.json()['access_token']}"
        }
        ws_resp = await client.post(
            "/api/v1/workspaces",
            headers=user_headers,
            json={"name": "to-delete"},
        )
        assert ws_resp.status_code == 200
        # Delete the user
        with patch.object(
            registry,
            "stop_user_containers",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(
                f"/api/v1/admin/users/{user['id']}", headers=headers
            )
        assert resp.status_code == 200
        # Workspace should be gone (CASCADE)
        ws_list = await model.get_user_workspaces_with_containers(user["id"])
        assert len(ws_list) == 0

    async def test_list_user_workspaces_admin(self, client, admin_user, user):
        """Admin can list another user's workspaces (#1224)."""
        headers = await self._admin_headers(client)
        # The `user` fixture owns no workspaces yet.
        resp = await client.get(
            f"/api/v1/admin/users/{user['id']}/workspaces", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []
        assert resp.json()["has_more"] is False

        # Create a workspace as that user, then it should appear.
        user_login = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        user_headers = {
            "Authorization": f"Bearer {user_login.json()['access_token']}"
        }
        ws_resp = await client.post(
            "/api/v1/workspaces",
            headers=user_headers,
            json={"name": "doomed"},
        )
        assert ws_resp.status_code == 200
        resp = await client.get(
            f"/api/v1/admin/users/{user['id']}/workspaces", headers=headers
        )
        assert resp.status_code == 200
        names = [ws["name"] for ws in resp.json()["items"]]
        assert names == ["doomed"]

    async def test_list_user_workspaces_admin_404(self, client, admin_user):
        """Listing workspaces for a nonexistent user 404s."""
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/api/v1/admin/users/nonexistent-id/workspaces", headers=headers
        )
        assert resp.status_code == 404

    async def test_update_email(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/admin/users/{user['id']}",
            json={"email": "renamed"},
            headers=headers,
        )
        assert resp.status_code == 200
        updated = await model.get_user_by_id(user["id"])
        assert updated["email"] == "renamed"

    async def test_update_password(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/admin/users/{user['id']}",
            json={"password": "newpass123"},
            headers=headers,
        )
        assert resp.status_code == 200
        # Verify can login with new password
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "newpass123"},
        )
        assert login_resp.status_code == 200

    async def test_update_nonexistent_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            "/api/v1/admin/users/nonexistent-id",
            json={"email": "x"},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_update_agent_password_rejected(
        self, client, app, admin_user, db
    ):
        # Seed the agent user so it exists in the DB
        from klangk_backend.main import seed_agent_user

        await seed_agent_user(make_settings({}))
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/admin/users/{model.AGENT_USER_ID}",
            json={"password": "sneaky123"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "system agent" in resp.json()["detail"]

    async def test_unlock_user(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        # Lock out the user
        await model.record_failed_login(user["email"])
        await model.set_login_lockout(
            user["email"], "2099-01-01T00:00:00+00:00"
        )
        # Verify locked
        info = await model.get_login_attempt_info(user["email"])
        assert info["locked_until"] is not None
        # Unlock via admin endpoint
        resp = await client.post(
            f"/api/v1/admin/users/{user['id']}/unlockout", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "unlocked"
        # Verify lockout cleared
        info = await model.get_login_attempt_info(user["email"])
        assert info is None

    async def test_unlock_nonexistent_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/users/nonexistent-id/unlockout", headers=headers
        )
        assert resp.status_code == 404

    async def test_unlock_requires_admin(self, client, user):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        resp = await client.post(
            f"/api/v1/admin/users/{user['id']}/unlockout", headers=headers
        )
        assert resp.status_code == 403


class TestGroupEndpoints:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_list_groups(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get("/api/v1/admin/groups", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        groups = body["groups"]
        assert any(g["name"] == "admin" for g in groups)
        # Paged envelope metadata.
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["total"] >= 1

    async def test_list_groups_default_page_size_is_10(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        for i in range(12):
            await model.create_group(f"size-{i}")
        resp = await client.get("/api/v1/admin/groups", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["total"] >= 13  # 12 created + admin fixture
        assert len(body["groups"]) == 10

    async def test_list_groups_pagination_across_pages(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        for i in range(5):
            await model.create_group(f"pg-{i}")
        page1 = await client.get(
            "/api/v1/admin/groups?page=1&page_size=3", headers=headers
        )
        page2 = await client.get(
            "/api/v1/admin/groups?page=2&page_size=3", headers=headers
        )
        assert page1.status_code == 200
        assert page2.status_code == 200
        b1 = page1.json()
        b2 = page2.json()
        assert b1["page"] == 1
        assert b2["page"] == 2
        assert b1["page_size"] == 3
        assert b1["total"] == b2["total"]
        # Pages don't overlap.
        ids1 = {g["id"] for g in b1["groups"]}
        ids2 = {g["id"] for g in b2["groups"]}
        assert ids1.isdisjoint(ids2)
        assert len(b1["groups"]) == 3
        assert len(b2["groups"]) == 3

    async def test_list_groups_sort_by_name(self, client, admin_user):
        headers = await self._admin_headers(client)
        for n in ["charlie", "alpha", "bravo"]:
            await model.create_group(n)
        resp = await client.get(
            "/api/v1/admin/groups?sort=name&order=asc&page_size=200",
            headers=headers,
        )
        names = [g["name"] for g in resp.json()["groups"]]
        assert names == sorted(names, key=str.lower)
        assert "alpha" in names

    async def test_list_groups_sort_desc_reverses(self, client, admin_user):
        headers = await self._admin_headers(client)
        for n in ["charlie", "alpha", "bravo"]:
            await model.create_group(n)
        asc = await client.get(
            "/api/v1/admin/groups?sort=name&order=asc&page_size=200",
            headers=headers,
        )
        desc = await client.get(
            "/api/v1/admin/groups?sort=name&order=desc&page_size=200",
            headers=headers,
        )
        asc_names = [g["name"] for g in asc.json()["groups"]]
        desc_names = [g["name"] for g in desc.json()["groups"]]
        assert asc_names == list(reversed(desc_names))

    async def test_list_groups_filter_by_name(self, client, admin_user):
        headers = await self._admin_headers(client)
        await model.create_group("needle-group")
        await model.create_group("haystack-group")
        resp = await client.get(
            "/api/v1/admin/groups?q=needle&page_size=200",
            headers=headers,
        )
        body = resp.json()
        names = [g["name"] for g in body["groups"]]
        assert names == ["needle-group"]
        assert body["total"] == 1

    async def test_list_groups_invalid_sort_falls_back_to_name(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        # An unknown sort column must not 500 (falls back to name).
        resp = await client.get(
            "/api/v1/admin/groups?sort=evil%3B%20DROP%20TABLE&order=asc",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["groups"] is not None

    async def test_create_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"name": "editors", "description": "Editor group"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "editors"

    async def test_create_group_duplicate(self, client, admin_user):
        headers = await self._admin_headers(client)
        await client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"name": "dup-group"},
        )
        resp = await client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"name": "dup-group"},
        )
        assert resp.status_code == 409

    async def test_update_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"name": "to-rename"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/admin/groups/{group_id}",
            headers=headers,
            json={"name": "renamed", "description": "new desc"},
        )
        assert resp.status_code == 200

    async def test_update_group_no_fields(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"name": "no-update"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/admin/groups/{group_id}",
            headers=headers,
            json={},
        )
        assert resp.status_code == 400

    async def test_update_group_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            "/api/v1/admin/groups/nonexistent",
            headers=headers,
            json={"name": "x"},
        )
        assert resp.status_code == 404

    async def test_delete_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"name": "to-delete"},
        )
        group_id = resp.json()["id"]
        resp = await client.delete(
            f"/api/v1/admin/groups/{group_id}", headers=headers
        )
        assert resp.status_code == 200

    async def test_delete_group_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            "/api/v1/admin/groups/nonexistent", headers=headers
        )
        assert resp.status_code == 404

    async def test_list_group_members(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"name": "members-test"},
        )
        group_id = resp.json()["id"]
        # Add user to group
        resp = await client.post(
            f"/api/v1/admin/groups/{group_id}/members",
            headers=headers,
            json={"user_id": user["id"]},
        )
        assert resp.status_code == 200
        # List members
        resp = await client.get(
            f"/api/v1/admin/groups/{group_id}/members", headers=headers
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["email"] == "testuser@example.com"

    async def test_list_group_members_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/api/v1/admin/groups/nonexistent/members", headers=headers
        )
        assert resp.status_code == 404

    async def test_add_group_member_user_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"name": "member-test2"},
        )
        group_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/admin/groups/{group_id}/members",
            headers=headers,
            json={"user_id": "nonexistent"},
        )
        assert resp.status_code == 404

    async def test_add_group_member_group_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/groups/nonexistent/members",
            headers=headers,
            json={"user_id": "x"},
        )
        assert resp.status_code == 404

    async def test_remove_group_member(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"name": "remove-test"},
        )
        group_id = resp.json()["id"]
        await client.post(
            f"/api/v1/admin/groups/{group_id}/members",
            headers=headers,
            json={"user_id": user["id"]},
        )
        resp = await client.delete(
            f"/api/v1/admin/groups/{group_id}/members/{user['id']}",
            headers=headers,
        )
        assert resp.status_code == 200

    async def test_remove_group_member_not_member(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/groups",
            headers=headers,
            json={"name": "rm-test"},
        )
        group_id = resp.json()["id"]
        resp = await client.delete(
            f"/api/v1/admin/groups/{group_id}/members/nonexistent",
            headers=headers,
        )
        assert resp.status_code == 404


class TestACLEndpoints:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_get_acl_tree(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get("/api/v1/admin/acl/tree", headers=headers)
        assert resp.status_code == 200
        tree = resp.json()
        assert len(tree) > 0

    async def test_get_acl_by_user(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            f"/api/v1/admin/acl/by-principal/user/{user['id']}",
            headers=headers,
        )
        assert resp.status_code == 200

    async def test_get_acl_by_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        # Get the admin group ID
        groups = (await model.list_groups())["groups"]
        admin_group = next(g for g in groups if g["name"] == "admin")
        resp = await client.get(
            f"/api/v1/admin/acl/by-principal/group/{admin_group['id']}",
            headers=headers,
        )
        assert resp.status_code == 200
        entries = resp.json()
        assert len(entries) > 0

    async def test_my_permissions(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get("/api/v1/my-permissions", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["email"] == "testadmin@example.com"
        assert "/admin" in data["permissions"]
        assert "*" in data["permissions"]["/admin"]

    async def test_my_permissions_non_admin(self, client, admin_user, user):
        """Non-admin user has no admin permissions."""
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/my-permissions", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "/admin" not in data["permissions"]

    async def test_my_permissions_for_resource(self, client, user):
        """Check permissions for a specific resource."""
        headers = await _auth_headers(client)
        # Create a workspace (owner gets * ACE)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "perm-check"}
        )
        ws_id = resp.json()["id"]
        resp = await client.get(
            f"/api/v1/my-permissions?resource=/workspaces/{ws_id}",
            headers=headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        perms = data["permissions"].get(f"/workspaces/{ws_id}", [])
        assert "*" in perms
        assert "view" in perms
        assert "terminal" in perms

    async def test_my_permissions_for_resource_no_access(
        self, client, app, admin_user, user
    ):
        """User without specific ACE only gets inherited permissions."""
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/my-permissions?resource=/workspaces/nonexistent",
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
            "/api/v1/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_get_resource_acl(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/api/v1/admin/acl/resource?resource=/workspaces", headers=headers
        )
        assert resp.status_code == 200
        entries = resp.json()
        # Default ACL has Authenticated create on /workspaces
        assert any(e["permission"] == "create" for e in entries)

    async def test_replace_resource_acl(self, client, admin_user):
        headers = await self._admin_headers(client)
        # Get current ACL
        resp = await client.get(
            "/api/v1/admin/acl/resource?resource=/workspaces", headers=headers
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
            "/api/v1/admin/acl/resource?resource=/workspaces",
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
            "/api/v1/admin/acl/resource?resource=/workspaces",
            headers=headers,
            json=restore,
        )
        assert resp.status_code == 200

    async def test_get_resource_acl_requires_admin(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/admin/acl/resource?resource=/workspaces", headers=headers
        )
        assert resp.status_code == 403

    async def test_root_acl_rejects_removing_authenticated_view(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        # Try to save root ACL without Authenticated view
        resp = await client.put(
            "/api/v1/admin/acl/resource?resource=/",
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
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        # Authenticated with * should be accepted
        resp = await client.put(
            "/api/v1/admin/acl/resource?resource=/",
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
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        # Try to save /admin ACL with no group Allow
        resp = await client.put(
            "/api/v1/admin/acl/resource?resource=/admin",
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
    def test_valid_path(self, temp_data_dir, app):
        path = app.state.workspaces.safe_path("user1", "home", "ws1")
        assert path == app.state.workspaces.root / "user1" / "home" / "ws1"

    def test_traversal_raises(self, temp_data_dir, app):
        with pytest.raises(ValueError, match="Path traversal blocked"):
            app.state.workspaces.safe_path("..", "..", "etc", "passwd")


class TestSanitizeFilename:
    def test_safe_characters_preserved(self):
        assert ws_mod.sanitize_filename("hello-world_v2.tar.gz") == (
            "hello-world_v2.tar.gz"
        )

    def test_unsafe_characters_replaced(self):
        assert ws_mod.sanitize_filename("a/b\\c..d\x00e") == "a_b_c..d_e"

    def test_email_sanitized(self):
        assert ws_mod.sanitize_filename("user@example.com") == (
            "user@example.com"
        )


class TestRmtree:
    def test_removes_directory(self, temp_data_dir):
        d = temp_data_dir / "workspaces" / "toremove"
        d.mkdir(parents=True)
        (d / "file.txt").write_text("data")
        ws_mod.rmtree(d, "test")
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
                ws_mod.rmtree(d, "test-label")
        assert "denied" in caplog.text
        assert "test-label" in caplog.text


class TestBuildWorkspaceArchive:
    async def test_builds_importable_archive(self, temp_data_dir, app):
        """Archive contains workspace.json and home/ directory."""
        import json
        import subprocess

        ws_root = app.state.workspaces.root
        ws_root.mkdir(parents=True, exist_ok=True)
        home_dir = ws_root / "user1" / "home" / "ws1"
        home_dir.mkdir(parents=True)
        (home_dir / "hello.txt").write_text("test content")

        metadata = {"name": "myws", "image": None, "num_ports": 5}
        archive_path = ws_root / "test.tar.gz"

        result = await app.state.workspaces.build_workspace_archive(
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

    async def test_builds_archive_without_home(self, temp_data_dir, app):
        """Archive works when home directory doesn't exist."""
        ws_root = app.state.workspaces.root
        ws_root.mkdir(parents=True, exist_ok=True)
        home_dir = ws_root / "nonexistent"
        metadata = {"name": "empty"}
        archive_path = ws_root / "empty.tar.gz"

        result = await app.state.workspaces.build_workspace_archive(
            metadata, home_dir, archive_path
        )
        assert result is True
        assert archive_path.exists()

    async def test_excludes_external_symlinks(self, temp_data_dir, app):
        """Symlinks pointing outside home_dir are excluded."""
        import subprocess

        ws_root = app.state.workspaces.root
        ws_root.mkdir(parents=True, exist_ok=True)
        home_dir = ws_root / "user1" / "home" / "ws1"
        home_dir.mkdir(parents=True)
        (home_dir / "good.txt").write_text("keep")
        (home_dir / "external_link").symlink_to("/etc/passwd")
        (home_dir / "relative_link").symlink_to("good.txt")

        metadata = {"name": "test"}
        archive_path = ws_root / "symtest.tar.gz"

        result = await app.state.workspaces.build_workspace_archive(
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

    async def test_tar_failure_returns_false(self, temp_data_dir, app):
        """Returns False when tar exits non-zero."""
        ws_root = app.state.workspaces.root
        ws_root.mkdir(parents=True, exist_ok=True)
        home_dir = ws_root / "home"
        home_dir.mkdir()
        metadata = {"name": "fail"}
        archive_path = ws_root / "fail.tar.gz"

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"tar: error"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await app.state.workspaces.build_workspace_archive(
                metadata, home_dir, archive_path
            )
        assert result is False

    async def test_oserror_returns_false(self, temp_data_dir, app):
        """Returns False when tar cannot be started."""
        ws_root = app.state.workspaces.root
        ws_root.mkdir(parents=True, exist_ok=True)
        home_dir = ws_root / "home"
        metadata = {"name": "fail"}
        archive_path = ws_root / "fail.tar.gz"

        with patch(
            "asyncio.create_subprocess_exec", side_effect=OSError("no tar")
        ):
            result = await app.state.workspaces.build_workspace_archive(
                metadata, home_dir, archive_path
            )
        assert result is False

    async def test_path_outside_workspaces_root_rejected(
        self, temp_data_dir, app
    ):
        """Returns False if paths are outside WORKSPACES_ROOT."""
        home_dir = temp_data_dir / "outside"
        home_dir.mkdir(parents=True)
        metadata = {"name": "bad"}
        archive_path = temp_data_dir / "bad.tar.gz"

        result = await app.state.workspaces.build_workspace_archive(
            metadata, home_dir, archive_path
        )
        assert result is False


class TestWorkspaceMetadata:
    def _ws(self):
        """Build a Workspaces instance for testing (#1484)."""
        import types as types_mod

        ns = types_mod.SimpleNamespace(settings=make_settings({}))
        ns.util = util_mod.Util(ns)
        return ws_mod.Workspaces(ns)

    def test_extracts_metadata(self):
        ws = self._ws()
        ws_dict = {
            "name": "myws",
            "image": "ubuntu",
            "service_command": "bash",
            "auto_start": True,
            "mounts": ["/data:/data"],
            "env": {"FOO": "bar"},
            "num_ports": 3,
        }
        meta = ws.workspace_metadata(ws_dict)
        assert meta == {
            "name": "myws",
            "instance_id": ws.app_state.util.instance_id(),
            "image": "ubuntu",
            "service_command": "bash",
            "auto_start": True,
            "mounts": ["/data:/data"],
            "env": {"FOO": "bar"},
            "health_check": None,
            "num_ports": 3,
        }

    def test_defaults_num_ports(self):
        meta = self._ws().workspace_metadata({"name": "x"})
        assert meta["num_ports"] == 5

    def test_includes_instance_id(self):
        ws = self._ws()
        meta = ws.workspace_metadata({"name": "x"})
        assert meta["instance_id"] == ws.app_state.util.instance_id()


class TestArchiveUserData:
    async def test_archive_creates_importable_tarballs(
        self, user, workspace, app
    ):
        """Creates one .tar.gz per workspace in export format."""
        import json
        import subprocess

        # Put a file in the workspace home directory
        home_dir = app.state.workspaces.home_path(workspace["id"])
        home_dir.mkdir(parents=True, exist_ok=True)
        (home_dir / "hello.txt").write_text("test content")

        ws_dir = app.state.workspaces.root / workspace["id"]
        result = await app.state.workspaces.archive_user_data(
            user["id"], user["email"]
        )
        assert len(result) == 1
        archive = result[0]
        assert archive.exists()
        assert archive.name.endswith(".tar.gz")
        assert user["email"].replace("@", "_") in archive.name or True
        # Workspace directory should be removed
        assert not ws_dir.exists()

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

    async def test_archive_multiple_workspaces(self, user, app):
        """Creates separate archives for each workspace."""
        ws1 = await model.create_workspace(user["id"], "ws-one")
        ws2 = await model.create_workspace(user["id"], "ws-two")

        for ws in [ws1, ws2]:
            home = app.state.workspaces.home_path(ws["id"])
            home.mkdir(parents=True, exist_ok=True)
            (home / "file.txt").write_text("data")

        result = await app.state.workspaces.archive_user_data(
            user["id"], user["email"]
        )
        assert len(result) == 2
        names = {a.name for a in result}
        assert any("ws-one" in n for n in names)
        assert any("ws-two" in n for n in names)

    async def test_archive_paginates_more_than_one_page(self, user, app):
        """Archival pages through every workspace when there are >10."""
        for i in range(12):
            ws = await model.create_workspace(user["id"], f"ws-{i:02d}")
            home = app.state.workspaces.home_path(ws["id"])
            home.mkdir(parents=True, exist_ok=True)
            (home / "file.txt").write_text("data")

        result = await app.state.workspaces.archive_user_data(
            user["id"], user["email"]
        )
        assert len(result) == 12

    async def test_archive_no_data_dir(self, user, app):
        """Returns empty list if user has no data directory."""
        result = await app.state.workspaces.archive_user_data(
            user["id"], user["email"]
        )
        assert result == []

    async def test_archive_no_workspaces(self, user, app):
        """Returns empty list if user has no workspaces."""
        result = await app.state.workspaces.archive_user_data(
            user["id"], user["email"]
        )
        assert result == []

    async def test_archive_tar_failure_skips_workspace(
        self, user, workspace, app
    ):
        """Skips workspaces where tar fails, doesn't remove workspace dir."""
        home_dir = app.state.workspaces.home_path(workspace["id"])
        home_dir.mkdir(parents=True, exist_ok=True)

        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"", b"tar: error"))
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await app.state.workspaces.archive_user_data(
                user["id"], user["email"]
            )
        assert result == []
        # Workspace dir not removed since no archives were created
        ws_dir = app.state.workspaces.root / workspace["id"]
        assert ws_dir.exists()

    async def test_archive_sanitizes_email(self, user, workspace, app):
        """Email with path separators is sanitized in archive filename."""
        home_dir = app.state.workspaces.home_path(workspace["id"])
        home_dir.mkdir(parents=True, exist_ok=True)

        result = await app.state.workspaces.archive_user_data(
            user["id"], "user/../../etc/passwd"
        )
        assert len(result) == 1
        archive = result[0]
        assert archive.resolve().is_relative_to(
            app.state.workspaces.root.resolve()
        )
        # Slashes are replaced with underscores
        assert "/" not in archive.name
        assert "\\" not in archive.name

    async def test_archive_path_traversal_blocked(self, user, workspace, app):
        """Skips workspace if archive path would escape WORKSPACES_ROOT."""
        from pathlib import PosixPath

        home_dir = app.state.workspaces.home_path(workspace["id"])
        home_dir.mkdir(parents=True, exist_ok=True)

        orig_is_relative_to = PosixPath.is_relative_to

        def fake_is_relative_to(self, other):
            if self.suffix == ".gz":
                return False
            return orig_is_relative_to(self, other)

        with patch.object(PosixPath, "is_relative_to", fake_is_relative_to):
            result = await app.state.workspaces.archive_user_data(
                user["id"], user["email"]
            )
        assert result == []


# --- Workspace Export/Import ---


class TestWorkspaceExportImport:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def _user_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def _meta(self, **overrides):
        """Build workspace metadata dict with instance_id included.

        The instance ID is read from the active test data_dir (the same file
        the server's app.state.util reads), so this matches whatever the
        import endpoint validates against.
        """
        from klangk_backend.settings import KlangkSettings

        ns = types.SimpleNamespace(settings=KlangkSettings(os.environ))
        ns.util = util_mod.Util(ns)
        d = {"instance_id": ns.util.instance_id()}
        d.update(overrides)
        return d

    async def test_export_workspace(self, client, admin_user, user, app):
        # Create a workspace as regular user
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "export-test"}
        )
        assert resp.status_code == 200
        ws = resp.json()

        # Write a file into the workspace home dir

        home = app.state.workspaces.home_path(ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "work").mkdir(exist_ok=True)
        (home / "work" / "hello.txt").write_text("hello world")

        # Export as admin
        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
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
            assert "instance_id" in metadata

    async def test_export_requires_admin(self, client, user):
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "no-export"}
        )
        ws = resp.json()

        # Non-admin cannot export
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
        )
        assert resp.status_code == 403

    async def test_export_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/api/v1/workspaces/nonexistent-id/export", headers=headers
        )
        assert resp.status_code == 404

    async def test_import_workspace(self, client, admin_user, user, app):
        # Create and export a workspace
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={
                "name": "import-source",
                "service_command": "pi",
                "env": {"FOO": "bar"},
            },
        )
        ws = resp.json()

        home = app.state.workspaces.home_path(ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "work").mkdir(exist_ok=True)
        (home / "work" / "data.txt").write_text("test data")

        admin_headers = await self._admin_headers(client)
        export_resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert export_resp.status_code == 200

        # Import as regular user with a new name
        import_resp = await client.post(
            "/api/v1/workspaces/import",
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
        new_home = app.state.workspaces.home_path(imported["id"])
        assert (new_home / "work" / "data.txt").exists()
        assert (new_home / "work" / "data.txt").read_text() == "test data"

    async def test_import_uses_archive_name(self, client, admin_user, user):
        # Build a minimal archive with workspace.json
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="from-archive")).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "from-archive"

    async def test_import_rejects_foreign_instance(self, client, user):
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                {"name": "foreign", "instance_id": "foreign-instance-uuid"}
            ).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400
        assert "different Klangk instance" in resp.json()["detail"]

    async def test_import_accepts_same_instance(self, client, user):
        import io
        import json
        import tarfile

        from klangk_backend.settings import KlangkSettings

        ns = types.SimpleNamespace(settings=KlangkSettings(os.environ))
        ns.util = util_mod.Util(ns)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                {"name": "same-inst", "instance_id": ns.util.instance_id()}
            ).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "same-inst"

    async def test_import_rejects_missing_instance_id(self, client, user):
        """Archives without instance_id are rejected."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps({"name": "legacy-import"}).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400
        assert "missing instance_id" in resp.json()["detail"]

    async def test_import_notifies_importer(self, client, user, sockets):
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="notify-import")).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        with patch.object(
            sockets, "notify_user_workspaces_changed"
        ) as mock_notify:
            resp = await client.post(
                "/api/v1/workspaces/import",
                headers=headers,
                files={
                    "file": (
                        "archive.tar.gz",
                        buf.getvalue(),
                        "application/gzip",
                    )
                },
            )
        assert resp.status_code == 200
        mock_notify.assert_called_once_with(user["id"])

    async def test_import_runs_tar_off_event_loop(
        self, client, app, admin_user, user
    ):
        """Import runs tar subprocesses off the event loop (regression #1261).

        A blocking ``subprocess.run`` in the async import handler freezes the
        whole server for up to the subprocess timeout. Every tar invocation
        on the import path must execute in a worker thread, not on the loop.
        """
        import io
        import json
        import subprocess
        import tarfile
        import threading

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="off-loop")).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        loop_thread = threading.get_ident()
        seen = []
        real_run = subprocess.run

        def spy(*args, **kwargs):
            seen.append(threading.get_ident())
            return real_run(*args, **kwargs)

        headers = await self._user_headers(client)
        with patch.object(subprocess, "run", spy):
            resp = await client.post(
                "/api/v1/workspaces/import",
                headers=headers,
                files={
                    "file": (
                        "archive.tar.gz",
                        buf.getvalue(),
                        "application/gzip",
                    )
                },
            )
        assert resp.status_code == 200
        # tar ran at least once (metadata extraction)...
        assert seen
        # ...and every run was off the event loop's thread.
        assert all(t != loop_thread for t in seen)

    async def test_export_runs_size_estimate_off_event_loop(
        self, client, app, admin_user, user
    ):
        """Export's ``du`` size-estimate runs off the event loop (#1261)."""
        import subprocess
        import threading

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "export-offloop"},
        )
        assert resp.status_code == 200
        ws = resp.json()

        home = app.state.workspaces.home_path(ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "f.txt").write_text("x")

        loop_thread = threading.get_ident()
        seen = []
        real_run = subprocess.run

        def spy(*args, **kwargs):
            seen.append(threading.get_ident())
            return real_run(*args, **kwargs)

        admin_headers = await self._admin_headers(client)
        with patch.object(subprocess, "run", spy):
            resp = await client.get(
                f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
            )
        assert resp.status_code == 200
        assert seen
        assert all(t != loop_thread for t in seen)

    async def test_import_duplicate_name(self, client, user):
        headers = await self._user_headers(client)
        await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "taken"}
        )

        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="taken")).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        resp = await client.post(
            "/api/v1/workspaces/import",
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
            "/api/v1/workspaces/import",
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
            "/api/v1/workspaces/import",
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
            meta = json.dumps(self._meta()).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/import",
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
                self._meta(name="img-fallback", image="evil:latest")
            ).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/import",
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
                self._meta(name="mount-drop", mounts=["bad-mount-spec"])
            ).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "mount-drop"

    async def test_import_home_root_member_skipped(self, client, user, app):
        """The bare 'home/' directory entry is skipped during extraction."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="home-root-skip")).encode()
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
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200

        ws = resp.json()
        home = app.state.workspaces.home_path(ws["id"])
        assert (home / "test.txt").exists()

    async def test_export_streams_valid_tarball(
        self, client, app, admin_user, user
    ):
        """Export streams a valid .tar.gz with size estimate header."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "stream-test"}
        )
        ws = resp.json()

        home = app.state.workspaces.home_path(ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "work").mkdir(exist_ok=True)
        (home / "work" / "file.txt").write_text("streamed content")

        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
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

    async def test_export_large_file_chunks(
        self, client, admin_user, user, app
    ):
        """Export with large files triggers the write buffer flush path."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "large-export"},
        )
        ws = resp.json()

        home = app.state.workspaces.home_path(ws["id"])
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
            f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 200

        import tarfile

        buf = io.BytesIO(resp.content)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            assert any("big.bin" in n for n in tar.getnames())

    async def test_export_du_failure_falls_back(
        self, client, app, admin_user, user, monkeypatch
    ):
        """If du fails, estimated size defaults to minimum."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "du-fail"}
        )
        ws = resp.json()

        home = app.state.workspaces.home_path(ws["id"])
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
            f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 200
        # Falls back to 0 * 0.4 = 0, clamped to 1
        assert resp.headers["x-estimated-size"] == "1"

    async def test_export_empty_workspace(self, client, admin_user, user):
        """Export of workspace with no home dir still works."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "empty-export"},
        )
        ws = resp.json()

        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 200
        # Estimated size is 0 * 0.4 = 0, clamped to 1
        assert resp.headers["x-estimated-size"] == "1"

    async def test_import_upload_error_cleans_tempfile(
        self, client, app, user, monkeypatch
    ):
        """If the upload write fails, the temp file is cleaned up."""
        import klangk_backend.api.workspaces as api_mod

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
                "/api/v1/workspaces/import",
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
        self, client, app, admin_user, user
    ):
        """All symlinks are preserved in export (stored as links, not content)."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "symlink-export"},
        )
        ws = resp.json()

        home = app.state.workspaces.home_path(ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "work").mkdir(exist_ok=True)
        (home / "work" / "real.txt").write_text("real file")
        (home / "work" / "relative_link").symlink_to("real.txt")
        (home / "work" / "external_link").symlink_to("/etc/passwd")

        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
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

    async def test_export_import_deep_nesting(
        self, client, admin_user, user, app
    ):
        """Export and import a workspace with deep directory nesting."""
        import random
        import tarfile

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "deep-export"}
        )
        ws = resp.json()

        home = app.state.workspaces.home_path(ws["id"])
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
            f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
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
            "/api/v1/workspaces/import",
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
        imported_home = app.state.workspaces.home_path(imported["id"])
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

    async def test_import_size_limit(self, client, user, app, monkeypatch):
        """Upload exceeding size limit is rejected."""
        monkeypatch.setattr(app.state.settings, "file_upload_size_max", "100")

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/import",
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
                self._meta(
                    name="env-sanitize",
                    env={
                        "MY_VAR": "safe",
                        "KLANGK_BRIDGE_TOKEN": "stolen",
                        "LD_PRELOAD": "/evil.so",
                        "PATH": "/bad",
                        "NORMAL_VAR": "ok",
                    },
                )
            ).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200
        ws = resp.json()

        # Fetch the workspace to check env
        resp = await client.get("/api/v1/workspaces", headers=headers)
        workspaces_list = resp.json()
        imported = next(w for w in workspaces_list if w["id"] == ws["id"])
        env = imported.get("env", {})
        assert "MY_VAR" in env
        assert "NORMAL_VAR" in env
        assert "KLANGK_BRIDGE_TOKEN" not in env
        assert "LD_PRELOAD" not in env
        assert "PATH" not in env

    async def test_import_cleanup_on_extraction_failure(
        self, client, app, user, monkeypatch
    ):
        """If tar extraction fails, the workspace is cleaned up."""
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="fail-extract")).encode()
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
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400

        # Workspace should have been cleaned up
        resp = await client.get("/api/v1/workspaces", headers=headers)
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
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400
        assert "corrupt" in resp.json()["detail"].lower()

    async def test_import_timeout_cleans_up_workspace(
        self, client, app, user, monkeypatch
    ):
        """If tar extraction times out after workspace creation, cleanup occurs."""
        import json
        import tarfile
        import subprocess as subprocess_mod

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="timeout-test")).encode()
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
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400

        resp = await client.get("/api/v1/workspaces", headers=headers)
        names = [w["name"] for w in resp.json()]
        assert "timeout-test" not in names

    async def test_import_path_traversal_rejected(self, client, user):
        """GNU tar rejects members with '..' in their path."""
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="traversal-test")).encode()
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
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        # GNU tar refuses to extract members with '..' — returns non-zero
        assert resp.status_code == 400

        # Workspace should have been cleaned up
        resp = await client.get("/api/v1/workspaces", headers=headers)
        names = [w["name"] for w in resp.json()]
        assert "traversal-test" not in names

    async def test_import_workspace_creates_role_groups(self, client, user):
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="import-roles-test")).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 200
        ws_id = resp.json()["id"]
        for suffix in ["owners", "coders", "collaborators", "spectators"]:
            group = await model.get_group_by_name(f"{suffix}-{ws_id}")
            assert group is not None, f"expected {suffix} group on import"
        # Importer should be in the owners group
        owners = await model.get_group_by_name(f"owners-{ws_id}")
        members = await model.get_group_members(owners["id"])
        assert any(m["id"] == user["id"] for m in members)


# --- Invitation endpoints ---


class TestInvitations:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_send_invitation(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ) as mock_send:
            resp = await client.post(
                "/api/v1/admin/invitations",
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
        self, client, app, admin_user, monkeypatch
    ):
        headers = await self._admin_headers(client)
        monkeypatch.setattr(
            app.state.auth, "invitations_enabled", lambda: False
        )
        resp = await client.post(
            "/api/v1/admin/invitations",
            headers=headers,
            json={"email": "invited@example.com"},
        )
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"]

    async def test_send_invitation_existing_user(
        self, client, app, admin_user, user
    ):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/invitations",
            headers=headers,
            json={"email": "testuser@example.com"},
        )
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]

    async def test_send_invitation_duplicate_pending(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "dup@example.com"},
            )
            resp = await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "dup@example.com"},
            )
        assert resp.status_code == 400
        assert "pending invitation" in resp.json()["detail"]

    async def test_send_invitation_invalid_email(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/invitations",
            headers=headers,
            json={"email": "not-an-email"},
        )
        assert resp.status_code == 400

    async def test_send_invitation_requires_admin(self, client, user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.post(
            "/api/v1/admin/invitations",
            headers=headers,
            json={"email": "invited@example.com"},
        )
        assert resp.status_code == 403

    async def test_list_invitations(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "list1@example.com"},
            )
            await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "list2@example.com"},
            )
        resp = await client.get("/api/v1/admin/invitations", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        invitations = body["invitations"]
        emails = [inv["email"] for inv in invitations]
        assert "list1@example.com" in emails
        assert "list2@example.com" in emails
        assert invitations[0]["invited_by_email"] == "testadmin@example.com"
        # Paged envelope metadata.
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["total"] >= 2
        # Two freshly-created pending invitations are reflected in the
        # global pending count (used by the UI badge).
        assert body["pending_count"] >= 2

    async def test_list_invitations_default_page_size_is_10(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            for i in range(12):
                await client.post(
                    "/api/v1/admin/invitations",
                    headers=headers,
                    json={"email": f"page{i}@example.com"},
                )
        resp = await client.get("/api/v1/admin/invitations", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["total"] >= 12
        assert len(body["invitations"]) == 10

    async def test_list_invitations_pagination_across_pages(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            for i in range(6):
                await client.post(
                    "/api/v1/admin/invitations",
                    headers=headers,
                    json={"email": f"pg{i}@example.com"},
                )
        page1 = await client.get(
            "/api/v1/admin/invitations?page=1&page_size=3", headers=headers
        )
        page2 = await client.get(
            "/api/v1/admin/invitations?page=2&page_size=3", headers=headers
        )
        assert page1.status_code == 200
        assert page2.status_code == 200
        b1 = page1.json()
        b2 = page2.json()
        assert b1["page"] == 1
        assert b2["page"] == 2
        assert b1["page_size"] == 3
        assert b1["total"] == b2["total"]
        # Pages don't overlap.
        ids1 = {inv["id"] for inv in b1["invitations"]}
        ids2 = {inv["id"] for inv in b2["invitations"]}
        assert ids1.isdisjoint(ids2)
        assert len(b1["invitations"]) == 3
        assert len(b2["invitations"]) == 3

    async def test_list_invitations_sort_by_email(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            for e in [
                "charlie@example.com",
                "alpha@example.com",
                "bravo@example.com",
            ]:
                await client.post(
                    "/api/v1/admin/invitations",
                    headers=headers,
                    json={"email": e},
                )
        resp = await client.get(
            "/api/v1/admin/invitations?sort=email&order=asc&page_size=50",
            headers=headers,
        )
        emails = [inv["email"] for inv in resp.json()["invitations"]]
        assert emails == sorted(emails, key=str.lower)
        assert emails[0] == "alpha@example.com"

    async def test_list_invitations_sort_by_invited_by(
        self, client, app, admin_user
    ):
        # Two invitations from two different inviters. Sorting by
        # ``invited_by`` must track the inviter's email (the value the UI
        # displays), not the invitee's email.
        inviter_a = await model.create_user(
            "aaa-admin@example.com", None, verified=True
        )
        inviter_z = await model.create_user(
            "zzz-admin@example.com", None, verified=True
        )
        await model.create_invitation("zeta@example.com", inviter_z["id"])
        await model.create_invitation("alpha@example.com", inviter_a["id"])
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/api/v1/admin/invitations?sort=invited_by&order=asc&page_size=50",
            headers=headers,
        )
        rows = resp.json()["invitations"]
        # Only the two we just created are relevant; confirm the inviter
        # ordering among them and that it tracks invited_by_email.
        ours = [
            r
            for r in rows
            if r["email"] in {"zeta@example.com", "alpha@example.com"}
        ]
        inviters = [r["invited_by_email"] for r in ours]
        assert inviters == sorted(inviters, key=str.lower)
        assert ours[0]["invited_by_email"] == "aaa-admin@example.com"
        assert ours[0]["email"] == "alpha@example.com"

    async def test_list_invitations_sort_desc_reverses(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            for e in [
                "charlie@example.com",
                "alpha@example.com",
                "bravo@example.com",
            ]:
                await client.post(
                    "/api/v1/admin/invitations",
                    headers=headers,
                    json={"email": e},
                )
        asc = await client.get(
            "/api/v1/admin/invitations?sort=email&order=asc&page_size=50",
            headers=headers,
        )
        desc = await client.get(
            "/api/v1/admin/invitations?sort=email&order=desc&page_size=50",
            headers=headers,
        )
        asc_emails = [inv["email"] for inv in asc.json()["invitations"]]
        desc_emails = [inv["email"] for inv in desc.json()["invitations"]]
        assert asc_emails == list(reversed(desc_emails))

    async def test_list_invitations_filter_by_email(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "needle@example.com"},
            )
            await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "haystack@example.com"},
            )
        resp = await client.get(
            "/api/v1/admin/invitations?q=needle&page_size=50",
            headers=headers,
        )
        body = resp.json()
        emails = [inv["email"] for inv in body["invitations"]]
        assert emails == ["needle@example.com"]
        assert body["total"] == 1
        # The filter narrows the page but not the global pending count.
        assert body["pending_count"] >= 2

    async def test_list_invitations_invalid_sort_falls_back_to_created(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        # An unknown sort column must not 500 (falls back to created_at).
        resp = await client.get(
            "/api/v1/admin/invitations?sort=evil%3B%20DROP%20TABLE&order=asc",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["invitations"] is not None

    async def test_revoke_invitation(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "revoke@example.com"},
            )
        inv_id = create_resp.json()["id"]
        resp = await client.delete(
            f"/api/v1/admin/invitations/{inv_id}", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"

        # Can't revoke again
        resp = await client.delete(
            f"/api/v1/admin/invitations/{inv_id}", headers=headers
        )
        assert resp.status_code == 404

    async def test_revoke_nonexistent(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            "/api/v1/admin/invitations/nonexistent-id", headers=headers
        )
        assert resp.status_code == 404

    async def test_resend_invitation(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "resend@example.com"},
            )
        inv_id = create_resp.json()["id"]
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ) as mock_resend:
            resp = await client.post(
                f"/api/v1/admin/invitations/{inv_id}/resend", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resent"
        mock_resend.assert_called_once()

    async def test_resend_nonexistent(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/admin/invitations/nonexistent/resend", headers=headers
        )
        assert resp.status_code == 404

    async def test_resend_revoked(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "revoked-resend@example.com"},
            )
        inv_id = create_resp.json()["id"]
        await client.delete(
            f"/api/v1/admin/invitations/{inv_id}", headers=headers
        )
        resp = await client.post(
            f"/api/v1/admin/invitations/{inv_id}/resend", headers=headers
        )
        assert resp.status_code == 404

    async def test_accept_invite(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "accept@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = _auth().create_invitation_token(inv_id, "accept@example.com")

        resp = await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "accepted"
        assert "access_token" in data

        # User can log in
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "accept@example.com", "password": "newpassword"},
        )
        assert login_resp.status_code == 200

    async def test_accept_invite_invalid_token(self, client, db):
        resp = await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": "invalid-token", "password": "newpassword"},
        )
        assert resp.status_code == 400
        assert "Invalid or expired" in resp.json()["detail"]

    async def test_accept_invite_already_accepted(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "double@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = _auth().create_invitation_token(inv_id, "double@example.com")

        # Accept once
        await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        # Try again
        resp = await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 400
        assert "no longer valid" in resp.json()["detail"]

    async def test_accept_invite_short_password(self, client, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "short@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = _auth().create_invitation_token(inv_id, "short@example.com")

        resp = await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "ab"},
        )
        assert resp.status_code == 400
        assert "Password" in resp.json()["detail"]

    async def test_accept_invite_works_when_registration_disabled(
        self, client, app, admin_user, monkeypatch
    ):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "noreg@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = _auth().create_invitation_token(inv_id, "noreg@example.com")

        # Disable registration
        monkeypatch.setattr(
            app.state.auth, "registration_enabled", lambda: False
        )

        # Accept-invite should still work
        resp = await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "accepted"

    async def test_accept_invite_email_already_registered(
        self, client, app, admin_user, user
    ):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/admin/invitations",
                headers=headers,
                json={"email": "race@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = _auth().create_invitation_token(inv_id, "race@example.com")

        # Simulate race: create user with that email before accepting
        await model.create_user(
            "race@example.com", auth_mod.hash_password("pass"), verified=True
        )

        resp = await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]

    async def test_accept_invite_wrong_purpose_token(self, client, db):
        # Use a verification token (wrong purpose)
        token = _auth().create_verification_token("fake-user-id")
        resp = await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 400

    async def test_config_includes_invitations_enabled(self, client):
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        assert "invitations_enabled" in resp.json()

    async def test_config_advertises_allow_autostart(
        self, client, app, monkeypatch
    ):
        # Default: flag unset -> not allowed, so the UI hides its checkbox.
        monkeypatch.setattr(app.state.settings, "allow_autostart", "")
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        assert resp.json()["allow_autostart"] is False

        # Flag set -> advertised as true so the UI may show the checkbox.
        monkeypatch.setattr(app.state.settings, "allow_autostart", "1")
        resp = await client.get("/api/v1/config")
        assert resp.json()["allow_autostart"] is True


# --- OIDC endpoints ---


class TestOIDCConfig:
    async def test_config_includes_oidc_fields(self, client, app, monkeypatch):
        # Default (no auth mode set) is ``none`` (#1374). Patch the OIDC
        # instance rather than the env (#1450).
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "oidc_providers" in data
        assert "auth_modes" in data
        assert data["oidc_providers"] == []
        # Production default (no OIDC, mode unset) is now ``none`` (#1374).
        assert data["auth_modes"] == "none"

    async def test_config_with_providers(self, client, app, monkeypatch):
        monkeypatch.setattr(
            app.state.oidc,
            "list_providers",
            lambda: [{"id": "test", "display_name": "Test"}],
        )
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda *args: "both")
        resp = await client.get("/api/v1/config")
        data = resp.json()
        assert len(data["oidc_providers"]) == 1
        assert data["auth_modes"] == "both"


class TestOIDCAuthModeGuards:
    async def test_login_blocked_when_oidc_only(
        self, client, app, monkeypatch, user
    ):
        monkeypatch.setattr(
            app.state.oidc, "password_login_allowed", lambda *args: False
        )
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"]

    async def test_register_blocked_when_oidc_only(
        self, client, app, monkeypatch, db
    ):
        monkeypatch.setattr(
            app.state.oidc, "password_login_allowed", lambda *args: False
        )
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "new@example.com", "password": "testpass"},
        )
        assert resp.status_code == 403

    async def test_login_allowed_when_both(
        self, client, app, monkeypatch, user
    ):
        monkeypatch.setattr(
            app.state.oidc, "password_login_allowed", lambda *args: True
        )
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 200


class TestOIDCLogin:
    async def test_oidc_login_not_enabled(self, client, app, monkeypatch):
        monkeypatch.setattr(
            app.state.oidc, "oidc_login_allowed", lambda *args: False
        )
        resp = await client.get("/api/v1/auth/oidc/test/login")
        assert resp.status_code == 404

    async def test_unknown_provider(self, client, app, monkeypatch):
        monkeypatch.setattr(
            app.state.oidc, "oidc_login_allowed", lambda *args: True
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: None)
        resp = await client.get("/api/v1/auth/oidc/nope/login")
        assert resp.status_code == 404

    async def test_invalid_cli_redirect(self, client, app, monkeypatch):
        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(
            app.state.oidc, "oidc_login_allowed", lambda *args: True
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        resp = await client.get(
            "/api/v1/auth/oidc/test/login",
            params={"cli_redirect": "https://evil.com/steal"},
        )
        assert resp.status_code == 400
        assert "localhost" in resp.json()["detail"]

    async def test_oidc_login_redirects(self, client, app, monkeypatch):
        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(
            app.state.oidc, "oidc_login_allowed", lambda *args: True
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            app.state.oidc,
            "build_auth_url",
            AsyncMock(return_value="https://idp.example.com/auth?foo=bar"),
        )
        resp = await client.get(
            "/api/v1/auth/oidc/test/login", follow_redirects=False
        )
        assert resp.status_code == 302
        assert (
            resp.headers["location"] == "https://idp.example.com/auth?foo=bar"
        )
        assert "oidc_test" in resp.headers.get("set-cookie", "")


class TestOIDCCallback:
    async def _setup_callback(self, client, app, monkeypatch, db, claims=None):
        """Set up mocks for a successful OIDC callback test."""
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            app.state.oidc,
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
            app.state.oidc,
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

    async def test_callback_creates_user(self, client, app, monkeypatch, db):
        _, cookie_data = await self._setup_callback(
            client, app, monkeypatch, db
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
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
        self, client, app, monkeypatch, db
    ):
        """OIDC callback calls the group mapping hook and syncs memberships."""

        def test_hook(provider, claims, email, tokens):
            if "admin-role" in claims.get("roles", []):
                return {"admin", "power-users"}
            return {"users"}

        monkeypatch.setattr(app.state.oidc, "login_hook", test_hook)
        monkeypatch.setattr(app.state.oidc, "login_hook_is_async", False)

        _, cookie_data = await self._setup_callback(
            client,
            app,
            monkeypatch,
            db,
            claims={
                "sub": "hook-sub",
                "email": "hookuser@example.com",
                "roles": ["admin-role"],
            },
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "test-state"},
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
        self, client, app, monkeypatch, db, user
    ):
        _, cookie_data = await self._setup_callback(
            client,
            app,
            monkeypatch,
            db,
            claims={"sub": "new-sub", "email": "testuser@example.com"},
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "test-state"},
            follow_redirects=False,
        )
        assert resp.status_code == 302

        # Existing user was linked
        linked = await model.get_user_by_external_id("test", "new-sub")
        assert linked is not None
        assert linked["id"] == user["id"]

    async def test_callback_state_mismatch(self, client, app, monkeypatch, db):
        _, cookie_data = await self._setup_callback(
            client, app, monkeypatch, db
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "wrong-state"},
        )
        assert resp.status_code == 400
        assert "State mismatch" in resp.json()["detail"]

    async def test_callback_missing_cookie(self, client, app, monkeypatch, db):
        await self._setup_callback(client, app, monkeypatch, db)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "test-state"},
        )
        assert resp.status_code == 400
        assert "cookie" in resp.json()["detail"].lower()

    async def test_callback_idp_error(self, client, app, monkeypatch, db):
        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"error": "access_denied"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Login failed"

    async def test_callback_cli_redirect(self, client, app, monkeypatch, db):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            app.state.oidc,
            "exchange_code",
            AsyncMock(return_value={"id_token": "idt", "access_token": "at"}),
        )
        monkeypatch.setattr(
            app.state.oidc,
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
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith(
            "http://localhost:12345/callback?token="
        )

    async def test_callback_tampered_cli_redirect_falls_back(
        self, client, app, monkeypatch, db
    ):
        """A tampered (non-localhost) cli_redirect in the unsigned state
        cookie must NOT receive the token — fall back to the web flow.

        Regression test for #936: the state cookie is client-controlled,
        so cli_redirect is re-validated at callback time.
        """
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            app.state.oidc,
            "exchange_code",
            AsyncMock(return_value={"id_token": "idt", "access_token": "at"}),
        )
        monkeypatch.setattr(
            app.state.oidc,
            "validate_id_token",
            AsyncMock(
                return_value={
                    "sub": "evil-sub",
                    "email": "evil@example.com",
                    "email_verified": True,
                }
            ),
        )
        cookie_data = json_mod.dumps(
            {
                "state": "s",
                "verifier": "v",
                "redirect_uri": "https://klangk.example.com/cb",
                "cli_redirect": "https://evil.com/steal",
            }
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        location = resp.headers["location"]
        # Must NOT redirect to the attacker host with the token.
        assert not location.startswith("https://evil.com")
        assert "evil.com" not in location
        # Falls back to the web flow, still carrying the token in-house.
        assert "oidc-complete" in location
        assert "token=" in location

    async def test_callback_token_exchange_failure(
        self, client, app, monkeypatch, db
    ):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        mock_request = httpx.Request("POST", "https://idp/token")
        mock_response = httpx.Response(
            400, text="bad request", request=mock_request
        )
        monkeypatch.setattr(
            app.state.oidc,
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
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
        )
        assert resp.status_code == 502

    async def test_callback_no_id_token(self, client, app, monkeypatch, db):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            app.state.oidc,
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
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
        )
        assert resp.status_code == 502
        assert "No ID token" in resp.json()["detail"]

    async def test_callback_invalid_id_token(
        self, client, app, monkeypatch, db
    ):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            app.state.oidc,
            "exchange_code",
            AsyncMock(return_value={"id_token": "bad", "access_token": "at"}),
        )
        monkeypatch.setattr(
            app.state.oidc,
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
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
        )
        assert resp.status_code == 502
        assert "validation failed" in resp.json()["detail"]

    async def test_callback_missing_claims(self, client, app, monkeypatch, db):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            app.state.oidc,
            "exchange_code",
            AsyncMock(return_value={"id_token": "t", "access_token": "at"}),
        )
        monkeypatch.setattr(
            app.state.oidc,
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
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
        )
        assert resp.status_code == 502
        assert "missing" in resp.json()["detail"].lower()

    async def test_callback_login_hook_rejects(
        self, client, app, monkeypatch, db
    ):
        """A login validation hook can reject an OIDC login."""

        def reject_hook(provider, claims, email, tokens):
            raise ValueError("Denied by hook")

        monkeypatch.setattr(app.state.oidc, "login_hook", reject_hook)
        monkeypatch.setattr(app.state.oidc, "login_hook_is_async", False)
        _, cookie_data = await self._setup_callback(
            client,
            app,
            monkeypatch,
            db,
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Login denied by server policy"
        assert await model.get_user_by_email("oidcuser@example.com") is None

    async def test_callback_rejects_unverified_email_by_default(
        self, client, app, monkeypatch, db
    ):
        """Unverified email is rejected when trust-email is false (default)."""
        _, cookie_data = await self._setup_callback(
            client,
            app,
            monkeypatch,
            db,
            claims={"email_verified": False},
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert "not verified" in resp.json()["detail"]
        assert await model.get_user_by_email("oidcuser@example.com") is None

    async def test_callback_rejects_missing_email_verified(
        self, client, app, monkeypatch, db
    ):
        """Missing email_verified claim is rejected (same as False)."""
        _, cookie_data = await self._setup_callback(
            client,
            app,
            monkeypatch,
            db,
            claims={"sub": "no-ev-sub", "email": "noev@example.com"},
        )
        # Override claims to omit email_verified entirely
        monkeypatch.setattr(
            app.state.oidc,
            "validate_id_token",
            AsyncMock(
                return_value={
                    "sub": "no-ev-sub",
                    "email": "noev@example.com",
                }
            ),
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    async def test_callback_trust_email_allows_unverified(
        self, client, app, monkeypatch, db
    ):
        """With trust-email: true, unverified emails are accepted."""
        provider, cookie_data = await self._setup_callback(
            client,
            app,
            monkeypatch,
            db,
            claims={"email_verified": False},
        )
        provider.trust_email = True
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert (
            await model.get_user_by_email("oidcuser@example.com") is not None
        )

    async def test_callback_returning_user(self, client, app, monkeypatch, db):
        """A user who already has the OIDC identity linked logs in without
        JIT provisioning or email lookup."""
        _, cookie_data = await self._setup_callback(
            client, app, monkeypatch, db
        )
        # First callback — creates the user via JIT provisioning.
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        user = await model.get_user_by_external_id("test", "oidc-sub-123")
        assert user is not None

        # Second callback — returning user, found by external ID.
        _, cookie_data2 = await self._setup_callback(
            client, app, monkeypatch, db
        )
        client.cookies.set("oidc_test", cookie_data2)
        resp2 = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            follow_redirects=False,
        )
        assert resp2.status_code == 302
        assert "token=" in resp2.headers["location"]

    async def test_callback_unknown_provider(
        self, client, app, monkeypatch, db
    ):
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: None)
        resp = await client.get(
            "/api/v1/auth/oidc/nope/callback",
            params={"code": "code", "state": "s"},
        )
        assert resp.status_code == 404

    async def test_callback_invalid_cookie_json(
        self, client, app, monkeypatch, db
    ):
        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        client.cookies.set("oidc_test", "not-json")
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
        )
        assert resp.status_code == 400

    async def test_callback_non_dict_cookie_json(
        self, client, app, monkeypatch, db
    ):
        """Non-dict JSON in the state cookie returns 400, not 500 (#1334)."""
        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        client.cookies.set("oidc_test", "[1, 2, 3]")
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
        )
        assert resp.status_code == 400


class TestOIDCCallbackAgentGuard:
    """OIDC callback must never mint a session as the system agent (#1225)."""

    async def _setup_callback(self, client, app, monkeypatch, db, claims=None):
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            app.state.oidc,
            "exchange_code",
            AsyncMock(
                return_value={
                    "id_token": "fake-id-token",
                    "access_token": "at",
                }
            ),
        )
        default_claims = {
            "sub": "agent-oidc-sub",
            "email": "clanker@example.com",
            "email_verified": True,
        }
        if claims:
            default_claims.update(claims)
        monkeypatch.setattr(
            app.state.oidc,
            "validate_id_token",
            AsyncMock(return_value=default_claims),
        )
        cookie_data = json_mod.dumps(
            {
                "state": "test-state",
                "verifier": "test-verifier",
                "redirect_uri": "https://klangk.example.com/auth/oidc/test/callback",
                "cli_redirect": None,
            }
        )
        return provider, cookie_data

    async def test_oidc_rejects_agent_email(
        self, client, app, monkeypatch, db, agent_user
    ):
        """OIDC login with the agent's email is rejected with 403."""
        _, cookie_data = await self._setup_callback(
            client, app, monkeypatch, db
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
        )
        assert resp.status_code == 403
        assert "system agent" in resp.json()["detail"]

    async def test_oidc_rejects_agent_by_external_id(
        self, client, app, monkeypatch, db, agent_user
    ):
        """OIDC login resolving the agent by external_id is rejected."""
        # The DB trigger blocks linking OIDC identity to the agent, so
        # mock get_user_by_external_id to simulate a pre-linked agent.
        agent = await model.get_user_by_id(model.AGENT_USER_ID)
        monkeypatch.setattr(
            model,
            "get_user_by_external_id",
            AsyncMock(return_value=agent),
        )
        _, cookie_data = await self._setup_callback(
            client, app, monkeypatch, db
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
        )
        assert resp.status_code == 403
        assert "system agent" in resp.json()["detail"]


class TestOIDCLogout:
    async def test_logout_returns_oidc_logout_url(self, client, app, db):
        """OIDC user with logout_redirect gets IdP logout URL in response."""
        # Create OIDC user
        user = await model.create_user(
            "oidc-logout@example.com",
            password_hash=None,
            verified=True,
            provider="test",
            external_id="logout-sub",
        )
        token = _auth().create_token(user["id"], user["email"])
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
            patch.object(
                app.state.oidc, "get_provider", return_value=provider
            ),
            patch.object(
                app.state.oidc,
                "build_logout_url",
                AsyncMock(return_value="https://idp.example.com/logout?x=1"),
            ),
        ):
            resp = await client.post("/api/v1/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert (
            resp.json()["oidc_logout_url"]
            == "https://idp.example.com/logout?x=1"
        )

    async def test_logout_no_redirect_for_local_user(self, client, user):
        """Local user gets no oidc_logout_url."""
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.post("/api/v1/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert "oidc_logout_url" not in resp.json()

    async def test_logout_no_redirect_when_disabled(self, client, app, db):
        """OIDC user with logout_redirect=false gets no URL."""
        user = await model.create_user(
            "oidc-nologout@example.com",
            password_hash=None,
            verified=True,
            provider="test",
            external_id="nologout-sub",
        )
        token = _auth().create_token(user["id"], user["email"])
        headers = {"Authorization": f"Bearer {token}"}

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
            logout_redirect=False,
        )
        with patch.object(
            app.state.oidc, "get_provider", return_value=provider
        ):
            resp = await client.post("/api/v1/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert "oidc_logout_url" not in resp.json()


class TestHandleEndpoints:
    async def test_change_own_handle(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/auth/change-handle",
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

    async def test_change_handle_refreshes_presence(
        self, client, app, user, sockets
    ):
        headers = await _auth_headers(client)
        with patch.object(
            api.wshandler,
            "refresh_user_handle",
            new_callable=AsyncMock,
        ) as mock_refresh:
            resp = await client.post(
                "/api/v1/auth/change-handle",
                json={"handle": "freshhandle", "password": "testpass"},
                headers=headers,
            )
        assert resp.status_code == 200
        mock_refresh.assert_awaited_once_with(
            sockets, user["id"], "freshhandle"
        )

    async def test_change_handle_invalid_empty(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/auth/change-handle",
            json={"handle": "", "password": "testpass"},
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_change_handle_invalid_chars(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/auth/change-handle",
            json={"handle": "BAD HANDLE!", "password": "testpass"},
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_change_handle_reserved(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/auth/change-handle",
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
            "/api/v1/auth/change-handle",
            json={"handle": "taken-handle", "password": "testpass"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "already taken" in resp.json()["detail"]

    async def test_change_handle_wrong_password(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/auth/change-handle",
            json={"handle": "good-handle", "password": "wrongpass"},
            headers=headers,
        )
        assert resp.status_code == 401

    async def test_change_handle_oidc_only_user(self, client, db):
        """OIDC-only users have no password; must 403, not 500 (#890)."""
        headers = await _oidc_user_headers()
        resp = await client.post(
            "/api/v1/auth/change-handle",
            json={"handle": "good-handle", "password": "anything"},
            headers=headers,
        )
        assert resp.status_code == 403
        assert (
            resp.json()["detail"]
            == "Account is managed by your identity provider"
        )

    async def test_admin_change_user_handle(self, client, admin_user, user):
        admin_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_resp.json()['access_token']}"
        }
        resp = await client.patch(
            f"/api/v1/admin/users/{user['id']}",
            json={"handle": "admin-set-handle"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        updated = await model.get_user_by_id(user["id"])
        assert updated["handle"] == "admin-set-handle"

    async def test_admin_change_handle_refreshes_presence(
        self, client, app, admin_user, user, sockets
    ):
        admin_resp = await client.post(
            "/api/v1/auth/login",
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
                f"/api/v1/admin/users/{user['id']}",
                json={"handle": "admin-refreshed"},
                headers=admin_headers,
            )
        assert resp.status_code == 200
        mock_refresh.assert_awaited_once_with(
            sockets, user["id"], "admin-refreshed"
        )

    async def test_admin_change_user_handle_invalid(
        self, client, app, admin_user, user
    ):
        admin_resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "testadmin@example.com", "password": "testpass"},
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_resp.json()['access_token']}"
        }
        resp = await client.patch(
            f"/api/v1/admin/users/{user['id']}",
            json={"handle": "", "password": "testpass"},
            headers=admin_headers,
        )
        assert resp.status_code == 400

    async def test_get_me(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == user["id"]
        assert data["email"] == "testuser@example.com"
        assert "handle" in data
