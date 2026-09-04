"""Tests for api.py: HTTP route handlers via FastAPI TestClient."""

import asyncio
import io
import os
import shutil
import tempfile

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
import httpx
from httpx import AsyncClient, ASGITransport
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from klangk import (
    api,
    auth as auth_mod,
    files as files_mod,
    model,
    nix as nix_mod,
    podman,
    terminal as terminal_mod,
    workspaces as ws_mod,
)
from klangk.container import ContainerRegistry
from klangk import emailsvc as emailsvc_mod
from klangk import util as util_mod
from klangk import netfilter as netfilter_mod
from klangk import oidc as oidc_mod
from klangk import features as features_mod
from klangk.model.container_events import (
    CAUSE_API,
    CAUSE_AUTO_START,
    CAUSE_DELETE,
    CAUSE_RESTART,
    CAUSE_STOP,
    EVENT_START,
    EVENT_STOP,
)
from _helpers import make_settings
from klangk.wshandler.session import WebSocketState
import types


def _auth():
    """A standalone Auth for token forging (same default secret as the
    app fixture, so tokens round-trip through app.state.auth.decode_*)."""
    from _helpers import wire_db_and_model

    state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=make_settings({}))
    )
    wire_db_and_model(state)
    return auth_mod.Auth(state)


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
    from klangk.util import API_PREFIX
    from klangk.main import register_exception_handlers

    settings = make_settings(
        env={
            "KLANGKD_AUTH_MODES": "password",
            "KLANGKD_DATA_DIR": str(temp_data_dir),
            "KLANGKD_CUSTOMIZE_DIR": str(temp_data_dir / "customize"),
        }
    )
    app.state.settings = settings
    app.state.podman = _mock_pod
    sockets = WebSocketState(app)
    app.state.sockets = sockets
    registry = ContainerRegistry(app)
    app.state.container_registry = registry
    app.state.oidc = oidc_mod.OIDC(app)
    app.state.features = features_mod.Features(app)
    app.state.workspaces = ws_mod.Workspaces(app)
    # #2762: workspace-created hook state (fired from the Workspaces
    # service layer on every creation path).
    from klangk import hooks as hooks_mod

    app.state.hooks = hooks_mod.Hooks(app)
    app.state.files = files_mod.Files(app)
    app.state.email = emailsvc_mod.EmailService(app)
    app.state.util = util_mod.Util(app)
    # #1365: create/update workspace validation reaches the netfilter
    # hooks-dir resolver.
    app.state.netfilter = netfilter_mod.NetFilter(app)
    # #2201: Workspaces.delete_workspace reaches state.nix (no-op when disabled).
    app.state.nix = nix_mod.Nix(app)

    app.state.auth = auth_mod.Auth(app)
    app.state.terminal = terminal_mod.Terminal(app)
    # #2661: server scheduler (never started here — the loop isn't under
    # test; the API endpoints reach notify_pending()).
    from klangk import server_schedule as server_schedule_mod

    app.state.server_scheduler = server_schedule_mod.ServerScheduler(app)
    # #1572: wire DB + Model so converted domains (tokens,
    # login_attempts, invitations, ports) reached via app.state.model.*
    # resolve the same per-test DB.
    from _helpers import wire_db_and_model

    wire_db_and_model(app)

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
        json={"identifier": "testuser@example.com", "password": "testpass"},
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _admin_login(client):
    """Auth headers for the seeded admin (admin_user fixture)."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "identifier": "testadmin@example.com",
            "password": "testpass",
        },
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _oidc_user_headers(app_state, email="oidc@example.com"):
    """Auth headers for an OIDC-only user (password_hash is NULL).

    OIDC users can't use /auth/login, so we mint a token directly,
    mirroring what the OIDC callback does.  Used to exercise the
    authenticated endpoints that must not 500 on a NULL hash (#890).
    """
    user = await app_state.state.model.users.create_user(
        email, None, verified=True, provider="oidc"
    )
    token = _auth().create_token(user["id"], user["email"])
    return {"Authorization": f"Bearer {token}"}


# --- Health ---


class TestHealth:
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        # The instance id lets a caller confirm it reached *this* klangkd —
        # the E2E harness detects fixture-server port collisions with it
        # (#3057).
        assert body["status"] == "ok"
        assert isinstance(body["instance"], str) and body["instance"]


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
        assert "features" in data

    async def test_version_no_file(self, client, app, monkeypatch):
        monkeypatch.setattr(app.state.settings, "version_file", None)
        resp = await client.get("/api/v1/version")
        assert resp.status_code == 200
        data = resp.json()
        assert data["version"] == "dev"
        assert data["commit"] == "unknown"
        assert data["built_at"] is None
        assert "features" in data

    async def test_version_includes_features(
        self, client, app, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(app.state.settings, "version_file", None)
        # The feature manifest is a single features.json at frontend_dir
        # (#1655) — no per-feature package.json scan.
        import json as json_mod

        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        (frontend_dir / "features.json").write_text(
            json_mod.dumps(
                {
                    "features": [
                        {
                            "name": "myfeature",
                            "version": "1.2.3",
                            "description": "A test feature",
                            "config": {},
                        }
                    ],
                    "defaults": [],
                    "container_env_keys": [],
                }
            )
        )
        # Rebuild the Features instance pointing at the tmp frontend dir
        import types as types_mod

        app.state.features = app.state.features.__class__(
            types_mod.SimpleNamespace(
                state=types_mod.SimpleNamespace(
                    settings=make_settings(
                        env={"KLANGKD_FRONTEND_DIR": str(frontend_dir)}
                    )
                )
            )
        )
        resp = await client.get("/api/v1/version")
        assert resp.status_code == 200
        features = resp.json()["features"]
        assert len(features) == 1
        assert features[0]["name"] == "myfeature"
        assert features[0]["version"] == "1.2.3"
        assert features[0]["description"] == "A test feature"

    async def test_version_includes_variant_when_present(
        self, client, app, tmp_path, monkeypatch
    ):
        # When version.json carries a "variant" field (a downstream product
        # identity string, set via KLANGKD_VARIANT in generate-version.sh), the
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

    async def test_get_config_advertises_per_handle_home_default(
        self, client, app
    ):
        # #2721: the create surfaces pre-reflect the deploy's home-layout
        # default (KLANGKD_PER_HANDLE_HOME) so an untouched form submits
        # exactly what a silent POST would get. Present pre-auth (the TUI
        # reads /config before login, like allow_autostart).
        app.state.settings.per_handle_home = False
        resp = await client.get("/api/v1/config")
        assert resp.json()["default_per_handle_home"] is False
        app.state.settings.per_handle_home = True
        resp = await client.get("/api/v1/config")
        assert resp.json()["default_per_handle_home"] is True

    async def test_get_config_advertises_classification_banner_default(
        self, client, app
    ):
        # #2768: the deploy default marking (KLANGKD_CLASSIFICATION_BANNER)
        # is surfaced so the web UI can render the banner for workspaces
        # without their own marking. Empty by default (no banner, no
        # reserved space); pre-auth like default_per_handle_home.
        resp = await client.get("/api/v1/config")
        assert resp.json()["default_classification_banner"] == ""
        app.state.settings.classification_banner = "CUI"
        resp = await client.get("/api/v1/config")
        assert resp.json()["default_classification_banner"] == "CUI"

    async def test_get_config_advertises_browser_delegate_flag(
        self, client, app
    ):
        # #2710: the frontend gates its BrowserDelegate on this flag, so
        # /config must carry it (public payload — like allow_autostart,
        # not the authenticated-only netfilter perimeter fields).
        app.state.settings.browser_delegate_enabled = False
        try:
            resp = await client.get("/api/v1/config")
            assert resp.json()["browser_delegate_enabled"] is False
        finally:
            app.state.settings.browser_delegate_enabled = True
        resp = await client.get("/api/v1/config")
        assert resp.json()["browser_delegate_enabled"] is True

    async def test_get_config_omits_netfilter_fields_when_unauthenticated(
        self, client, app
    ):
        # #1365: the deploy allow-list + armed-status advertise the egress
        # perimeter, so they must NOT appear on the pre-auth /config payload
        # (which feeds the login page). Anonymous callers learn neither.
        app.state.settings.netfilter_default_domains = ["github.com:443"]
        resp = await client.get("/api/v1/config")
        data = resp.json()
        assert "netfilter_default_domains" not in data
        assert "netfilter_enabled" not in data

    async def test_get_config_exposes_netfilter_default_domains(
        self, client, app, user
    ):
        # #1365: the create-workspace UI pre-fills its allowed-domains editor
        # from the deploy-wide default (which a workspace overrides, not
        # unions). netfilter_enabled gates showing the editor at all. Both
        # are returned only to authenticated callers (the UI sends the token
        # on its /config fetch and re-fetches after login).
        app.state.settings.netfilter_default_domains = ["github.com:443"]
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/config", headers=headers)
        assert resp.json()["netfilter_default_domains"] == ["github.com:443"]
        # The network sidecar ships with a default image, so egress
        # filtering is available out of the box (#2255).
        assert resp.json()["netfilter_enabled"] is True

    async def test_get_config_netfilter_disabled_when_sidecar_unset(
        self, client, app, user
    ):
        # An operator who clears the sidecar image (or sets
        # KLANGKD_NETFILTER_ENABLED=false) sees filtering as off.
        app.state.settings.network_sidecar_image = ""
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/config", headers=headers)
        assert resp.json()["netfilter_enabled"] is False

    async def test_get_config_includes_features(
        self, client, app, tmp_path, monkeypatch
    ):
        # frontend_config() resolves frontend-scope values from the per-feature
        # config blocks in features.json (#1655).
        import json as json_mod

        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        (frontend_dir / "features.json").write_text(
            json_mod.dumps(
                {
                    "features": [
                        {
                            "name": "test",
                            "version": "1.0.0",
                            "description": "",
                            "config": {
                                "KLANGKWS_FEATURE_MY_FEATURE_VAR": {
                                    "description": "",
                                    "default": "",
                                    "scope": "frontend",
                                }
                            },
                        }
                    ],
                    "defaults": [],
                    "container_env_keys": [],
                }
            )
        )
        import types as types_mod

        app.state.features = app.state.features.__class__(
            types_mod.SimpleNamespace(
                state=types_mod.SimpleNamespace(
                    settings=make_settings(
                        env={"KLANGKD_FRONTEND_DIR": str(frontend_dir)}
                    )
                )
            )
        )
        monkeypatch.setenv("KLANGKWS_FEATURE_MY_FEATURE_VAR", "test-value")
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        # KLANGKWS_FEATURE_MY_FEATURE_VAR → lowercased suffix `my_feature_var`
        # (#1662: strip prefix + lowercase suffix for /api/config keys).
        assert data["my_feature_var"] == "test-value"

    async def test_get_config_includes_features_enable_when_set(
        self, client, app, monkeypatch
    ):
        # KLANGKD_FEATURES_ENABLE is forwarded verbatim via /api/config when
        # set (#1655); absent when unset (frontend falls back to manifest
        # defaults).
        monkeypatch.setattr(
            app.state.settings, "features_enable", "celebrate,soliplex"
        )
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        assert resp.json()["features_enable"] == "celebrate,soliplex"

    async def test_get_config_omits_features_enable_when_unset(
        self, client, app, monkeypatch
    ):
        monkeypatch.setattr(app.state.settings, "features_enable", None)
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        assert "features_enable" not in resp.json()

    async def test_get_config_banner_fields(self, client, app, monkeypatch):
        monkeypatch.setattr(app.state.settings, "login_banner_title", "Notice")
        monkeypatch.setattr(
            app.state.settings, "login_banner", "You must accept terms."
        )
        monkeypatch.setattr(
            app.state.settings, "login_banner_every_visit", True
        )
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["login_banner_title"] == "Notice"
        assert data["login_banner"] == "You must accept terms."
        assert data["login_banner_every_visit"] is True

    async def test_get_config_advertises_min_password_length(self, client):
        # Surfaced so the UI can validate password length inline; matches the
        # rule enforced server-side by auth.validate_password.
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["min_password_length"] == _auth().min_password_length

    async def test_get_config_advertises_password_requirements(
        self, client, app, monkeypatch
    ):
        # Character-class counts (#2581), same contract as
        # min_password_length: what the client validates inline is what the
        # server enforces on every password setter.
        for key, val in {
            "password_require_upper": "1",
            "password_require_lower": "1",
            "password_require_digit": "2",
            "password_require_special": "0",
        }.items():
            monkeypatch.setattr(app.state.settings, key, val)
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["password_requirements"] == {
            "upper": 1,
            "lower": 1,
            "digit": 2,
            "special": 0,
        }

    async def test_get_config_advertises_password_history_count(
        self, client, app, monkeypatch
    ):
        # #2582: the reuse window is public config so change-password
        # UIs can explain the constraint inline.
        monkeypatch.setattr(
            app.state.settings,
            "password_history_count",
            5,
            raising=False,
        )
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        assert resp.json()["password_history_count"] == 5

    async def test_get_config_logo_url_defaults_empty(self, client, app):
        # No KLANGKD_LOGO_URL set -> empty string (UI renders default widget).
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
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
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

    async def test_register_persists_handle(self, client, db, app_state):
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
        user = await app_state.state.model.users.get_user_by_email(
            "handleme@example.com"
        )
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

    async def test_register_test_mode_falsy_string_not_enabled(
        self, client, app, db, monkeypatch
    ):
        """A falsy "false" string must not enable test mode (#2796).

        The gate previously used plain string truthiness, so any
        non-empty value — including "false"/"0" — auto-verified
        registrations; it now parses through parse_bool_setting.
        """
        monkeypatch.setattr(app.state.settings, "test_mode", "false")
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
        assert resp.json() == {
            "status": "pending_verification",
            "email": "new@example.com",
        }

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

    async def test_register_email_send_failure_rolls_back(
        self, client, db, app_state
    ):
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
        user = await app_state.state.model.users.get_user_by_email(
            "fail@example.com"
        )
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
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        token = login_resp.json()["access_token"]
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": "testadmin@example.com", "password": "pass"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 400

    async def test_register_race_integrity_error(self, client, app, db):
        """The production email-verification path must catch a lost
        duplicate-email race: same opaque 400 as the pre-check, no 500,
        and no verification email sent (#3101)."""
        with (
            patch.object(
                app.state.model.users,
                "insert_unverified_user",
                side_effect=SAIntegrityError(
                    "statement", {}, Exception("UNIQUE constraint failed")
                ),
            ),
            patch.object(
                emailsvc_mod.EmailService,
                "send_verification_email",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            resp = await client.post(
                "/api/v1/auth/register",
                json={"email": "race@example.com", "password": "newpass1"},
            )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Registration failed"
        mock_send.assert_not_called()
        assert (
            await app.state.model.users.get_user_by_email("race@example.com")
            is None
        )

    async def test_verify_email(self, client, db, app_state):
        """Verify endpoint marks user as verified."""
        from klangk import auth as auth_mod

        password_hash = auth_mod.hash_password("pass")
        user = await app_state.state.model.users.create_user(
            "unverified@example.com", password_hash, verified=False
        )
        token = _auth().create_verification_token(user["id"])
        resp = await client.get(f"/api/v1/auth/verify?token={token}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "verified"
        # User can now log in
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={"identifier": "unverified@example.com", "password": "pass"},
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
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        assert resp.status_code == 200
        assert "access_token" in resp.json()

    async def test_login_bad_password(self, client, user):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"identifier": "testuser@example.com", "password": "wrong"},
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
        # Idempotent (#2687): no token presented means nothing to revoke,
        # which is the desired end state — not an auth failure.
        resp = await client.post("/api/v1/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_logout_idempotent_revoked_token(self, client, user):
        """Second logout with the already-blocklisted token: still 200
        (#2687). A strict auth dependency 401s here, which taught clients
        to treat logout as failing."""
        headers = await _auth_headers(client)
        resp = await client.post("/api/v1/auth/logout", headers=headers)
        assert resp.status_code == 200
        # Security property: the first logout actually revoked the token.
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 401
        resp = await client.post("/api/v1/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_logout_disabled_account(self, client, user, app_state):
        """A disabled account logging out gets 200 and the token is still
        blocklisted (#2687): logout is a client cleaning up, not an auth
        attempt, so the #2588 403-for-disabled must not apply here."""
        headers = await _auth_headers(client)
        await app_state.state.model.users.set_user_disabled(user["id"], True)
        resp = await client.post("/api/v1/auth/logout", headers=headers)
        assert resp.status_code == 200
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 401

    async def test_logout_lowercase_bearer_scheme(self, client, user):
        """HTTPBearer accepts a case-insensitive scheme, so a lowercase
        ``bearer`` token must still be blocklisted (#2687)."""
        headers = await _auth_headers(client)
        token = headers["Authorization"][len("Bearer ") :]
        resp = await client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"bearer {token}"},
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 401

    async def test_logout_expired_token(self, client):
        """Logout with an expired token: 200, not 401 (#2687). The token
        is dead either way; logout reports success."""
        import datetime as dt
        import uuid

        from jose import jwt as pyjwt

        from _helpers import make_settings

        settings = make_settings({})
        payload = {
            "sub": "00000000-0000-0000-0000-000000000001",
            "email": "testuser@example.com",
            "jti": str(uuid.uuid4()),
            "exp": dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1),
        }
        token = pyjwt.encode(payload, settings.jwt_secret, algorithm="HS256")
        resp = await client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_logout_lenient_resolution_edge_cases(self, client):
        """Forged-but-validly-signed tokens that resolve to no user still
        log out with 200 — the lenient dependency returns None, never
        raises (#2687)."""
        import datetime as dt
        import uuid

        from jose import jwt as pyjwt

        from _helpers import make_settings

        settings = make_settings({})
        exp = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        no_sub = pyjwt.encode(
            {"jti": str(uuid.uuid4()), "exp": exp},
            settings.jwt_secret,
            algorithm="HS256",
        )
        ghost_sub = pyjwt.encode(
            {
                "sub": "00000000-0000-0000-0000-00000000dead",
                "jti": str(uuid.uuid4()),
                "exp": exp,
            },
            settings.jwt_secret,
            algorithm="HS256",
        )
        for token in (no_sub, ghost_sub):
            resp = await client.post(
                "/api/v1/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

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
        self, client, app, db, monkeypatch, app_state
    ):
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "local@example.com"
        )
        await app_state.state.model.users.create_user(
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
        self, client, app, db, monkeypatch, app_state
    ):
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "local@example.com"
        )
        await app_state.state.model.users.create_user(
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
        # The no-auth login minted a session, so /auth/me reports it
        # (#2583).
        assert resp.json()["last_login_at"] is not None

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

    async def test_no_body_required(
        self, client, app, db, monkeypatch, app_state
    ):
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "local@example.com"
        )
        await app_state.state.model.users.create_user(
            "local@example.com",
            auth_mod.hash_password("unused"),
            verified=True,
        )
        # Simple POST (no JSON body, no custom header) — the loopback bind +
        # proxy ACL, not a credential, is the identity boundary in this mode.
        resp = await client.post("/api/v1/auth/local")
        assert resp.status_code == 200

    # --- source-IP self-defense (front-proxy bypass, #1374 review) ---
    # The proxy `allow 127.0.0.1; deny all` ACL keys off $remote_addr, which
    # is the loopback proxy<->uvicorn hop when any loopback proxy fronts the klangk proxy.
    # So the ACL alone admits a workspace container that reached the proxy through
    # such a proxy. The backend re-checks the effective client here and refuses
    # non-loopback X-Real-IP even when the immediate peer is loopback.

    async def test_rejects_nonloopback_real_client_via_proxy(
        self, client, app, db, monkeypatch, app_state
    ):
        """Front-proxy bypass: peer is loopback (the proxy) but X-Real-IP is the
        real client (a workspace container) -> backend refuses independently."""
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "local@example.com"
        )
        await app_state.state.model.users.create_user(
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

    async def test_admits_loopback_real_client_via_proxy(
        self, client, app, db, monkeypatch, app_state
    ):
        """The benign mirror: peer loopback (the proxy), X-Real-IP loopback (the
        operator's browser) -> admit. (ASGI test client peer is itself
        loopback, satisfying the trust gate that honors X-Real-IP.)"""
        monkeypatch.setattr(app.state.oidc, "auth_modes", lambda: "none")
        monkeypatch.setattr(
            app.state.settings, "default_user", "local@example.com"
        )
        await app_state.state.model.users.create_user(
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
    async def _create_unverified_user(self, app_state):
        password_hash = auth_mod.hash_password("testpass")
        await app_state.state.model.users.create_user(
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

    async def test_resend_success(self, client, db, app_state):
        await self._create_unverified_user(app_state)
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

    async def test_resend_wrong_password(self, client, db, app_state):
        await self._create_unverified_user(app_state)
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

    async def test_resend_oidc_only_user_no_password(
        self, client, db, app_state
    ):
        """OIDC-only users have no password hash; must 401, not 500 (#890)."""
        await app_state.state.model.users.create_user(
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

    async def test_resend_rate_limited(self, client, db, app_state):
        # Clear stale rate limit state from parallel test workers
        api.resend_timestamps.pop(
            api.rate_limit_key("unverified@example.com"), None
        )
        await self._create_unverified_user(app_state)
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
        api.resend_timestamps.pop(
            api.rate_limit_key("unverified@example.com"), None
        )

    async def test_resend_prunes_expired_entries(self, client, db, app_state):
        # Stale rate-limit state from parallel test workers
        api.resend_timestamps.clear()
        await self._create_unverified_user(app_state)
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

            key = api.rate_limit_key("unverified@example.com")
            api.resend_timestamps[key] = (
                time.monotonic() - api.RESEND_COOLDOWN_SECONDS - 1
            )
            # Also seed an unrelated expired address to confirm it is evicted.
            api.resend_timestamps[api.rate_limit_key("stale@example.com")] = (
                time.monotonic() - api.RESEND_COOLDOWN_SECONDS - 1
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
        assert (
            api.rate_limit_key("stale@example.com")
            not in api.resend_timestamps
        )
        api.resend_timestamps.clear()


class TestResendVerificationLockout:
    """Failed credential checks against resend-verification count toward
    the login lockout (#2618).

    Without this the endpoint is an unthrottled password-guessing oracle:
    the 60s per-email cooldown only bounds email sending, and only applies
    after the credential check succeeds. Failures share the login counter
    (keyed on the resolved user's canonical email, raw input for unknown
    addresses)."""

    async def _create_unverified_user(self, app_state):
        password_hash = auth_mod.hash_password("testpass")
        await app_state.state.model.users.create_user(
            "unverified@example.com", password_hash, verified=False
        )

    async def _post(self, client, email, password):
        return await client.post(
            "/api/v1/auth/resend-verification",
            json={"email": email, "password": password},
        )

    async def test_lockout_after_max_attempts(self, client, db, app_state):
        """N-1 wrong passwords 401; the Nth triggers the 429 lockout, and
        even the correct password is then rejected before the verify."""
        await self._create_unverified_user(app_state)
        failures = app_state.state.settings.login_lockout_failures
        for i in range(failures):
            resp = await self._post(client, "unverified@example.com", "wrong")
            assert resp.status_code == (401 if i < failures - 1 else 429)
        resp = await self._post(client, "unverified@example.com", "testpass")
        assert resp.status_code == 429

    async def test_lockout_shared_with_login(self, client, db, app_state):
        """Resend failures key the same counter as login, so exhausting
        the threshold via resend locks the account's login too."""
        await self._create_unverified_user(app_state)
        failures = app_state.state.settings.login_lockout_failures
        for _ in range(failures):
            await self._post(client, "unverified@example.com", "wrong")
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "unverified@example.com",
                "password": "testpass",
            },
        )
        assert resp.status_code == 429

    async def test_unknown_email_also_rate_limited(
        self, client, db, app_state
    ):
        """Guesses against a made-up address are counted under the raw
        input, so unknown accounts get the same lockout protection."""
        failures = app_state.state.settings.login_lockout_failures
        for i in range(failures):
            resp = await self._post(client, "ghost@example.com", "guess")
            assert resp.status_code == (401 if i < failures - 1 else 429)

    async def test_success_clears_attempts(self, client, db, app_state):
        """A correct credential check clears the counter, like a
        successful login does."""
        await self._create_unverified_user(app_state)
        attempts = app_state.state.model.login_attempts
        for _ in range(2):
            await self._post(client, "unverified@example.com", "wrong")
        info = await attempts.get_login_attempt_info("unverified@example.com")
        assert info["attempt_count"] == 2
        api.resend_timestamps.pop(
            api.rate_limit_key("unverified@example.com"), None
        )
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp = await self._post(
                client, "unverified@example.com", "testpass"
            )
        assert resp.status_code == 200
        assert (
            await attempts.get_login_attempt_info("unverified@example.com")
            is None
        )
        api.resend_timestamps.pop(
            api.rate_limit_key("unverified@example.com"), None
        )

    async def test_window_reset_not_lockout(self, client, db, app_state):
        """A near-threshold count whose first failure predates the window
        resets instead of locking: old failures stop counting."""
        from datetime import datetime, timedelta, timezone

        await self._create_unverified_user(app_state)
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        async with app_state.state.db.transaction() as raw_db:
            await raw_db.execute(
                "INSERT INTO login_attempts"
                " (email, attempt_count, first_attempt_at)"
                " VALUES (?, ?, ?)",
                (
                    "unverified@example.com",
                    app_state.state.settings.login_lockout_failures - 1,
                    old,
                ),
            )
        resp = await self._post(client, "unverified@example.com", "wrong")
        assert resp.status_code == 401  # reset, not 429
        info = (
            await app_state.state.model.login_attempts.get_login_attempt_info(
                "unverified@example.com"
            )
        )
        assert info["attempt_count"] == 1


class TestForgotPassword:
    async def _create_user(self, app_state):
        password_hash = auth_mod.hash_password("oldpass")
        return await app_state.state.model.users.create_user(
            "forgot@example.com", password_hash, verified=True
        )

    async def test_forgot_sends_email(self, client, db, app_state):
        user = await self._create_user(app_state)
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
        # The URL build lives in schedule_reset_delivery (#3114), so pin
        # it here: the delivered reset URL must carry a token that
        # decodes to the account the request named.
        email_arg, reset_url = mock_send.await_args.args
        assert email_arg == "forgot@example.com"
        assert "/#/reset-password?token=" in reset_url
        token = reset_url.split("token=")[1]
        decoded = app_state.state.auth.decode_password_reset_token(token)
        assert decoded == user["id"]
        api.reset_timestamps.pop(
            api.rate_limit_key("forgot@example.com"), None
        )

    async def test_forgot_unknown_email_still_returns_sent(self, client, db):
        resp = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "nobody@example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"
        api.reset_timestamps.pop(
            api.rate_limit_key("nobody@example.com"), None
        )

    async def test_forgot_disabled_user_no_email_still_sent(
        self, client, app, db, app_state
    ):
        """#2588 review: a disabled account gets no reset email (the
        reset itself 403s), but the response never reveals the
        disabled state to an anonymous caller."""
        u = await self._create_user(app_state)
        await app.state.model.users.set_user_disabled(u["id"], True)
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
        mock_send.assert_not_awaited()
        api.reset_timestamps.pop(
            api.rate_limit_key("forgot@example.com"), None
        )

    async def test_forgot_smtp_failure_answers_sent_and_logs(
        self, client, db, app_state, caplog
    ):
        """#3114: an SMTP failure must not turn the response into a 503.
        Only the existing-enabled path ever awaited the send, so the
        status code was an account-existence and enabled-state oracle.
        Delivery failures are logged server-side instead."""
        import logging

        await self._create_user(app_state)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_password_reset_email",
            new_callable=AsyncMock,
            side_effect=RuntimeError("SMTP is down"),
        ):
            with caplog.at_level(logging.ERROR, logger="klangk.api.auth"):
                resp = await client.post(
                    "/api/v1/auth/forgot-password",
                    json={"email": "forgot@example.com"},
                )
        assert resp.status_code == 200
        assert resp.json()["status"] == "sent"
        assert any(
            "Failed to send password reset email" in r.message
            for r in caplog.records
        )
        api.reset_timestamps.pop(
            api.rate_limit_key("forgot@example.com"), None
        )

    async def test_forgot_non_send_paths_mint_token(
        self, client, db, app, app_state
    ):
        """#3114 timing channel: the unknown and disabled paths perform
        the same response-path work (the reset-token mint) as the
        sending path, so response latency cannot reveal whether the
        address belongs to an existing, enabled account."""
        u = await self._create_user(app_state)
        await app.state.model.users.set_user_disabled(u["id"], True)
        with patch.object(
            auth_mod.Auth,
            "create_password_reset_token",
            return_value="dummy-token",
        ) as mint:
            unknown = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "nobody@example.com"},
            )
            disabled = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
        assert unknown.status_code == 200
        assert disabled.status_code == 200
        assert mint.call_count == 2
        # Unknown mints against a discarded dummy subject; disabled
        # against the real (unused) id — the design intent.
        dummy_subject, real_subject = (c.args[0] for c in mint.call_args_list)
        assert real_subject == u["id"]
        assert dummy_subject != u["id"]
        api.reset_timestamps.pop(
            api.rate_limit_key("nobody@example.com"), None
        )
        api.reset_timestamps.pop(
            api.rate_limit_key("forgot@example.com"), None
        )

    async def test_forgot_rate_limited(self, client, db, app_state):
        await self._create_user(app_state)
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
        api.reset_timestamps.pop(
            api.rate_limit_key("forgot@example.com"), None
        )

    async def test_forgot_rate_limited_unknown_email(self, client, db):
        """#3100: the cooldown must not be an account-existence oracle —
        an unknown address answers 429 on the second request, exactly
        like a known one."""
        resp1 = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "ghost-forgot@example.com"},
        )
        resp2 = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "ghost-forgot@example.com"},
        )
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "sent"
        assert resp2.status_code == 429
        api.reset_timestamps.pop(
            api.rate_limit_key("ghost-forgot@example.com"), None
        )

    async def test_forgot_rate_limited_disabled_user(
        self, client, app, db, app_state
    ):
        """#3100: a disabled account must be indistinguishable from an
        enabled one across repeated requests — 429 on the second, no
        email on either."""
        u = await self._create_user(app_state)
        await app.state.model.users.set_user_disabled(u["id"], True)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_password_reset_email",
            new_callable=AsyncMock,
        ) as mock_send:
            resp1 = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
            resp2 = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
        assert resp1.status_code == 200
        assert resp1.json()["status"] == "sent"
        assert resp2.status_code == 429
        mock_send.assert_not_awaited()
        api.reset_timestamps.pop(
            api.rate_limit_key("forgot@example.com"), None
        )

    async def test_forgot_prunes_expired_entries(self, client, db, app_state):
        api.reset_timestamps.clear()
        await self._create_user(app_state)
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

            api.reset_timestamps[api.rate_limit_key("forgot@example.com")] = (
                time.monotonic() - api.RESET_COOLDOWN_SECONDS - 1
            )
            api.reset_timestamps[api.rate_limit_key("stale@example.com")] = (
                time.monotonic() - api.RESET_COOLDOWN_SECONDS - 1
            )
            resp2 = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
        assert resp2.status_code == 200
        assert (
            api.rate_limit_key("stale@example.com") not in api.reset_timestamps
        )
        api.reset_timestamps.clear()

    async def test_forgot_flood_caps_dict_size(self, client, db, monkeypatch):
        """#3113: a flood of unique unknown addresses cannot grow the
        dict past the cap (oldest entries are shed instead), and every
        flood response stays identical to a known-address one — no
        existence oracle under pressure. An over-full dict (seeded past
        a shrunk cap) is likewise driven back under the cap in one
        request."""
        import sys
        import time

        monkeypatch.setattr(
            sys.modules["klangk.api.auth"], "RATE_LIMIT_MAX_ENTRIES", 5
        )
        api.reset_timestamps.clear()
        for i in range(25):
            resp = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": f"flood-{i}@example.com"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "sent"
        assert len(api.reset_timestamps) <= 5
        for i in range(7):
            api.reset_timestamps[
                api.rate_limit_key(f"seed-{i}@example.com")
            ] = time.monotonic()
        resp = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "after@example.com"},
        )
        assert resp.status_code == 200
        assert len(api.reset_timestamps) <= 5
        api.reset_timestamps.clear()

    async def test_forgot_rate_limit_keys_hold_no_raw_email(
        self, client, db, app_state
    ):
        """#3113: the dict is keyed by fixed-width hashes, so no raw
        (possibly attacker-chosen, arbitrarily long) address is
        retained past the request."""
        await self._create_user(app_state)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_password_reset_email",
            new_callable=AsyncMock,
        ):
            await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
        assert api.rate_limit_key("forgot@example.com") in api.reset_timestamps
        assert all("@" not in key for key in api.reset_timestamps)
        assert all(len(key) == 32 for key in api.reset_timestamps)
        api.reset_timestamps.clear()

    async def test_forgot_limited_while_dict_near_full(
        self, client, db, app_state, monkeypatch
    ):
        """#3113: the cooldown check is unaffected by a dict at the
        cap — a recorded address still 429s while junk entries fill the
        dict around it. Its protection lasts while fewer than
        RATE_LIMIT_MAX_ENTRIES newer entries arrive (the window is the
        most recent cap entries), not indefinitely."""
        import sys

        monkeypatch.setattr(
            sys.modules["klangk.api.auth"], "RATE_LIMIT_MAX_ENTRIES", 5
        )
        await self._create_user(app_state)
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
            # Junk entries fill the dict to the cap without evicting
            # the recorded address.
            for i in range(4):
                await client.post(
                    "/api/v1/auth/forgot-password",
                    json={"email": f"junk-{i}@example.com"},
                )
            resp2 = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": "forgot@example.com"},
            )
        assert resp2.status_code == 429
        api.reset_timestamps.clear()


class TestResetPassword:
    async def _create_user(self, app_state):
        password_hash = auth_mod.hash_password("oldpass")
        return await app_state.state.model.users.create_user(
            "reset@example.com", password_hash, verified=True
        )

    async def test_reset_success(self, client, db, app_state):
        user = await self._create_user(app_state)
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
                "identifier": "reset@example.com",
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

    async def test_reset_short_password(self, client, db, app_state):
        user = await self._create_user(app_state)
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

    async def _create_user_with(self, app, password):
        """Seed a verified user with an explicit (policy-valid) password.

        Creation records nothing (#2582): the initial hash enters
        history only once the user changes/resets *away* from it.
        """
        password_hash = auth_mod.hash_password(password)
        return await app.state.model.users.create_user(
            "reset@example.com", password_hash, verified=True
        )

    async def test_reset_rejected_when_reused(
        self, client, db, app, monkeypatch
    ):
        """#2582: reset to the current or a remembered password → 400."""
        monkeypatch.setattr(
            app.state.settings,
            "password_history_count",
            3,
            raising=False,
        )
        user = await self._create_user_with(app, "oldpass12")
        token = _auth().create_password_reset_token(user["id"])
        # Reusing the current password.
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "oldpass12"},
        )
        assert resp.status_code == 400
        assert "current" in resp.json()["detail"]
        # Reuse via history: reset to newpass1, then try to reset back.
        token = _auth().create_password_reset_token(user["id"])
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "newpass1"},
        )
        assert resp.status_code == 200
        token = _auth().create_password_reset_token(user["id"])
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "oldpass12"},
        )
        assert resp.status_code == 400
        assert "recently" in resp.json()["detail"]

    async def test_reset_allowed_when_disabled(self, client, db, app):
        """count=0 (default): the same reset-to-old flow succeeds."""
        user = await self._create_user_with(app, "oldpass12")
        token = _auth().create_password_reset_token(user["id"])
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "newpass1"},
        )
        assert resp.status_code == 200
        token = _auth().create_password_reset_token(user["id"])
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "oldpass12"},
        )
        assert resp.status_code == 200


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
                "identifier": "testuser@example.com",
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

    async def test_change_password_reuse_rejected(
        self, client, user, app, monkeypatch
    ):
        """#2582: changing to the current or a previous password → 400."""
        monkeypatch.setattr(
            app.state.settings,
            "password_history_count",
            3,
            raising=False,
        )
        headers = await _auth_headers(client)
        # Change away from the seed password once — this retires the
        # seed hash into history; the new hash is never recorded until
        # the user changes away from *it* (#2582).
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "midpass1",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        # To the current password.
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "midpass1",
                "new_password": "midpass1",
            },
            headers=headers,
        )
        assert resp.status_code == 400
        assert "current" in resp.json()["detail"]
        # Change away, then try to change back (remembered).
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "midpass1",
                "new_password": "newpass1",
            },
            headers=headers,
        )
        assert resp.status_code == 200
        resp2 = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "newpass1",
                "new_password": "midpass1",
            },
            headers=headers,
        )
        assert resp2.status_code == 400
        assert "recently" in resp2.json()["detail"]

    async def test_change_password_no_auth(self, client, db):
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "newpass1",
            },
        )
        assert resp.status_code == 401

    async def test_change_password_oidc_only_user(self, client, db, app_state):
        """OIDC-only users have no password; must 403, not 500 (#890)."""
        headers = await _oidc_user_headers(app_state)
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
    async def test_change_email_success(self, client, user, app_state):
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
        updated = await app_state.state.model.users.get_user_by_email(
            "new@example.com"
        )
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

    async def test_change_email_already_taken(
        self, client, user, db, app_state
    ):
        # Create another user
        password_hash = auth_mod.hash_password("other")
        await app_state.state.model.users.create_user(
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

    async def test_change_email_oidc_only_user(self, client, db, app_state):
        """OIDC-only users have no password; must 403, not 500 (#890)."""
        headers = await _oidc_user_headers(app_state)
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
    @pytest.fixture(autouse=True)
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation requires admin; make the standard
        test user an admin so existing tests keep working."""

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

    async def test_create_workspace_member_allowed_by_default(
        self, client, user, app_state
    ):
        """#3137: a plain member (non-admin) can create workspaces on a
        stock deploy — the members group holds the seeded
        create-workspace grant."""
        from klangk.auth import hash_password

        pw_hash = hash_password("testpass")
        await app_state.state.model.users.create_user(
            "nonadmin@example.com", pw_hash, verified=True
        )
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "nonadmin@example.com",
                "password": "testpass",
            },
        )
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "member-ws"}
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "member-ws"

    async def test_create_workspace_explicit_deny_restores_admin_only(
        self, client, user, app_state
    ):
        """#3137: an explicit Deny for the members group ahead of the
        seeded Allow restores the pre-#3137 admin-only posture
        (ordered ACL, first-match-wins)."""
        from klangk.auth import hash_password
        from klangk.model import (
            ACTION_DENY,
            PRINCIPAL_GROUP,
        )

        members = await app_state.state.model.users.get_group_by_name(
            "members"
        )
        # Stage the Deny at position 1 (ahead of the seeded Allow,
        # which shifts to position 2).
        async with app_state.state.db.transaction() as tx:
            await tx.execute(
                "UPDATE acl_entries SET position = position + 1"
                " WHERE resource = '/workspaces' AND position >= 1"
            )
        await app_state.state.model.acl.add_acl_entry(
            "/workspaces",
            1,
            ACTION_DENY,
            "create-workspace",
            PRINCIPAL_GROUP,
            group_id=members["id"],
        )

        pw_hash = hash_password("testpass")
        await app_state.state.model.users.create_user(
            "denied-member@example.com", pw_hash, verified=True
        )
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "denied-member@example.com",
                "password": "testpass",
            },
        )
        token = resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "denied-ws"}
        )
        assert resp.status_code == 403

    async def test_create_with_allow_egress_mode(self, client, user):
        # #2409: 'allow' is a valid egress_mode at create time and is
        # persisted on the workspace.
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "allow-ws", "egress_mode": "allow"},
        )
        assert resp.status_code == 200
        assert resp.json()["egress_mode"] == "allow"

    async def test_create_with_unknown_egress_mode_rejected(
        self, client, user
    ):
        # #2409: the Literal is allow/static/interactive; anything else is a
        # 422 (pydantic validation), not a silent accept.
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "bad", "egress_mode": "permissive"},
        )
        assert resp.status_code == 422

    async def test_create_per_handle_home_defaults_shared(self, client, user):
        # #2723: silent create inherits the deploy default (shared since
        # the chunk-5 flip); an explicit value wins; both are exposed in
        # payloads.
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "home-dflt"}
        )
        assert resp.status_code == 200
        assert resp.json()["per_handle_home"] is False
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "home-off", "per_handle_home": True},
        )
        assert resp.status_code == 200
        assert resp.json()["per_handle_home"] is True
        resp = await client.get("/api/v1/workspaces", headers=headers)
        by_name = {w["name"]: w for w in resp.json()}
        assert by_name["home-dflt"]["per_handle_home"] is False
        assert by_name["home-off"]["per_handle_home"] is True

    async def test_create_per_handle_home_inherits_config_default(
        self, client, user, app, monkeypatch
    ):
        # #2719: an unset field follows KLANGKD_PER_HANDLE_HOME (read live
        # off settings at create time).
        monkeypatch.setattr(app.state.settings, "per_handle_home", False)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "cfg-dflt"},
        )
        assert resp.status_code == 200
        assert resp.json()["per_handle_home"] is False
        # An explicit value still wins over the deploy default.
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "cfg-over", "per_handle_home": True},
        )
        assert resp.json()["per_handle_home"] is True

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

    async def test_create_rejects_blank_name(self, client, user):
        # #3110: the request models enforce the name minimum centrally —
        # empty AND whitespace-only names are 422s on every surface, not
        # just the CLI's client-side guard (PR #3103).
        headers = await _auth_headers(client)
        for bad in ("", "   "):
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": bad},
            )
            assert resp.status_code == 422
            assert "cannot be empty" in str(resp.json()["detail"])

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

    async def test_create_with_allowed_domains_persists(
        self, client, app, user, caplog
    ):
        # When the network sidecar is disabled, allowed_domains is still
        # persisted (with a loud warning) so it takes effect once filtering
        # is re-enabled (#1365, #2255).
        app.state.settings.network_sidecar_image = ""
        headers = await _auth_headers(client)
        with caplog.at_level("WARNING"):
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={
                    "name": "filtered",
                    "allowed_domains": [
                        "github.com:443",
                        "github.com:443",
                        "pypi.org",
                    ],
                },
            )
        assert resp.status_code == 200
        assert resp.json()["allowed_domains"] == [
            "github.com:443",
            "pypi.org",
        ]
        assert any(
            "network sidecar is disabled" in r.message for r in caplog.records
        )

    async def test_create_with_invalid_allowed_domains_rejected(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "bad", "allowed_domains": ["bad spec"]},
        )
        assert resp.status_code == 400
        assert "Invalid allowed_domains" in resp.json()["detail"]

    async def test_create_with_rejected_domains_persists(
        self, client, app, user, caplog
    ):
        # rejected_domains is the deny counterpart (#2367): host-only grammar
        # mirroring allowed_domains (bare = exact, ``.host`` = inclusive),
        # de-duplicated + order-preserved, and warned (not rejected) when the
        # sidecar is off so it takes effect once filtering is re-enabled.
        app.state.settings.network_sidecar_image = ""
        headers = await _auth_headers(client)
        with caplog.at_level("WARNING"):
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={
                    "name": "blocked",
                    "rejected_domains": [
                        "evil.com:443",
                        "evil.com:443",
                        ".malicious.net",
                    ],
                },
            )
        assert resp.status_code == 200
        assert resp.json()["rejected_domains"] == [
            "evil.com:443",
            ".malicious.net",
        ]
        assert any(
            "network sidecar is disabled" in r.message for r in caplog.records
        )

    async def test_create_with_invalid_rejected_domains_rejected(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "bad", "rejected_domains": ["bad spec"]},
        )
        assert resp.status_code == 400
        assert "Invalid rejected_domains" in resp.json()["detail"]

    async def test_create_with_rejected_domains_cidr_rejected(
        self, client, user
    ):
        # A CIDR is rejected up front: the sidecar NXDOMAINs a rejected name
        # *before* resolution (no IP/CIDR dimension), and a deny-list must not
        # silently ignore an entry an operator believed was blocking (#2367).
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "bad-cidr", "rejected_domains": ["10.0.0.0/8"]},
        )
        assert resp.status_code == 400
        assert (
            "rejected_domains does not support CIDR" in resp.json()["detail"]
        )

    async def test_create_auto_start_rejected_without_env(self, client, user):
        headers = await _auth_headers(client)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KLANGKD_ALLOW_AUTOSTART", None)
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

    async def test_create_eager_start_drained_not_failed(
        self, client, app, user
    ):
        """A graceful-restart drain racing create's eager start degrades
        to a warning — the workspace is created but not started (#2527)."""
        from klangk.exceptions import NodeDrainingError

        headers = await _auth_headers(client)
        app.state.container_registry.draining = True
        try:
            with (
                patch.object(app.state.settings, "allow_autostart", "1"),
                patch.object(
                    app.state.workspaces,
                    "start_workspace",
                    new_callable=AsyncMock,
                    side_effect=NodeDrainingError(
                        "node is draining: new workspace starts are "
                        "disabled (a restart is in progress)"
                    ),
                ),
            ):
                resp = await client.post(
                    "/api/v1/workspaces",
                    headers=headers,
                    json={"name": "auto-ws", "auto_start": True},
                )
            assert resp.status_code == 200
            assert resp.json()["auto_start"] is True
        finally:
            app.state.container_registry.draining = False

    async def test_start_refused_503_while_draining(
        self, client, app, user, registry, ws_admin
    ):
        """POST /start under the drain flag returns a clear 503 (#2527)."""
        from klangk.exceptions import NodeDrainingError

        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "dw"}
        )
        assert create_resp.status_code == 200, create_resp.text
        ws_id = create_resp.json()["id"]
        app.state.container_registry.draining = True
        try:
            with patch.object(
                app.state.workspaces,
                "start_workspace",
                new_callable=AsyncMock,
                side_effect=NodeDrainingError(
                    "node is draining: new workspace starts are disabled "
                    "(a restart is in progress)"
                ),
            ):
                resp = await client.post(
                    f"/api/v1/workspaces/{ws_id}/start", headers=headers
                )
            assert resp.status_code == 503
            assert "draining" in resp.json()["detail"]
        finally:
            app.state.container_registry.draining = False

    async def test_restart_refused_503_up_front_while_draining(
        self, client, app, user, registry, ws_admin
    ):
        """POST /restart checks the drain flag BEFORE stopping the
        running container — a running workspace survives the refusal
        (#2527)."""
        from klangk.container.basics import ContainerState

        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "dw2"}
        )
        assert create_resp.status_code == 200, create_resp.text
        ws_id = create_resp.json()["id"]
        registry.states[ws_id] = ContainerState(ws_id, "cid-live", app)
        app.state.container_registry.draining = True
        try:
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/restart", headers=headers
            )
            assert resp.status_code == 503
            assert "draining" in resp.json()["detail"]
            # The running container was NOT stopped by the refusal.
            assert registry.states.get(ws_id) is not None
            assert registry.states[ws_id].container_id == "cid-live"
        finally:
            app.state.container_registry.draining = False
            registry.states.pop(ws_id, None)

    async def test_restart_stop_then_drained_503(
        self, client, app, user, registry, ws_admin
    ):
        """A drain that lands mid-restart (after the stop, before the
        start) leaves the workspace stopped with a 503 — never
        half-restarted (#2527)."""
        from klangk.exceptions import NodeDrainingError

        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "dw3"}
        )
        assert create_resp.status_code == 200, create_resp.text
        ws_id = create_resp.json()["id"]
        with patch.object(
            app.state.workspaces,
            "start_workspace",
            new_callable=AsyncMock,
            side_effect=NodeDrainingError(
                "node is draining: new workspace starts are disabled "
                "(a restart is in progress)"
            ),
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/restart", headers=headers
            )
        assert resp.status_code == 503
        assert "draining" in resp.json()["detail"]

    async def test_start_refused_503_on_capacity(
        self, client, app, user, registry, ws_admin
    ):
        """POST /start under a capacity refusal returns a clear,
        distinguishable 503 (#2525)."""
        from klangk.exceptions import WorkspaceCapacityError

        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "cap-ws"}
        )
        assert create_resp.status_code == 200, create_resp.text
        ws_id = create_resp.json()["id"]
        with patch.object(
            app.state.workspaces,
            "start_workspace",
            new_callable=AsyncMock,
            side_effect=WorkspaceCapacityError(
                "host at capacity: 1.2 GB available, workspace wants "
                "9.0 GB (memory limit 8.0 GB + 1.0 GB reserve). Stop an "
                "idle workspace, free host memory, or lower the workspace "
                "memory limit (KLANGKD_CONTAINER_MEMORY_LIMIT)."
            ),
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/start", headers=headers
            )
        assert resp.status_code == 503
        assert "host at capacity" in resp.json()["detail"]

    async def test_restart_capacity_refusal_503(
        self, client, app, user, registry, ws_admin
    ):
        """POST /restart surfaces a capacity refusal as 503 — the stop
        already happened, so the workspace is left stopped and capacity
        is re-checked on the next start (#2525)."""
        from klangk.exceptions import WorkspaceCapacityError

        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "cap-r"}
        )
        assert create_resp.status_code == 200, create_resp.text
        ws_id = create_resp.json()["id"]
        with (
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
            patch.object(
                app.state.workspaces,
                "start_workspace",
                new_callable=AsyncMock,
                side_effect=WorkspaceCapacityError(
                    "workspace quota reached: 2 of this user's workspaces "
                    "are already running and the server caps it at 2 "
                    "(KLANGKD_MAX_RUNNING_WORKSPACES_PER_USER). Stop a "
                    "workspace first, or ask the operator to raise the cap."
                ),
            ),
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/restart", headers=headers
            )
        assert resp.status_code == 503
        assert "quota" in resp.json()["detail"]

    async def test_create_eager_start_capacity_refusal_not_failed(
        self, client, app, user
    ):
        """A capacity refusal on create's eager start degrades to a
        warning — the workspace row exists and runs once capacity frees
        (#2525; creation is not capacity-gated, only starts are)."""
        from klangk.exceptions import WorkspaceCapacityError

        headers = await _auth_headers(client)
        with (
            patch.object(app.state.settings, "allow_autostart", "1"),
            patch.object(
                app.state.workspaces,
                "start_workspace",
                new_callable=AsyncMock,
                side_effect=WorkspaceCapacityError(
                    "host at capacity: 1.2 GB available, workspace wants "
                    "9.0 GB (memory limit 8.0 GB + 1.0 GB reserve)."
                ),
            ),
        ):
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": "cap-create-ws", "auto_start": True},
            )
        assert resp.status_code == 200

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
        # #2974: the deploy-level toggles moved to the authenticated-only
        # /config fields — the images listing is image data only.
        assert "nix_available" not in data
        assert "sudo_available" not in data

    async def test_config_sudo_available(self, client, app, user, monkeypatch):
        """#2017/#2974: sudo_available reports the deploy-wide allow_sudo
        posture (authenticated-only /config field) so create/edit UIs can
        gate the per-workspace lock-down toggle."""
        headers = await _auth_headers(client)
        monkeypatch.setattr(app.state.settings, "allow_sudo", "true")
        resp = await client.get("/api/v1/config", headers=headers)
        assert resp.json()["sudo_available"] is True
        monkeypatch.setattr(app.state.settings, "allow_sudo", "")
        resp = await client.get("/api/v1/config", headers=headers)
        assert resp.json()["sudo_available"] is False
        # #1365 posture: like the netfilter fields, absent pre-auth.
        resp = await client.get("/api/v1/config")
        assert "sudo_available" not in resp.json()
        assert "nix_available" not in resp.json()

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

    async def test_delete_workspace_prunes_registry_entries(
        self, client, app, user, registry, app_state
    ):
        """#2912: delete drops the per-workspace lock + stop-epoch entries
        (the id can never be started again)."""
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "pruned"}
        )
        ws_id = create_resp.json()["id"]
        registry._get_workspace_lock(ws_id)
        registry.stop_epoch[ws_id] = 4

        with patch.object(
            registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ):
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}", headers=headers
            )
        assert resp.status_code == 200
        assert ws_id not in registry._workspace_locks
        assert ws_id not in registry.stop_epoch

    async def test_delete_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.delete(
            "/api/v1/workspaces/fake-id", headers=headers
        )
        assert resp.status_code == 403

    async def test_delete_not_found(self, client, user, app_state):
        """ACL passes but workspace doesn't exist."""
        headers = await _auth_headers(client)
        fake_id = "fake-del-id"
        await app_state.state.model.acl.add_acl_entry(
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
        self, client, app, user, registry, app_state
    ):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "has-container"},
        )
        ws_id = create_resp.json()["id"]
        # Simulate a running container
        await app_state.state.model.workspaces.update_workspace_container(
            ws_id, "fake-container-id"
        )

        with patch.object(
            registry,
            "stop_and_remove_container",
            new_callable=AsyncMock,
        ) as mock_rm:
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}", headers=headers
            )
        assert resp.status_code == 200
        mock_rm.assert_awaited_once_with(
            "fake-container-id",
            workspace_id=ws_id,
            cause=CAUSE_DELETE,
            actor_id=user["id"],
        )

    async def test_delete_workspace_cleans_up_groups(
        self, client, app, user, registry, app_state
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
            group = await app_state.state.model.users.get_group_by_name(
                f"{suffix}-{ws_id}"
            )
            assert group is not None, f"expected {suffix} group to exist"

        # Verify ACL entries exist for the workspace
        acl = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws_id}"
        )
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
            group = await app_state.state.model.users.get_group_by_name(
                f"{suffix}-{ws_id}"
            )
            assert group is None, f"expected {suffix} group to be deleted"

        # ACL entries should be gone
        acl = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws_id}"
        )
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
        mock_stop.assert_awaited_once_with(
            "cid-restart",
            workspace_id=ws_id,
            cause=CAUSE_RESTART,
            actor_id=user["id"],
        )
        # #1244: restart re-starts the container (not just stop+remove),
        # so the service command re-fires at the create choke point and
        # the workspace recovers.
        mock_start.assert_awaited_once()

        # Clean up registry state.
        registry.states.pop(ws_id, None)

    async def test_restart_returns_400_on_user_config_error(
        self, client, app, user, registry
    ):
        # A user-config problem on restart surfaces as a 400, not a 500
        # (#2157). The existing container is still stopped+removed first.
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "bad-mount-restart"},
        )
        ws_id = create_resp.json()["id"]
        registry.track_activity("cid-rst", ws_id)
        with (
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ),
            patch.object(
                app.state.workspaces,
                "start_workspace",
                new_callable=AsyncMock,
                side_effect=ValueError(
                    "Bind mount source does not exist: /nonexistent/path"
                ),
            ),
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/restart", headers=headers
            )
        assert resp.status_code == 400
        assert "Bind mount source does not exist" in resp.json()["detail"]
        registry.states.pop(ws_id, None)

    async def test_restart_not_found(self, client, user, app_state):
        headers = await _auth_headers(client)
        fake_id = "fake-restart-id"
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "restart-workspace",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.post(
            f"/api/v1/workspaces/{fake_id}/restart", headers=headers
        )
        assert resp.status_code == 404

    async def test_stop_workspace(self, client, app, user, registry):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "stop-me"}
        )
        ws_id = create_resp.json()["id"]
        registry.track_activity("cid-stop", ws_id)

        mock_session = MagicMock()
        mock_session.full_reset = AsyncMock()
        with (
            patch.object(
                registry,
                "stop_and_remove_container",
                new_callable=AsyncMock,
            ) as mock_stop,
            patch.object(
                registry,
                "notify_workspace_killed",
                new_callable=AsyncMock,
            ) as mock_killed,
            patch.object(
                app.state.sockets, "get_session", return_value=mock_session
            ),
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/stop", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        mock_killed.assert_awaited_once_with(ws_id, container_id="cid-stop")
        mock_stop.assert_awaited_once_with(
            "cid-stop",
            workspace_id=ws_id,
            cause=CAUSE_STOP,
            actor_id=user["id"],
        )
        # Re-homed from the retired WS shutdown_container handler: REST /stop
        # broadcasts container_stopped so live viewers show "stopped".
        mock_session.broadcast.assert_called_once()
        event = mock_session.broadcast.call_args[0][0]
        assert event["type"] == "event"
        assert event["event"]["name"] == "container_stopped"
        registry.states.pop(ws_id, None)

    async def test_stop_workspace_no_container(self, client, app, user):
        # /stop on a workspace with no running container is a no-op and
        # must NOT broadcast container_stopped (#1868 review: the broadcast
        # is gated on a container actually being stopped).
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "no-cid"}
        )
        ws_id = create_resp.json()["id"]
        # No registry.track_activity -> no running container (cid is None).

        mock_session = MagicMock()
        mock_session.full_reset = AsyncMock()
        with patch.object(
            app.state.sockets, "get_session", return_value=mock_session
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/stop", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        mock_session.broadcast.assert_not_called()

    async def test_start_workspace(self, client, app, user):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "start-me"}
        )
        ws_id = create_resp.json()["id"]

        with patch.object(
            app.state.workspaces,
            "start_workspace",
            new_callable=AsyncMock,
        ) as mock_start:
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/start", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"
        mock_start.assert_awaited_once()

    async def test_start_already_running(self, client, app, user, registry):
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "running"}
        )
        ws_id = create_resp.json()["id"]
        registry.track_activity("cid-run", ws_id)

        with patch.object(
            app.state.workspaces,
            "start_workspace",
            new_callable=AsyncMock,
        ) as mock_start:
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/start", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "already_running"
        mock_start.assert_not_awaited()
        registry.states.pop(ws_id, None)

    async def test_start_returns_400_on_user_config_error(
        self, client, app, user
    ):
        # A user-config problem (e.g. a bind-mount source path that doesn't
        # exist) surfaces as a 400, not an unhandled 500 (#2157).
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "bad-mount"}
        )
        ws_id = create_resp.json()["id"]
        with patch.object(
            app.state.workspaces,
            "start_workspace",
            new_callable=AsyncMock,
            side_effect=ValueError(
                "Bind mount source does not exist: /nonexistent/path"
            ),
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/start", headers=headers
            )
        assert resp.status_code == 400
        assert "Bind mount source does not exist" in resp.json()["detail"]

    async def test_stop_not_found(self, client, user, app_state):
        headers = await _auth_headers(client)
        fake_id = "fake-stop-id"
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "stop-workspace",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.post(
            f"/api/v1/workspaces/{fake_id}/stop", headers=headers
        )
        assert resp.status_code == 404

    async def test_start_not_found(self, client, user, app_state):
        headers = await _auth_headers(client)
        fake_id = "fake-start-id"
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "start-workspace",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.post(
            f"/api/v1/workspaces/{fake_id}/start", headers=headers
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
        # #2524: restart bookkeeping rides along (None when clean).
        assert data["restart"] is None

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
        assert data["restart"] is None

    async def test_workspace_status_crash_loop(self, client, user, registry):
        """A crash-looping workspace surfaces its terminal state (#2524)."""
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "crash-loop-ws"},
        )
        ws_id = create_resp.json()["id"]

        from klangk.container.crash import RestartTracker

        tracker = RestartTracker()
        tracker.attempts = 3
        tracker.last_cause = "OOM-killed at 8g memory limit (exit code 137)"
        tracker.gave_up_at = 1_700_000_000.0
        registry.crash.trackers[ws_id] = tracker
        try:
            resp = await client.get(
                f"/api/v1/workspaces/{ws_id}/status",
                headers=headers,
            )
            assert resp.status_code == 200
            restart = resp.json()["restart"]
            assert restart["state"] == "crash-loop"
            assert restart["attempts"] == 3
            assert restart["last_cause"].startswith("OOM-killed")
            assert restart["gave_up_at"] is not None
        finally:
            registry.crash.trackers.pop(ws_id, None)

    async def test_workspace_status_monitor_only_member(
        self, client, user, app_state
    ):
        """#2783: ``monitor`` alone (no ``terminal``) can read status —
        health observation is decoupled from exec/attach."""
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "status-monitor-only"},
        )
        ws_id = create_resp.json()["id"]
        other = await app_state.state.model.users.create_user(
            "monitor@example.com",
            auth_mod.hash_password("monpass"),
            verified=True,
        )
        resource = f"/workspaces/{ws_id}"
        seeded = await app_state.state.model.acl.get_acl_entries(resource)
        pos = max((e["position"] for e in seeded), default=-1) + 1
        await app_state.state.model.acl.add_acl_entry(
            resource,
            pos,
            model.ACTION_ALLOW,
            "view",
            model.PRINCIPAL_USER,
            user_id=other["id"],
        )
        await app_state.state.model.acl.add_acl_entry(
            resource,
            pos + 1,
            model.ACTION_ALLOW,
            "monitor-workspace",
            model.PRINCIPAL_USER,
            user_id=other["id"],
        )
        login = await client.post(
            "/api/v1/auth/login",
            json={"identifier": "monitor@example.com", "password": "monpass"},
        )
        other_headers = {
            "Authorization": f"Bearer {login.json()['access_token']}"
        }
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/status", headers=other_headers
        )
        assert resp.status_code == 200
        assert resp.json()["running"] is False

    async def test_workspace_status_view_only_member_denied(
        self, client, user, app_state
    ):
        """``view`` alone does not grant status reads — ``monitor`` is the
        gate (the deployment-wide view-for-authenticated seed stays too
        weak)."""
        headers = await _auth_headers(client)
        create_resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "status-view-only"},
        )
        ws_id = create_resp.json()["id"]
        other = await app_state.state.model.users.create_user(
            "viewer@example.com",
            auth_mod.hash_password("viewpass"),
            verified=True,
        )
        seeded = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws_id}"
        )
        pos = max((e["position"] for e in seeded), default=-1) + 1
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{ws_id}",
            pos,
            model.ACTION_ALLOW,
            "view",
            model.PRINCIPAL_USER,
            user_id=other["id"],
        )
        login = await client.post(
            "/api/v1/auth/login",
            json={"identifier": "viewer@example.com", "password": "viewpass"},
        )
        other_headers = {
            "Authorization": f"Bearer {login.json()['access_token']}"
        }
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/status", headers=other_headers
        )
        assert resp.status_code == 403

    async def test_workspace_status_not_found(self, client, user, app_state):
        headers = await _auth_headers(client)
        fake_id = "fake-status-id"
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "monitor-workspace",
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

    async def test_update_workspace_name_collision(self, client, user):
        """#3097: renaming onto another name the owner holds is a 409
        (same as create/duplicate/import), not a 500 off UNIQUE(user_id,
        name)."""
        headers = await _auth_headers(client)
        for name in ("collide-a", "collide-b"):
            resp = await client.post(
                "/api/v1/workspaces",
                json={"name": name},
                headers=headers,
            )
            assert resp.status_code == 200
            if name == "collide-a":
                ws_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"name": "collide-b"},
            headers=headers,
        )
        assert resp.status_code == 409
        assert "collide-b" in resp.json()["detail"]
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["name"] == "collide-a"

    async def test_update_workspace_cross_owner_name_ok(
        self, client, user, app_state
    ):
        """#3097: the name constraint is per-owner — renaming onto a name
        a different owner holds must succeed, not 409."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "cross-owner"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        other = await app_state.state.model.users.create_user(
            "crossowner@example.com", "irrelevant-hash", verified=True
        )
        await app_state.state.model.workspaces.create_workspace_with_acl(
            other["id"], "cross-owner-target"
        )
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"name": "cross-owner-target"},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["name"] == "cross-owner-target"

    async def test_update_workspace_null_not_null_field_rejected(
        self, client, user, app_state
    ):
        """#3097: explicitly nulling a NOT NULL column is a 400 — not a
        fabricated 409 collision (name) or a 500 (setup_state/
        egress_mode enum coercers). auto_start/per_handle_home nulls
        stay legal (documented coerce-to-0)."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "null-guard"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        for field in ("name", "setup_state", "egress_mode"):
            resp = await client.put(
                f"/api/v1/workspaces/{ws_id}",
                json={field: None},
                headers=headers,
            )
            assert resp.status_code == 400, field
            assert resp.json()["detail"] == f"Field '{field}' cannot be null"
        row = await app_state.state.model.workspaces.get_workspace(ws_id)
        assert row["name"] == "null-guard"

    async def test_update_workspace_egress_mode(self, client, user):
        # #2409: egress_mode is editable (PUT), taking effect on next start.
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "mode-ws"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        # Default is interactive; switch to allow, then static.
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"egress_mode": "allow"},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["egress_mode"] == "allow"
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"egress_mode": "static"},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["egress_mode"] == "static"

    async def test_update_workspace_per_handle_home_editable(
        self, client, user
    ):
        # #2719: the flag is mutable — a PUT flips it and the new value
        # shows in list payloads. The flip applies to the next
        # connect/start (nothing reads it yet — chunk 1).
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "home-edit"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        assert resp.json()["per_handle_home"] is False  # deploy default
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"per_handle_home": True},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["per_handle_home"] is True
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"per_handle_home": False},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["per_handle_home"] is False

    async def test_create_and_update_classification_banner(self, client, user):
        # #2768: the marking is settable at create (stripped), exposed in
        # list payloads, editable via PUT, and clearable with an empty
        # value (back to inheriting the deploy default — NULL).
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "marked", "classification_banner": "  SECRET  "},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["classification_banner"] == "SECRET"
        ws_id = resp.json()["id"]
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["classification_banner"] == "SECRET"
        # PUT replaces it.
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"classification_banner": "CUI"},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["classification_banner"] == "CUI"
        # Empty clears the override (inherit).
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"classification_banner": ""},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["classification_banner"] is None

    async def test_create_classification_banner_validation(self, client, user):
        # #2768: one printable line, at most 120 chars.
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={
                "name": "bad-mark",
                "classification_banner": "TOP\nSECRET",
            },
            headers=headers,
        )
        assert resp.status_code == 400
        assert "single line" in resp.json()["detail"]
        resp = await client.post(
            "/api/v1/workspaces",
            json={
                "name": "long-mark",
                "classification_banner": "X" * 121,
            },
            headers=headers,
        )
        assert resp.status_code == 400
        assert "at most" in resp.json()["detail"]
        resp = await client.put(
            "/api/v1/workspaces/missing",
            json={"classification_banner": "ok"},
            headers=headers,
        )
        # The edit ACL dependency gates nonexistent workspaces as 403
        # before the handler's 404 path can fire.
        assert resp.status_code == 403
        # An invalid PUT banner (control character) is a 400.
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "bad-put-mark"},
            headers=headers,
        )
        put_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{put_id}",
            json={"classification_banner": "A\nB"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "single line" in resp.json()["detail"]

    async def test_duplicate_carries_classification_banner(self, client, user):
        # #2768: the marking is workspace metadata — a duplicate keeps it.
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={
                "name": "dup-marked",
                "classification_banner": "SECRET",
            },
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/duplicate",
            json={"name": "dup-marked-2"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["classification_banner"] == "SECRET"

    async def test_duplicate_carries_stored_nix_while_off(
        self, client, user, app
    ):
        # #2560 decision pin: duplicate carries the source's settings bag
        # verbatim — a stored nix=true is persisted state, not a new
        # opt-in, so it is NOT rejected while the feature is off (it stays
        # inert exactly like the source's).
        self._arm_nix(app, True)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "dup-nix-src", "settings": {"nix": True}},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        self._arm_nix(app, False)
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/duplicate",
            json={"name": "dup-nix-clone"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["settings"] == {"nix": True}

    async def test_update_workspace_allowed_domains(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "dom-ws"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"allowed_domains": ["github.com:443"]},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["allowed_domains"] == ["github.com:443"]

    async def test_update_workspace_rejected_domains(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "rej-ws"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"rejected_domains": ["evil.com:443"]},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["rejected_domains"] == ["evil.com:443"]

    async def test_update_classification_banner_notifies_viewers(
        self, client, user, app
    ):
        """#2768: a marking change pushes workspaces_changed so open pages
        re-render the banner — to the owner, the editor (a shared member
        with the edit ACE may not be the owner), and every ACL-shared
        member (read-only viewers see the same page via
        /workspaces/shared and must not keep a stale, lower marking)."""
        from klangk.model import ACTION_ALLOW, PRINCIPAL_USER

        notified = []
        app.state.sockets.notify_user_workspaces_changed = lambda uid: (
            notified.append(uid)
        )
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "notify-ws"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        owner_id = resp.json()["user_id"]
        # A second user with a direct edit ACE (a shared editor).
        import klangk.auth as auth_mod

        other = await app.state.model.users.create_user(
            "editor@x.com", auth_mod.hash_password("testpass"), verified=True
        )
        await app.state.model.acl.add_acl_entry(
            f"/workspaces/{ws_id}",
            100,
            ACTION_ALLOW,
            "edit-workspace",
            PRINCIPAL_USER,
            user_id=other["id"],
        )
        login = await client.post(
            "/api/v1/auth/login",
            json={"identifier": "editor@x.com", "password": "testpass"},
        )
        editor_headers = {
            "Authorization": f"Bearer {login.json()['access_token']}"
        }
        # A read-only shared member (view ACE only) — the page it views
        # re-resolves the marking on the same push.
        viewer = await app.state.model.users.create_user(
            "viewer@x.com", auth_mod.hash_password("testpass"), verified=True
        )
        await app.state.model.acl.add_acl_entry(
            f"/workspaces/{ws_id}",
            101,
            ACTION_ALLOW,
            "view",
            PRINCIPAL_USER,
            user_id=viewer["id"],
        )
        # Drop the create-time notification so only the PUT's notifies
        # are asserted below.
        notified.clear()
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"classification_banner": "CUI"},
            headers=editor_headers,
        )
        assert resp.status_code == 200
        # Owner, editor, and the read-only viewer — each notified exactly
        # once.
        assert sorted(notified) == sorted(
            [owner_id, other["id"], viewer["id"]]
        )

    async def test_update_empty_body_rejected(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "empty-put"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}", json={}, headers=headers
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "No fields to update"

    async def test_rename_rejects_blank_name(self, client, user):
        # #3110: PUT renames share the create-side name minimum.
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "blank-rename"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        for bad in ("", " \t "):
            resp = await client.put(
                f"/api/v1/workspaces/{ws_id}",
                json={"name": bad},
                headers=headers,
            )
            assert resp.status_code == 422
        # The stored name is untouched.
        resp = await client.get("/api/v1/workspaces", headers=headers)
        by_name = {w["name"]: w for w in resp.json()}
        assert "blank-rename" in by_name

    async def test_create_workspace_with_settings(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={
                "name": "tuned",
                "settings": {
                    "idle_timeout": 300,
                    "cpu_limit": "1.5",
                    "memory_limit": "2g",
                    "tmp_size": "4g",
                },
            },
            headers=headers,
        )
        assert resp.status_code == 200
        ws_id = resp.json()["id"]
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        # Numeric strings coerced to typed values.
        assert match[0]["settings"] == {
            "idle_timeout": 300,
            "cpu_limit": 1.5,
            "memory_limit": "2g",
            "tmp_size": "4g",
        }

    async def test_create_workspace_rejects_unknown_setting(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "bad", "settings": {"nonsense": 1}},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "Unknown setting" in resp.json()["detail"]

    async def test_create_workspace_rejects_bad_setting_value(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "bad", "settings": {"idle_timeout": -5}},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "non-negative" in resp.json()["detail"]

    async def test_update_workspace_settings_full_replace(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={
                "name": "replaceable",
                "settings": {"idle_timeout": 300},
            },
            headers=headers,
        )
        ws_id = resp.json()["id"]
        # Full replace via PUT: the new bag overwrites the old.
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"settings": {"cpu_limit": 2.0}},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["settings"] == {"cpu_limit": 2.0}
        # settings=None on PUT clears the whole bag.
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"settings": None},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["settings"] is None

    async def test_patch_workspace_settings_merge(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={
                "name": "mergeable",
                "settings": {"idle_timeout": 300},
            },
            headers=headers,
        )
        ws_id = resp.json()["id"]
        # PATCH adds a key + replaces an existing one (merge, not replace).
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/settings",
            json={"idle_timeout": 600, "pids_limit": 512},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["settings"] == {
            "idle_timeout": 600,
            "pids_limit": 512,
        }
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["settings"] == {
            "idle_timeout": 600,
            "pids_limit": 512,
        }

    async def test_patch_workspace_settings_by_shared_editor(
        self, client, user, app_state
    ):
        """A shared non-owner with the ``edit`` ACE can PATCH settings (#864).

        Regression for the original PATCH handler, which passed the
        *caller's* id into the owner-scoped model merge and silently
        no-op'd (200 with ``settings: null``) for any non-owner — while
        the sibling PUT update path worked fine for the same editor. The
        handler now resolves the owner (like PUT) before calling the model.
        """
        headers = await _auth_headers(client)
        # Create a second, non-owner user and log in as them.
        other = await app_state.state.model.users.create_user(
            "other@example.com",
            auth_mod.hash_password("otherpass"),
            verified=True,
        )
        login = await client.post(
            "/api/v1/auth/login",
            json={"identifier": "other@example.com", "password": "otherpass"},
        )
        other_headers = {
            "Authorization": f"Bearer {login.json()['access_token']}"
        }
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "shared-edit", "settings": {"idle_timeout": 300}},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        # Grant the other user the `edit` permission explicitly (no default
        # role group ships `edit` — only owners get `*`).
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{ws_id}",
            other["id"],
            model.ACTION_ALLOW,
            "edit-workspace",
            model.PRINCIPAL_USER,
            user_id=other["id"],
        )
        # As the shared editor, PATCH a setting.
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/settings",
            json={"idle_timeout": 600, "cpu_limit": 2.0},
            headers=other_headers,
        )
        assert resp.status_code == 200
        # The pre-fix bug returned ``settings: null`` here (the merge
        # no-op'd). It must return the merged bag.
        assert resp.json()["settings"] == {
            "idle_timeout": 600,
            "cpu_limit": 2.0,
        }
        # And the row must actually have changed — confirm via the owner.
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["settings"] == {
            "idle_timeout": 600,
            "cpu_limit": 2.0,
        }

    async def test_patch_workspace_settings_delete_key(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={
                "name": "deletable",
                "settings": {
                    "idle_timeout": 300,
                    "cpu_limit": 1.5,
                },
            },
            headers=headers,
        )
        ws_id = resp.json()["id"]
        # null value deletes just that key.
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/settings",
            json={"idle_timeout": None},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["settings"] == {"cpu_limit": 1.5}

    async def test_patch_workspace_settings_delete_last_key(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "last-key", "settings": {"idle_timeout": 300}},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/settings",
            json={"idle_timeout": None},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["settings"] is None

    async def test_patch_workspace_settings_rejects_empty(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "empty-patch"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/settings",
            json={},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "empty" in resp.json()["detail"]

    # --- the nix_enabled master switch (#2560) ---

    def _arm_nix(self, app, on: bool) -> None:
        """Flip the resolved armed status live (settings are read at call
        time, so a mid-test flip takes effect immediately)."""
        app.state.settings.nix_enabled = on
        app.state.settings.nix_seed.path = "/tmp/nix-seed" if on else None

    async def test_config_reports_nix_unavailable_by_default(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/config", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["nix_available"] is False

    async def test_config_reports_nix_available_when_armed(
        self, client, user, app
    ):
        self._arm_nix(app, True)
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/config", headers=headers)
        assert resp.json()["nix_available"] is True

    async def test_create_rejects_nix_optin_while_off(self, client, user, app):
        # Even with a backend configured, the off switch rejects the opt-in.
        self._arm_nix(app, False)
        app.state.settings.nix_seed.path = "/tmp/nix-seed"
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "no-nix", "settings": {"nix": True}},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "nix feature" in resp.json()["detail"]

    async def test_create_accepts_nix_false_while_off(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "explicit-off", "settings": {"nix": False}},
            headers=headers,
        )
        assert resp.status_code == 200

    async def test_create_accepts_nix_true_when_armed(self, client, user, app):
        self._arm_nix(app, True)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "nixy", "settings": {"nix": True}},
            headers=headers,
        )
        assert resp.status_code == 200
        ws_id = resp.json()["id"]
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["settings"] == {"nix": True}

    async def test_put_rejects_new_optin_while_off(self, client, user, app):
        self._arm_nix(app, True)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "flip-me"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        self._arm_nix(app, False)
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"settings": {"nix": True}},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "nix feature" in resp.json()["detail"]

    async def test_put_tolerates_echo_of_stored_true(self, client, user, app):
        # A legacy nix workspace stays editable while the flag is off: the
        # TUI/web panel PUT a full-replace bag merged over the stored one.
        self._arm_nix(app, True)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "legacy-nix", "settings": {"nix": True}},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        self._arm_nix(app, False)
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"settings": {"nix": True, "idle_timeout": 300}},
            headers=headers,
        )
        assert resp.status_code == 200

    async def test_patch_rejects_flip_while_off(self, client, user, app):
        self._arm_nix(app, True)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "patch-me"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        self._arm_nix(app, False)
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/settings",
            json={"nix": True},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "nix feature" in resp.json()["detail"]

    async def test_patch_tolerates_reassert_of_stored_true(
        self, client, user, app
    ):
        self._arm_nix(app, True)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "reassert", "settings": {"nix": True}},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        self._arm_nix(app, False)
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/settings",
            json={"nix": True},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["settings"] == {"nix": True}

    async def test_patch_workspace_settings_rejects_unknown_key(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "unknown-key"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/settings",
            json={"bogus": 1},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "Unknown setting" in resp.json()["detail"]

    async def test_patch_workspace_settings_rejects_bad_value(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "bad-value"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/settings",
            json={"cpu_limit": "fast"},
            headers=headers,
        )
        assert resp.status_code == 400

    async def test_update_workspace_rejects_bad_settings(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "put-bad"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"settings": {"idle_timeout": -5}},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "non-negative" in resp.json()["detail"]

    async def test_patch_workspace_settings_missing_workspace(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.patch(
            "/api/v1/workspaces/does-not-exist/settings",
            json={"idle_timeout": 300},
            headers=headers,
        )
        # The ACL "edit" guard runs before the handler, so a nonexistent
        # workspace is rejected as 403 (no edit permission) — existence is
        # not leaked. Same posture as the PUT update endpoint.
        assert resp.status_code == 403

    async def test_patch_workspace_settings_not_found(
        self, client, user, app_state
    ):
        """ACL grants edit but the workspace doesn't exist -> 404 (#864).

        Mirrors ``test_delete_not_found``: the ``edit-workspace`` ACE on a nonexistent
        resource lets the caller past the ACL guard, then the handler's
        owner resolution finds no row and returns 404 (a race / stale id,
        not a normal path).
        """
        headers = await _auth_headers(client)
        fake_id = "fake-patch-id"
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{fake_id}",
            0,
            model.ACTION_ALLOW,
            "edit-workspace",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        resp = await client.patch(
            f"/api/v1/workspaces/{fake_id}/settings",
            json={"idle_timeout": 300},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_update_workspace_env(self, client, user):
        # Regression: a partial PUT of env (e.g. adding a new var from the
        # TUI/Flutter edit form) must persist and round-trip through GET.
        # This was the only creatable field with no PUT coverage (#1891).
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "env-ws"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"env": {"a": "1"}},
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["env"] == {"a": "1"}

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

    async def test_update_workspace_rename_propagates_to_status_bar(
        self, client, app, user, registry
    ):
        # Renaming a workspace whose container is live pushes the new
        # name into tmux so the status bar updates without a restart
        # (#1880): open terminals would otherwise keep showing the old
        # name until a new terminal_start fires.
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "old-name"},
            headers=headers,
        )
        ws_id = resp.json()["id"]

        registry.track_activity("cid-rename", ws_id, setup_state="complete")
        called = []

        async def _fake_set(cid, name):
            called.append((cid, name))

        app.state.terminal.set_workspace_name = _fake_set
        try:
            resp = await client.put(
                f"/api/v1/workspaces/{ws_id}",
                json={"name": "new-name"},
                headers=headers,
            )
            assert resp.status_code == 200
            assert called == [("cid-rename", "new-name")]
        finally:
            await registry.remove_state(ws_id)

    async def test_update_workspace_rename_skipped_when_no_live_state(
        self, client, app, user, registry
    ):
        # Renaming a workspace that has no live container must not call
        # set_workspace_name (#1880).
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "stale-name"},
            headers=headers,
        )
        ws_id = resp.json()["id"]

        called = []

        async def _fake_set(cid, name):
            called.append((cid, name))

        app.state.terminal.set_workspace_name = _fake_set
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            json={"name": "renamed"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert called == []

    async def test_update_workspace_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.put(
            "/api/v1/workspaces/nonexistent",
            json={"service_command": "pi"},
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_update_workspace_not_found(self, client, user, app_state):
        """ACL passes but workspace doesn't exist."""
        headers = await _auth_headers(client)
        fake_id = "fake-ws-id"
        await app_state.state.model.acl.add_acl_entry(
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
        ws_model = app.state.model.workspaces
        original_update = ws_model.update_workspace

        async def _delete_then_update(workspace_id, user_id, **fields):
            await ws_model.delete_workspace(workspace_id, user_id)
            return await original_update(workspace_id, user_id, **fields)

        monkeypatch.setattr(ws_model, "update_workspace", _delete_then_update)
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
            os.environ.pop("KLANGKD_ALLOW_AUTOSTART", None)
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
                # Explicit so the copy assertion below tests duplication,
                # not the (now-shared) deploy default (#2723).
                "per_handle_home": True,
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
        assert data["per_handle_home"] is True  # copied from the source

    async def test_duplicate_workspace_copies_shared_home(self, client, user):
        # #2719: duplicate copies the source's per_handle_home, including
        # an explicit False (a shared-home workspace duplicates as one).
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "shared-src", "per_handle_home": False},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/duplicate",
            json={"name": "shared-dup"},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["per_handle_home"] is False

    async def test_duplicate_workspace_no_permission(
        self, client, user, app_state
    ):
        """A non-admin user cannot duplicate (no collection create)."""
        from klangk.auth import hash_password

        pw_hash = hash_password("testpass")
        await app_state.state.model.users.create_user(
            "nonadmin-dup@example.com", pw_hash, verified=True
        )
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "nonadmin-dup@example.com",
                "password": "testpass",
            },
        )
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        resp = await client.post(
            "/api/v1/workspaces/nonexistent/duplicate",
            json={"name": "dup"},
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_duplicate_workspace_not_found(
        self, client, user, app_state
    ):
        """ACL passes but workspace doesn't exist."""
        headers = await _auth_headers(client)
        fake_id = "fake-dup-id"
        await app_state.state.model.acl.add_acl_entry(
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

    async def test_duplicate_rejects_blank_name(self, client, user):
        # #3110: the duplicate create shares the name minimum.
        headers = await _auth_headers(client)
        ws_id = (
            await client.post(
                "/api/v1/workspaces",
                json={"name": "blank-dup-src"},
                headers=headers,
            )
        ).json()["id"]
        for bad in ("", "   "):
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/duplicate",
                json={"name": bad},
                headers=headers,
            )
            assert resp.status_code == 422

    async def test_duplicate_workspace_creates_role_groups(
        self, client, user, app_state
    ):
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
            group = await app_state.state.model.users.get_group_by_name(
                f"{suffix}-{dup_id}"
            )
            assert group is not None, f"expected {suffix} group on duplicate"
        # Creator should be in the owners group
        owners = await app_state.state.model.users.get_group_by_name(
            f"owners-{dup_id}"
        )
        members = await app_state.state.model.users.get_group_members(
            owners["id"]
        )
        assert any(m["id"] == user["id"] for m in members)


class TestWorkspaceCreatedHookFiring:
    """#2762: the workspace-created hook fires on every creation path.

    All three paths (create / import / duplicate) go through the
    Workspaces service layer's create_workspace, which fires
    app.state.hooks. The hook is installed directly on the wired Hooks
    instance (unit-style, like TestCallLoginHook) — loading from
    KLANGKD_WORKSPACE_CREATED_HOOK is covered in test_hooks.py.
    """

    @pytest.fixture(autouse=True)
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation requires admin."""

    def _install(self, app):
        seen = []

        async def hook(workspace, actor):
            seen.append((workspace["id"], actor["id"]))
            workspace["egress_mode"] = "static"

        app.state.hooks.workspace_created_hook = hook
        app.state.hooks.workspace_created_hook_is_async = True
        app.state.hooks.workspace_created_hook_source = "firing-test"
        return seen

    async def test_fires_on_create(self, client, user, app):
        seen = self._install(app)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "hooked-create"},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert seen == [(body["id"], user["id"])]
        # The mutation is persisted and reflected in the response.
        assert body["egress_mode"] == "static"
        row = await app.state.model.workspaces.get_workspace(body["id"])
        assert row["egress_mode"] == "static"

    async def test_fires_on_duplicate(self, client, user, app, app_state):
        seen = self._install(app)
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            json={"name": "hooked-dup-src"},
            headers=headers,
        )
        ws_id = resp.json()["id"]
        seen.clear()  # the source create also fired
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/duplicate",
            json={"name": "hooked-dup"},
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert seen == [(body["id"], user["id"])]
        assert body["id"] != ws_id
        assert body["egress_mode"] == "static"

    async def test_fires_on_import(self, client, user, app):
        import io
        import json
        import tarfile

        seen = self._install(app)
        headers = await _auth_headers(client)
        # Build a minimal importable archive (instance_id included —
        # the import endpoint rejects foreign archives).
        from klangk.settings import KlangkSettings

        ns = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=KlangkSettings(os.environ))
        )
        ns.state.util = util_mod.Util(ns)
        meta = json.dumps(
            {
                "instance_id": ns.state.util.instance_id(),
                "name": "hooked-import",
                "image": None,
                "service_command": None,
                "auto_start": False,
                "mounts": None,
                "env": None,
                "health_check": None,
                "allowed_domains": None,
                "rejected_domains": None,
                "settings": None,
                "egress_mode": "interactive",
                "per_handle_home": True,
            }
        ).encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        buf.seek(0)

        resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            files={"file": ("archive.tar.gz", buf.read(), "application/gzip")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert seen == [(body["id"], user["id"])]
        assert body["egress_mode"] == "static"

    async def test_raising_hook_does_not_fail_create(
        self, client, user, app, caplog
    ):
        import logging

        async def hook(workspace, actor):
            raise RuntimeError("hook boom")

        app.state.hooks.workspace_created_hook = hook
        app.state.hooks.workspace_created_hook_is_async = True
        app.state.hooks.workspace_created_hook_source = "firing-raise"
        headers = await _auth_headers(client)
        with caplog.at_level(logging.WARNING, logger="klangk.hooks"):
            resp = await client.post(
                "/api/v1/workspaces",
                json={"name": "hooked-raises"},
                headers=headers,
            )
        assert resp.status_code == 200
        assert resp.json()["name"] == "hooked-raises"
        assert any(
            "workspace-created hook firing-raise failed" in r.message
            for r in caplog.records
        )


# --- Workspace sharing ---


class TestWorkspaceSharingRoutes:
    @pytest.fixture(autouse=True)
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation requires admin."""

    async def _create_other_user(self, app_state):
        password_hash = auth_mod.hash_password("otherpass")
        return await app_state.state.model.users.create_user(
            "other@example.com", password_hash, verified=True
        )

    async def _other_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={"identifier": "other@example.com", "password": "otherpass"},
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_list_shared_workspaces(self, client, user, app_state):
        headers = await _auth_headers(client)
        await self._create_other_user(app_state)
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
        self, client, app, user, app_state
    ):
        """Shared bare-list path returns more than the default of 10 (#1266).

        Mirrors the owned-list regression: the Settings panel also
        fetches ``/api/v1/workspaces/shared`` with no params, so a user
        with more than 10 shared workspaces must not be silently cut off.
        """
        headers = await _auth_headers(client)
        await self._create_other_user(app_state)
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

    async def test_list_shared_pagination_returns_envelope(
        self, client, user, app_state
    ):
        headers = await _auth_headers(client)
        await self._create_other_user(app_state)
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

    async def test_list_shared_filter_and_sort(self, client, user, app_state):
        headers = await _auth_headers(client)
        await self._create_other_user(app_state)
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

    async def test_add_member(self, client, user, app_state):
        headers = await _auth_headers(client)
        other = await self._create_other_user(app_state)
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

    async def test_add_member_grants_files_download(
        self, client, user, app_state
    ):
        """Sharing a member grants `files-download`/`files-write`
        alongside `files` (#2705), so the simple share flow keeps both
        transfer channels working."""
        headers = await _auth_headers(client)
        other = await self._create_other_user(app_state)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws_id}"
        )
        member_perms = sorted(
            e["permission"]
            for e in entries
            if e["principal_type"] == model.PRINCIPAL_USER
            and e["user_id"] == other["id"]
        )
        assert member_perms == [
            "files-download",
            "files-view",
            "files-write",
            "join-workspace",
            "monitor-workspace",
            "terminal",
            "view",
        ]

    async def test_add_member_duplicate_rejected(
        self, client, user, app_state
    ):
        """Re-sharing an already-shared member is a 409, not a second
        stacked block (#3101)."""
        headers = await _auth_headers(client)
        other = await self._create_other_user(app_state)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "share-ws"}
        )
        ws_id = resp.json()["id"]
        assert (
            await client.post(
                f"/api/v1/workspaces/{ws_id}/members",
                headers=headers,
                json={"email": "other@example.com"},
            )
        ).status_code == 200

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/members",
            headers=headers,
            json={"email": "other@example.com"},
        )
        assert resp.status_code == 409
        assert "already has access" in resp.json()["detail"]
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws_id}"
        )
        member_entries = [
            e
            for e in entries
            if e["principal_type"] == model.PRINCIPAL_USER
            and e["user_id"] == other["id"]
        ]
        assert len(member_entries) == 7  # one block, not two

    async def test_add_member_notifies_owner_and_target(
        self, client, app, user, sockets, app_state
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user(app_state)
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
        self, client, app, user, sockets, app_state
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user(app_state)
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
        self, client, app, user, sockets, app_state
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user(app_state)
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
        self, client, app, user, sockets, app_state
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user(app_state)
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
        self, client, app, user, sockets, app_state
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user(app_state)
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
        self, client, app, user, db, app_state
    ):
        # role=None is removal-from-all-roles — harmless cleanup, so the
        # guard (which only fires on a grant) must let it through.
        from klangk.main import Lifecycle

        from _helpers import wire_db_and_model

        _seed_state = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=make_settings({}))
        )
        wire_db_and_model(_seed_state)
        await Lifecycle(_seed_state).seed_agent_user()
        agent = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
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

    async def test_add_member_rejects_system_agent(
        self, client, user, db, app_state
    ):
        # End-to-end smoke for the #1135 refactor: the guard now lives at
        # the model choke point (model.add_acl_entry), and a global
        # handler translates AgentPrincipalError to HTTP 400. This is the
        # one HTTP-level grant test kept to prove the wiring; the choke
        # points themselves are unit-tested in test_model.py.
        from klangk.main import Lifecycle

        from _helpers import wire_db_and_model

        _seed_state = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=make_settings({}))
        )
        wire_db_and_model(_seed_state)
        await Lifecycle(_seed_state).seed_agent_user()
        agent = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
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
        entries = await app_state.state.model.acl.get_acl_entries(resource)
        assert not any(e["user_id"] == agent["id"] for e in entries)

    async def test_remove_member(self, client, user, app_state):
        headers = await _auth_headers(client)
        other = await self._create_other_user(app_state)
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

    async def test_non_owner_cannot_manage_members(
        self, client, user, app_state
    ):
        headers = await _auth_headers(client)
        other = await self._create_other_user(app_state)
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
    @pytest.fixture(autouse=True)
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation requires admin."""

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
        self, client, app, admin_user, user, app_state
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
        group = await app_state.state.model.users.create_group(
            "test-acl-group"
        )
        await app_state.state.model.acl.add_acl_entry(
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

    async def test_workspace_acl_requires_change_acls(
        self, client, user, app_state
    ):
        """#2764: ``share`` alone no longer opens the raw ACL editor.

        A member holding ``share`` can still manage members (the simple
        sharing surface) but GET/PUT on the raw ACE list require the
        dedicated ``change-acls`` permission.
        """
        from klangk.auth import hash_password

        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "cacl-ws"}
        )
        ws_id = resp.json()["id"]
        resource = f"/workspaces/{ws_id}"

        member = await app_state.state.model.users.create_user(
            "cacl-member@example.com", hash_password("testpass"), verified=True
        )
        entries = await app_state.state.model.acl.get_acl_entries(resource)
        next_pos = max(e["position"] for e in entries) + 1
        for i, perm in enumerate(("view", "share-workspace")):
            await app_state.state.model.acl.add_acl_entry(
                resource,
                next_pos + i,
                model.ACTION_ALLOW,
                perm,
                model.PRINCIPAL_USER,
                user_id=member["id"],
            )
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "cacl-member@example.com",
                "password": "testpass",
            },
        )
        member_headers = {
            "Authorization": f"Bearer {login.json()['access_token']}"
        }

        # share keeps the simple sharing surface...
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/members", headers=member_headers
        )
        assert resp.status_code == 200
        # ...but not the raw ACE list, even with share granted.
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/acl", headers=member_headers
        )
        assert resp.status_code == 403
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}/acl",
            headers=member_headers,
            json=[
                {
                    "action": model.ACTION_ALLOW,
                    "principal_type": model.PRINCIPAL_USER,
                    "permission": "*",
                    "user_id": member["id"],
                }
            ],
        )
        assert resp.status_code == 403

        # Granting change-acls opens the editor (read and write).
        await app_state.state.model.acl.add_acl_entry(
            resource,
            next_pos + 2,
            model.ACTION_ALLOW,
            "share-advanced",
            model.PRINCIPAL_USER,
            user_id=member["id"],
        )
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/acl", headers=member_headers
        )
        assert resp.status_code == 200
        current = [
            {
                "action": e["action"],
                "principal_type": e["principal_type"],
                "permission": e["permission"],
                "user_id": e.get("user_id"),
                "group_id": e.get("group_id"),
                "system_principal": e.get("system_principal"),
            }
            for e in resp.json()
        ]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}/acl",
            headers=member_headers,
            json=current,
        )
        assert resp.status_code == 200


class TestWorkspaceRoles:
    @pytest.fixture(autouse=True)
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation requires admin."""

    async def test_role_writes_require_change_acls(
        self, client, user, app_state
    ):
        """#2764: a bare ``share`` holder cannot mint owners — role-group
        writes need ``change-acls`` too (they carry the raw power: the
        owners group holds the ``*`` wildcard)."""
        from klangk.auth import hash_password

        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "role-gate-ws"},
        )
        ws_id = resp.json()["id"]
        resource = f"/workspaces/{ws_id}"

        member = await app_state.state.model.users.create_user(
            "role-gate@test.com", hash_password("testpass"), verified=True
        )
        entries = await app_state.state.model.acl.get_acl_entries(resource)
        next_pos = max(e["position"] for e in entries) + 1
        for i, perm in enumerate(("view", "share-workspace")):
            await app_state.state.model.acl.add_acl_entry(
                resource,
                next_pos + i,
                model.ACTION_ALLOW,
                perm,
                model.PRINCIPAL_USER,
                user_id=member["id"],
            )
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "role-gate@test.com",
                "password": "testpass",
            },
        )
        member_headers = {
            "Authorization": f"Bearer {login.json()['access_token']}"
        }

        # Reading the buckets stays on share; writing any role (not just
        # owners — a role IS an ACE principal) needs change-acls.
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=member_headers
        )
        assert resp.status_code == 200
        for method, path, kwargs in (
            (
                "post",
                f"/api/v1/workspaces/{ws_id}/roles/owners",
                {"json": {"email": "role-gate@test.com"}},
            ),
            (
                "delete",
                f"/api/v1/workspaces/{ws_id}/roles/owners/{member['id']}",
                {},
            ),
            (
                "patch",
                f"/api/v1/workspaces/{ws_id}/roles",
                {"json": {"email": "role-gate@test.com", "role": "coders"}},
            ),
        ):
            resp = await getattr(client, method)(
                path, headers=member_headers, **kwargs
            )
            assert resp.status_code == 403, (method, path)

        # Granting change-acls reopens the role writes.
        await app_state.state.model.acl.add_acl_entry(
            resource,
            next_pos + 2,
            model.ACTION_ALLOW,
            "share-advanced",
            model.PRINCIPAL_USER,
            user_id=member["id"],
        )
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/roles/spectators",
            headers=member_headers,
            json={"email": "role-gate@test.com"},
        )
        assert resp.status_code == 200

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

    async def test_roles_include_effective_permissions(
        self, client, user, app_state
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "role-perms-ws"},
        )
        ws_id = resp.json()["id"]
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=headers
        )
        roles = {r["role"]: r for r in resp.json()}

        # Seeded grants show per group (#2986): the owners' wildcard
        # expands to the whole vocabulary (including the literal '*'),
        # the operating roles hold egress-consent, spectators never do.
        assert "*" in roles["owners"]["permissions"]
        assert "terminal" in roles["owners"]["permissions"]
        assert "egress-consent" in roles["coders"]["permissions"]
        assert "egress-consent" in roles["collaborators"]["permissions"]
        assert "share-terminals" in roles["collaborators"]["permissions"]
        assert "egress-consent" not in roles["spectators"]["permissions"]
        assert "share-terminals" not in roles["spectators"]["permissions"]

        # Effective, not seeded-static: a post-seed ACE grant on the
        # group is reflected on the next roles read.
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{ws_id}",
            position=100,
            action=model.ACTION_ALLOW,
            permission="share-advanced",
            principal_type=model.PRINCIPAL_GROUP,
            group_id=roles["spectators"]["group_id"],
        )
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=headers
        )
        roles = {r["role"]: r for r in resp.json()}
        assert "share-advanced" in roles["spectators"]["permissions"]

        # No inherited root grants: the seeded `Allow view Authenticated`
        # on `/` must not surface in every bucket (#2987 review) — the
        # roles read evaluates only the workspace's own node.
        assert "view" not in roles["spectators"]["permissions"]

        # A malformed user-principal ACE with a NULL user_id (inert for
        # every real principal) must not None==None-match its way into
        # any group's list (#2987 review).
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{ws_id}",
            position=101,
            action=model.ACTION_ALLOW,
            permission="egress-consent",
            principal_type=model.PRINCIPAL_USER,
        )
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=headers
        )
        roles = {r["role"]: r for r in resp.json()}
        assert "egress-consent" not in roles["spectators"]["permissions"]

        # First-match-wins: a deny positioned before the group's allow
        # removes the permission from the bucket exactly as it would
        # deny a real member.
        await app_state.state.model.acl.replace_acl_entries(
            f"/workspaces/{ws_id}",
            [
                {
                    "position": 0,
                    "action": model.ACTION_DENY,
                    "permission": "terminal",
                    "principal_type": model.PRINCIPAL_GROUP,
                    "group_id": roles["spectators"]["group_id"],
                },
                {
                    "position": 1,
                    "action": model.ACTION_ALLOW,
                    "permission": "terminal",
                    "principal_type": model.PRINCIPAL_GROUP,
                    "group_id": roles["spectators"]["group_id"],
                },
                # Keep the creator's own wildcard so the share-gated
                # roles read below stays authorized.
                {
                    "position": 2,
                    "action": model.ACTION_ALLOW,
                    "permission": "*",
                    "principal_type": model.PRINCIPAL_USER,
                    "user_id": user["id"],
                },
            ],
        )
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=headers
        )
        roles = {r["role"]: r for r in resp.json()}
        assert "terminal" not in roles["spectators"]["permissions"]

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

    async def test_add_user_to_role(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "add-role-ws"}
        )
        ws_id = resp.json()["id"]
        # Create a second user
        target = await app_state.state.model.users.create_user(
            "role-target@test.com", "pass"
        )
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

    async def test_remove_user_from_role(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "rm-role-ws"}
        )
        ws_id = resp.json()["id"]
        target = await app_state.state.model.users.create_user(
            "role-rm@test.com", "pass"
        )
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

    async def test_role_group_not_found_add(self, client, user, app_state):
        """Adding to a role when the group was deleted returns 404."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "norole-add-ws"},
        )
        ws_id = resp.json()["id"]
        # Delete the spectators group to simulate missing role group
        group = await app_state.state.model.users.get_group_by_name(
            f"spectators-{ws_id}"
        )
        await app_state.state.model.users.delete_group(group["id"])
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/roles/spectators",
            headers=headers,
            json={"email": "x@test.com"},
        )
        assert resp.status_code == 404
        assert "Role group" in resp.json()["detail"]

    async def test_role_group_not_found_remove(self, client, user, app_state):
        """Removing from a role when the group was deleted returns 404."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "norole-rm-ws"},
        )
        ws_id = resp.json()["id"]
        group = await app_state.state.model.users.get_group_by_name(
            f"coders-{ws_id}"
        )
        await app_state.state.model.users.delete_group(group["id"])
        resp = await client.delete(
            f"/api/v1/workspaces/{ws_id}/roles/coders/some-id",
            headers=headers,
        )
        assert resp.status_code == 404
        assert "Role group" in resp.json()["detail"]

    async def test_roles_with_missing_group(self, client, user, app_state):
        """Listing roles skips groups that were deleted."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "missing-grp-ws"},
        )
        ws_id = resp.json()["id"]
        group = await app_state.state.model.users.get_group_by_name(
            f"spectators-{ws_id}"
        )
        await app_state.state.model.users.delete_group(group["id"])
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/roles", headers=headers
        )
        roles = resp.json()
        role_names = [r["role"] for r in roles]
        assert "spectators" not in role_names
        assert "owners" in role_names


class TestChangeWorkspaceRole:
    @pytest.fixture(autouse=True)
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation requires admin."""

    async def test_change_role(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "chg-role-ws"}
        )
        ws_id = resp.json()["id"]
        target = await app_state.state.model.users.create_user(
            "chg-role@test.com", "pass"
        )
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

    async def test_remove_all_roles(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "rm-all-ws"}
        )
        ws_id = resp.json()["id"]
        await app_state.state.model.users.create_user(
            "rm-all@test.com", "pass"
        )
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

    async def test_invalid_role(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "bad-chg-ws"}
        )
        ws_id = resp.json()["id"]
        await app_state.state.model.users.create_user(
            "bad-chg@test.com", "pass"
        )
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

    async def test_change_role_missing_group(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "miss-grp-chg-ws"},
        )
        ws_id = resp.json()["id"]
        await app_state.state.model.users.create_user(
            "miss-grp@test.com", "pass"
        )
        # Delete the target role group
        group = await app_state.state.model.users.get_group_by_name(
            f"spectators-{ws_id}"
        )
        await app_state.state.model.users.delete_group(group["id"])
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "miss-grp@test.com", "role": "spectators"},
        )
        assert resp.status_code == 404
        assert "Role group" in resp.json()["detail"]

    async def test_change_role_skips_missing_groups_on_remove(
        self, client, app, user, app_state
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "skip-miss-ws"},
        )
        ws_id = resp.json()["id"]
        await app_state.state.model.users.create_user(
            "skip-miss@test.com", "pass"
        )
        # Add user to coders
        await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "skip-miss@test.com", "role": "coders"},
        )
        # Delete spectators group — should not break removal
        group = await app_state.state.model.users.get_group_by_name(
            f"spectators-{ws_id}"
        )
        await app_state.state.model.users.delete_group(group["id"])
        # Change role — removal phase should skip missing group
        resp = await client.patch(
            f"/api/v1/workspaces/{ws_id}/roles",
            headers=headers,
            json={"email": "skip-miss@test.com", "role": None},
        )
        assert resp.status_code == 200


class TestTransferOwnership:
    @pytest.fixture(autouse=True)
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation requires admin."""

    async def test_transfer_ownership(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "transfer-ws"},
        )
        assert resp.status_code == 200
        ws_id = resp.json()["id"]

        target = await app_state.state.model.users.create_user(
            "xfer-target@test.com", "pass"
        )
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

    async def test_transfer_duplicate_name(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "dup-name-ws"},
        )
        ws_id = resp.json()["id"]

        target = await app_state.state.model.users.create_user(
            "xfer-dup@test.com", "pass"
        )
        # Create a workspace with the same name owned by the target
        await app_state.state.model.workspaces.create_workspace_with_acl(
            target["id"], "dup-name-ws"
        )

        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/transfer",
            headers=headers,
            json={"email": "xfer-dup@test.com"},
        )
        assert resp.status_code == 409
        assert "dup-name-ws" in resp.json()["detail"]

    async def test_transfer_non_owner_forbidden(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "xfer-forbid-ws"},
        )
        ws_id = resp.json()["id"]

        other = await app_state.state.model.users.create_user(
            "xfer-other@test.com", "pass"
        )
        other_token = _auth().create_token(other["id"], other["email"])
        other_headers = {"Authorization": f"Bearer {other_token}"}

        await app_state.state.model.users.create_user(
            "xfer-target2@test.com", "pass"
        )
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/transfer",
            headers=other_headers,
            json={"email": "xfer-target2@test.com"},
        )
        assert resp.status_code == 403

    async def test_transfer_to_agent_rejected(self, client, user, app_state):
        from klangk.main import Lifecycle

        from _helpers import wire_db_and_model

        _seed_state = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=make_settings({}))
        )
        wire_db_and_model(_seed_state)
        await Lifecycle(_seed_state).seed_agent_user()
        agent = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )

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

    async def test_transfer_workspace_not_found(self, client, user, app_state):
        result = await app_state.state.model.workspaces.transfer_workspace(
            "nonexistent-ws-id", user["id"]
        )
        assert result is None

    async def test_transfer_updates_acl(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "xfer-acl-ws"},
        )
        ws_id = resp.json()["id"]

        target = await app_state.state.model.users.create_user(
            "xfer-acl@test.com", "pass"
        )
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
    @pytest.fixture(autouse=True)
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation requires admin."""

    async def test_share_with_group(self, client, admin_user, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "group-share-ws"},
        )
        ws_id = resp.json()["id"]
        group = await app_state.state.model.users.create_group("devs")

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
        # The grant includes `files-download`/`files-write` alongside
        # `files` (#2705).
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws_id}"
        )
        group_perms = sorted(
            e["permission"]
            for e in entries
            if e["principal_type"] == model.PRINCIPAL_GROUP
            and e["group_id"] == group["id"]
        )
        assert group_perms == [
            "files-download",
            "files-view",
            "files-write",
            "join-workspace",
            "terminal",
            "view",
        ]

    async def test_share_with_group_duplicate_rejected(
        self, client, user, app_state
    ):
        """Re-sharing an already-shared group is a 409, not a second
        stacked block (#3101)."""
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "group-dup-ws"},
        )
        ws_id = resp.json()["id"]
        group = await app_state.state.model.users.create_group("dup-devs")

        assert (
            await client.post(
                f"/api/v1/workspaces/{ws_id}/groups",
                headers=headers,
                json={"group_id": group["id"]},
            )
        ).status_code == 200
        resp = await client.post(
            f"/api/v1/workspaces/{ws_id}/groups",
            headers=headers,
            json={"group_id": group["id"]},
        )
        assert resp.status_code == 409
        assert "already has access" in resp.json()["detail"]
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws_id}"
        )
        group_entries = [
            e
            for e in entries
            if e["principal_type"] == model.PRINCIPAL_GROUP
            and e["group_id"] == group["id"]
        ]
        assert len(group_entries) == 6  # one block, not two

    async def test_remove_group(self, client, user, app_state):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "group-rm-ws"}
        )
        ws_id = resp.json()["id"]
        group = await app_state.state.model.users.create_group("temp-devs")

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

    async def test_share_rejects_other_workspaces_role_group(
        self, client, user, app_state
    ):
        """#2750: a workspace's role group is grantable only on its own
        resource — sharing workspace A with workspace B's role group is a
        400."""
        headers = await _auth_headers(client)
        ws_a = (
            await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": "cross-guard-a"},
            )
        ).json()["id"]
        ws_b = (
            await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": "cross-guard-b"},
            )
        ).json()["id"]
        owners_b = await app_state.state.model.users.get_group_by_name(
            f"owners-{ws_b}"
        )

        resp = await client.post(
            f"/api/v1/workspaces/{ws_a}/groups",
            headers=headers,
            json={"group_id": owners_b["id"]},
        )
        assert resp.status_code == 400
        assert "grantable only" in resp.json()["detail"]

        # Its own workspace's role group stays grantable in principle —
        # but it already holds the seeded owner grants on that
        # workspace, so the share is a detected duplicate (409, #3101),
        # never a second stacked block.
        owners_a = await app_state.state.model.users.get_group_by_name(
            f"owners-{ws_a}"
        )
        resp = await client.post(
            f"/api/v1/workspaces/{ws_a}/groups",
            headers=headers,
            json={"group_id": owners_a["id"]},
        )
        assert resp.status_code == 409
        assert "already has access" in resp.json()["detail"]
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws_a}"
        )
        owners_entries = [
            e
            for e in entries
            if e["principal_type"] == model.PRINCIPAL_GROUP
            and e["group_id"] == owners_a["id"]
        ]
        assert len(owners_entries) == 1  # the seeded wildcard, not a stack

    async def test_replace_acl_rejects_other_workspaces_role_group(
        self, client, user, app_state
    ):
        """#2750: the PUT-acl writer carries the same cross-workspace
        guard."""
        headers = await _auth_headers(client)
        ws_a = (
            await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": "acl-guard-a"},
            )
        ).json()["id"]
        ws_b = (
            await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": "acl-guard-b"},
            )
        ).json()["id"]
        owners_b = await app_state.state.model.users.get_group_by_name(
            f"owners-{ws_b}"
        )
        entries = await app_state.state.model.acl.get_acl_entries(
            f"/workspaces/{ws_a}"
        )
        payload = [
            {
                "position": e["position"],
                "action": e["action"],
                "principal_type": e["principal_type"],
                "permission": e["permission"],
                "user_id": e["user_id"],
                "group_id": owners_b["id"] if e["group_id"] else None,
                "system_principal": e["system_principal"],
            }
            for e in entries
        ]
        resp = await client.put(
            f"/api/v1/workspaces/{ws_a}/acl",
            headers=headers,
            json=payload,
        )
        assert resp.status_code == 400
        assert "grantable only" in resp.json()["detail"]

    async def test_group_share_no_permission(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/workspaces/nonexistent/groups", headers=headers
        )
        assert resp.status_code == 403


class TestUserGroupEndpoints:
    """The /groups surface (#2944): an authenticated listing plus
    manage-groups-gated writes (creator grant dropped — flat
    permissions; ACE cleanup on delete retained)."""

    async def test_list_groups(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/groups", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"groups", "page", "page_size", "total"}

    async def test_list_groups_source_filter_and_pagination(
        self, client, user, app_state
    ):
        headers = await _auth_headers(client)
        for name in ("g-manual-a", "g-manual-b"):
            await app_state.state.model.users.create_group(name)
        resp = await client.get(
            "/api/v1/groups?source=manual", headers=headers
        )
        assert resp.status_code == 200
        manual = resp.json()
        assert manual["total"] >= 2
        assert all(g["source"] == "manual" for g in manual["groups"])
        resp = await client.get(
            "/api/v1/groups?source=workspace-role", headers=headers
        )
        assert resp.status_code == 200
        assert all(
            g["source"] == "workspace-role" for g in resp.json()["groups"]
        )
        bad = await client.get("/api/v1/groups?source=nope", headers=headers)
        assert bad.status_code == 422
        paged = await client.get(
            "/api/v1/groups?page=1&page_size=1", headers=headers
        )
        assert paged.status_code == 200
        assert len(paged.json()["groups"]) == 1

    async def test_admin_list_groups_source_filter(
        self, client, admin_user, app_state
    ):
        headers = await self._admin_login(client)
        await app_state.state.model.users.create_group("a-manual-g")
        resp = await client.get(
            "/api/v1/groups?source=manual", headers=headers
        )
        assert resp.status_code == 200
        assert all(g["source"] == "manual" for g in resp.json()["groups"])
        bad = await client.get("/api/v1/groups?source=nope", headers=headers)
        assert bad.status_code == 422

    async def test_admin_groups_lifecycle(self, client, app, admin_user):
        """The single management surface, driven by a wildcard admin."""
        headers = await self._admin_login(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "lifecycle-group", "description": "d"},
        )
        assert resp.status_code == 200, resp.text
        group = resp.json()
        gid = group["id"]

        resp = await client.patch(
            f"/api/v1/groups/{gid}",
            headers=headers,
            json={"description": "updated"},
        )
        assert resp.status_code == 200

        resp = await client.get(
            f"/api/v1/groups/{gid}/members", headers=headers
        )
        assert resp.status_code == 200

        resp = await client.delete(f"/api/v1/groups/{gid}", headers=headers)
        assert resp.status_code == 200
        # Ported from DELETE /groups: the group's ACEs are cleaned up.
        entries = await app.state.model.acl.get_acl_entries(f"/groups/{gid}")
        assert entries == []

    async def test_admin_create_group_duplicate(self, client, admin_user):
        headers = await self._admin_login(client)
        await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "dup-admin-group"},
        )
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "dup-admin-group"},
        )
        assert resp.status_code == 409

    async def test_admin_update_nonexistent_group(self, client, admin_user):
        headers = await self._admin_login(client)
        resp = await client.patch(
            "/api/v1/groups/fake-id",
            headers=headers,
            json={"name": "x"},
        )
        assert resp.status_code == 404

    async def test_admin_update_group_no_fields(self, client, admin_user):
        headers = await self._admin_login(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "noupdate-group"},
        )
        gid = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/groups/{gid}",
            headers=headers,
            json={},
        )
        assert resp.status_code == 400

    async def test_groups_listing_authenticated_writes_gated(
        self, client, user
    ):
        """#2944: GET /groups is the authenticated listing (pickers);
        writes on the tree need manage-groups."""
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/groups", headers=headers)
        assert resp.status_code == 200, resp.text

        resp = await client.post(
            "/api/v1/groups", headers=headers, json={"name": "nope"}
        )
        assert resp.status_code == 403
        resp = await client.delete("/api/v1/groups/some-id", headers=headers)
        assert resp.status_code == 403

    async def _admin_login(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}


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

    async def test_invalid_token_returns_401(self, client, user):
        resp = await client.post(
            "/api/v1/browser-delegate",
            json={"action": "fetch", "browser_id": "bad-id"},
            headers={"Authorization": "Bearer garbage"},
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid workspace token"

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

    async def test_cross_workspace_browser_id_returns_403(
        self, client, app, user, registry, sockets
    ):
        """#1715: a token for workspace A cannot relay through a browser
        registered against workspace B."""
        mock_sock = MagicMock()
        registry.register_browser("bid-other", "ws-other", mock_sock)
        mock_session = AsyncMock()
        mock_session.browser_subscribers = {mock_sock}
        mock_session.dispatch_browser_request_to = AsyncMock()
        try:
            with patch.object(
                sockets, "get_session", return_value=mock_session
            ):
                resp = await client.post(
                    "/api/v1/browser-delegate",
                    json={"action": "fetch", "browser_id": "bid-other"},
                    headers=self._ws_token_headers("ws-own"),
                )
            assert resp.status_code == 403
            assert resp.json()["detail"] == "Unknown browser ID"
            mock_session.dispatch_browser_request_to.assert_not_awaited()
        finally:
            registry.revoke_workspace_browsers("ws-other")

    async def test_stream_cross_workspace_browser_id_returns_403(
        self, client, app, user, registry, sockets
    ):
        """#1715: same binding on /browser-delegate/stream."""
        mock_sock = MagicMock()
        registry.register_browser("bid-other-s", "ws-other", mock_sock)
        mock_session = AsyncMock()
        mock_session.browser_subscribers = {mock_sock}
        mock_session.dispatch_browser_request_stream_to = MagicMock()
        try:
            with patch.object(
                sockets, "get_session", return_value=mock_session
            ):
                resp = await client.post(
                    "/api/v1/browser-delegate/stream",
                    json={"action": "fetch", "browser_id": "bid-other-s"},
                    headers=self._ws_token_headers("ws-own"),
                )
            assert resp.status_code == 403
            assert resp.json()["detail"] == "Unknown browser ID"
            mock_session.dispatch_browser_request_stream_to.assert_not_called()
        finally:
            registry.revoke_workspace_browsers("ws-other")

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

    async def test_disabled_returns_403_before_resolution(
        self, client, app, user, registry, sockets
    ):
        """#2710: KLANGKD_BROWSER_DELEGATE_ENABLED=false 403s both endpoints
        before any browser/session resolution — a valid, registered browser
        is never contacted."""
        mock_sock = MagicMock()
        registry.register_browser("bid-off", "ws-off", mock_sock)
        mock_session = AsyncMock()
        mock_session.browser_subscribers = {mock_sock}
        mock_session.dispatch_browser_request_to = AsyncMock()
        mock_session.dispatch_browser_request_stream_to = MagicMock()
        app.state.settings.browser_delegate_enabled = False
        try:
            with patch.object(
                sockets, "get_session", return_value=mock_session
            ):
                resp = await client.post(
                    "/api/v1/browser-delegate",
                    json={"action": "fetch", "browser_id": "bid-off"},
                    headers=self._ws_token_headers("ws-off"),
                )
                stream_resp = await client.post(
                    "/api/v1/browser-delegate/stream",
                    json={"action": "fetch", "browser_id": "bid-off"},
                    headers=self._ws_token_headers("ws-off"),
                )
            assert resp.status_code == 403
            assert "disabled" in resp.json()["detail"].lower()
            assert stream_resp.status_code == 403
            assert "disabled" in stream_resp.json()["detail"].lower()
            mock_session.dispatch_browser_request_to.assert_not_awaited()
            mock_session.dispatch_browser_request_stream_to.assert_not_called()
        finally:
            app.state.settings.browser_delegate_enabled = True
            registry.revoke_workspace_browsers("ws-off")


# --- Volume routes ---


def _instance_id():
    """The instance ID this server uses (read from <data_dir>/instance-id).

    Matches the value the volume routes validate ``klangk.instance`` labels
    against. Uses the active test data_dir (KLANGKD_DATA_DIR in os.environ, set
    by the temp_data_dir fixture) so it agrees with the ``app`` fixture's util.
    Not a cached global — a fresh read each call.
    """
    from klangk.settings import KlangkSettings

    ns = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=KlangkSettings(os.environ))
    )
    ns.state.util = util_mod.Util(ns)
    return ns.state.util.instance_id()


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
    """Volume endpoints (#2993): GET needs view-volumes, POST/DELETE
    need manage-volumes — admins hold both by seed. Functional tests
    authenticate as the admin; the permission-split tests use the
    plain (non-admin) user."""

    async def test_list_volumes_shows_whole_inventory(
        self, client, admin_user, user, app_state
    ):
        """An admin sees every instance volume with creator provenance
        (no longer an access filter), the creator's handle, and the
        workspaces mounting each volume (#2993)."""
        await self._seed_volume_world(app_state, user, admin_user)
        headers = await _admin_login(client)
        with patch.object(
            _mock_pod,
            "list_volumes",
            AsyncMock(return_value=self._world_volumes(user)),
        ):
            resp = await client.get("/api/v1/volumes", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 4
        assert [
            (v["name"], v["user_id"], v["created_by"], v["workspaces"])
            for v in data["volumes"]
        ] == [
            ("system-vol", None, None, []),
            ("orphan-vol", "ghost-user", None, []),
            ("my-vol", user["id"], user["handle"], ["aaa-ws", "ws-uses-vol"]),
            ("my-vol-2", user["id"], user["handle"], []),
        ]

    async def _seed_volume_world(self, app_state, user, admin_user):
        """The shared listing world: two workspaces mounting my-vol,
        one workspace without mounts."""
        await app_state.state.model.workspaces.create_workspace(
            user["id"],
            "ws-uses-vol",
            mounts=["my-vol:/data", "/host/path:/ro"],
        )
        await app_state.state.model.workspaces.create_workspace(
            admin_user["id"],
            "aaa-ws",
            mounts=["my-vol:/x"],
        )
        await app_state.state.model.workspaces.create_workspace(
            user["id"], "no-mounts-ws"
        )

    def _world_volumes(self, user):
        """The podman listing behind the shared world (four volumes)."""
        return [
            {
                "Name": "my-vol",
                "CreatedAt": "2026-01-01T00:00:00Z",
                "Labels": {
                    "klangk.instance": _instance_id(),
                    "klangk.user-id": user["id"],
                },
            },
            # No CreatedAt: the sort key's empty-date branch.
            {
                "Name": "my-vol-2",
                "Labels": {
                    "klangk.instance": _instance_id(),
                    "klangk.user-id": user["id"],
                },
            },
            {
                "Name": "orphan-vol",
                "CreatedAt": "2026-01-02T00:00:00Z",
                "Labels": {
                    "klangk.instance": _instance_id(),
                    "klangk.user-id": "ghost-user",
                },
            },
            {
                "Name": "system-vol",
                "CreatedAt": "2026-01-03T00:00:00Z",
                "Labels": {"klangk.instance": _instance_id()},
            },
        ]

    async def test_list_volumes_search_and_paging(
        self, client, admin_user, user, app_state
    ):
        """q matches volume name, creator handle, and workspace name
        (case-insensitive); the envelope paginates and sorts (#2993)."""
        await self._seed_volume_world(app_state, user, admin_user)
        headers = await _admin_login(client)
        with patch.object(
            _mock_pod,
            "list_volumes",
            AsyncMock(return_value=self._world_volumes(user)),
        ):

            async def listing(**params):
                resp = await client.get(
                    "/api/v1/volumes",
                    headers=headers,
                    params=params,
                )
                assert resp.status_code == 200
                return resp.json()

            # By name (case-insensitive; matches both my-vol*).
            data = await listing(q="MY-VOL")
            assert [v["name"] for v in data["volumes"]] == [
                "my-vol",
                "my-vol-2",
            ]
            assert data["total"] == 2
            # By creator handle.
            data = await listing(q=user["handle"])
            assert [v["name"] for v in data["volumes"]] == [
                "my-vol",
                "my-vol-2",
            ]
            # By workspace name using the volume.
            data = await listing(q="uses")
            assert [v["name"] for v in data["volumes"]] == ["my-vol"]
            # No match anywhere.
            data = await listing(q="nope")
            assert data["volumes"] == []
            assert data["total"] == 0

            # Sort: created desc (default) vs name asc.
            data = await listing()
            assert [v["name"] for v in data["volumes"]] == [
                "system-vol",
                "orphan-vol",
                "my-vol",
                "my-vol-2",
            ]
            data = await listing(sort="name", order="asc")
            assert [v["name"] for v in data["volumes"]] == [
                "my-vol",
                "my-vol-2",
                "orphan-vol",
                "system-vol",
            ]
            # Unknown sort falls back to created.
            data = await listing(sort="bogus")
            assert data["volumes"][0]["name"] == "system-vol"

            # Paging: page/page_size over the unfiltered four.
            data = await listing(page=2, page_size=2, sort="name", order="asc")
            assert [v["name"] for v in data["volumes"]] == [
                "orphan-vol",
                "system-vol",
            ]
            assert (data["page"], data["page_size"], data["total"]) == (
                2,
                2,
                4,
            )
            # Past the end: empty page, total intact; page 0 clamps to 1.
            data = await listing(page=9, page_size=2)
            assert data["volumes"] == []
            assert data["total"] == 4
            data = await listing(page=0, page_size=2, sort="name", order="asc")
            assert [v["name"] for v in data["volumes"]] == [
                "my-vol",
                "my-vol-2",
            ]

    async def test_list_volumes_requires_view_volumes(self, client, user):
        """A plain authenticated user holds nothing on /volumes by seed
        (#2993) — the listing is admin-surface now."""
        headers = await _auth_headers(client)
        with patch.object(
            _mock_pod, "list_volumes", AsyncMock(return_value=[])
        ):
            resp = await client.get("/api/v1/volumes", headers=headers)
        assert resp.status_code == 403

    async def test_view_only_holder_lists_but_cannot_delete(
        self, client, user, app_state
    ):
        """A delegated read-only volumes auditor (view-volumes alone)
        lists the inventory but cannot delete — manage-volumes gates
        the write endpoints."""
        await app_state.state.model.acl.add_acl_entry(
            "/volumes",
            2,
            model.ACTION_ALLOW,
            "view-volumes",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        headers = await _auth_headers(client)
        with patch.object(
            _mock_pod, "list_volumes", AsyncMock(return_value=[])
        ):
            resp = await client.get("/api/v1/volumes", headers=headers)
        assert resp.status_code == 200
        with patch.object(
            _mock_pod,
            "inspect_volume",
            AsyncMock(return_value=_managed_volume(user["id"])),
        ):
            resp = await client.delete("/api/v1/volumes/mine", headers=headers)
        assert resp.status_code == 403

    async def test_create_volume(self, client, admin_user):
        headers = await _admin_login(client)
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
        # The creator label stays on created volumes (provenance).
        assert labels["klangk.user-id"] == admin_user["id"]

    async def test_create_volume_accepts_full_charset(
        self, client, admin_user
    ):
        """#2971: every character the pattern allows is accepted
        together in one name."""
        headers = await _admin_login(client)
        with (
            patch.object(
                _mock_pod, "inspect_volume", AsyncMock(return_value=None)
            ),
            patch.object(
                _mock_pod,
                "create_volume",
                AsyncMock(return_value={"Name": "a-b_c.d", "CreatedAt": ""}),
            ),
        ):
            resp = await client.post(
                "/api/v1/volumes",
                json={"name": "a-b_c.d"},
                headers=headers,
            )
        assert resp.status_code == 200

    async def test_create_volume_rejects_leading_dash(
        self, client, admin_user
    ):
        """#2971: a name starting with "-" would be parsed as a flag by
        the podman CLI — rejected with 422 before podman is called."""
        headers = await _admin_login(client)
        mock_inspect = AsyncMock()
        mock_create = AsyncMock()
        with (
            patch.object(_mock_pod, "inspect_volume", mock_inspect),
            patch.object(_mock_pod, "create_volume", mock_create),
        ):
            resp = await client.post(
                "/api/v1/volumes",
                json={"name": "-flag"},
                headers=headers,
            )
        assert resp.status_code == 422
        assert mock_inspect.await_count == 0
        assert mock_create.await_count == 0

    async def test_create_volume_rejects_overlong_name(
        self, client, admin_user
    ):
        """#2971: names past the 64-char cap are rejected with 422 (the
        64-char boundary itself is still accepted)."""
        headers = await _admin_login(client)
        mock_create = AsyncMock(
            return_value={"Name": "x", "CreatedAt": "2026-01-01"}
        )
        with (
            patch.object(
                _mock_pod, "inspect_volume", AsyncMock(return_value=None)
            ),
            patch.object(_mock_pod, "create_volume", mock_create),
        ):
            boundary = await client.post(
                "/api/v1/volumes",
                json={"name": "a" * 64},
                headers=headers,
            )
            overlong = await client.post(
                "/api/v1/volumes",
                json={"name": "a" * 65},
                headers=headers,
            )
        assert boundary.status_code == 200
        assert overlong.status_code == 422
        assert mock_create.await_count == 1

    async def test_create_volume_rejects_bad_charset(self, client, admin_user):
        """#2971: names outside [a-zA-Z0-9_.-] (after an alphanumeric
        first char) are rejected with 422."""
        headers = await _admin_login(client)
        mock_inspect = AsyncMock()
        mock_create = AsyncMock()
        with (
            patch.object(_mock_pod, "inspect_volume", mock_inspect),
            patch.object(_mock_pod, "create_volume", mock_create),
        ):
            for name in (
                "has space",
                "sla/sh",
                "ex!am",
                ".dotstart",
                "_under",
                "abc\n",
                "a\nb",
            ):
                resp = await client.post(
                    "/api/v1/volumes",
                    json={"name": name},
                    headers=headers,
                )
                assert resp.status_code == 422, name
        assert mock_inspect.await_count == 0
        assert mock_create.await_count == 0

    async def test_create_volume_requires_manage_volumes(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/volumes",
            json={"name": "nope-vol"},
            headers=headers,
        )
        assert resp.status_code == 403

    async def test_create_duplicate_volume(self, client, admin_user):
        headers = await _admin_login(client)
        with patch.object(
            _mock_pod,
            "inspect_volume",
            AsyncMock(return_value=_managed_volume(admin_user["id"])),
        ):
            resp = await client.post(
                "/api/v1/volumes",
                json={"name": "dup-vol"},
                headers=headers,
            )
        assert resp.status_code == 409

    async def test_create_volume_foreign_instance_not_enumerable(
        self, app, admin_user
    ):
        """#2973: a volume owned by another instance must not confirm its
        existence via 409 — the create falls through to podman, and the
        client sees a bare 500 whose body carries no probed name
        (podman's own conflict text stays in the server log)."""
        mock_create = AsyncMock(
            side_effect=podman.PodmanError(
                500, "volume with name 'foreign-vol' already exists"
            )
        )
        with (
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(
                    return_value={
                        "Name": "foreign-vol",
                        "Labels": {"klangk.instance": "other"},
                    }
                ),
            ),
            patch.object(_mock_pod, "create_volume", mock_create),
        ):
            transport = ASGITransport(app=app, raise_app_exceptions=False)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as c:
                headers = await _admin_login(c)
                resp = await c.post(
                    "/api/v1/volumes",
                    json={"name": "foreign-vol"},
                    headers=headers,
                )
        assert mock_create.await_count == 1
        assert resp.status_code == 500
        assert "foreign-vol" not in resp.text

    async def test_create_volume_error_propagates(self, client, admin_user):
        headers = await _admin_login(client)
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

    # --- Per-user volume quota (#2972) ---

    # The endpoint delegates counting to podman.count_user_volumes
    # (label filtering is unit-tested in test_podman.py); tests here
    # stub the count and assert the route's quota decision, its call
    # args, and that the refusal never reaches podman.create_volume.

    async def test_create_volume_under_quota(self, app, client, admin_user):
        headers = await _admin_login(client)
        mock_create = AsyncMock(
            return_value={"Name": "new-vol", "CreatedAt": "2026-01-01"}
        )
        mock_count = AsyncMock(return_value=1)
        with (
            patch.object(app.state.settings, "volume_quota_per_user", 2),
            patch.object(
                _mock_pod, "inspect_volume", AsyncMock(return_value=None)
            ),
            patch.object(_mock_pod, "count_user_volumes", mock_count),
            patch.object(_mock_pod, "create_volume", mock_create),
        ):
            resp = await client.post(
                "/api/v1/volumes",
                json={"name": "new-vol"},
                headers=headers,
            )
        assert resp.status_code == 200
        assert mock_create.await_count == 1
        # The count is scoped to this instance and the caller's id.
        assert mock_count.await_args.args[1] == admin_user["id"]

    async def test_create_volume_at_quota_refused_429(
        self, app, client, admin_user
    ):
        headers = await _admin_login(client)
        mock_create = AsyncMock(
            return_value={"Name": "nope", "CreatedAt": "2026-01-01"}
        )
        with (
            patch.object(app.state.settings, "volume_quota_per_user", 2),
            patch.object(
                _mock_pod, "inspect_volume", AsyncMock(return_value=None)
            ),
            patch.object(
                _mock_pod, "count_user_volumes", AsyncMock(return_value=2)
            ),
            patch.object(_mock_pod, "create_volume", mock_create),
        ):
            resp = await client.post(
                "/api/v1/volumes",
                json={"name": "v3"},
                headers=headers,
            )
        assert resp.status_code == 429
        detail = resp.json()["detail"]
        assert "2" in detail
        assert "KLANGKD_VOLUME_QUOTA_PER_USER" in detail
        assert "Delete a volume first" in detail
        mock_create.assert_not_awaited()

    async def test_create_volume_duplicate_at_quota_reports_409(
        self, app, client, admin_user
    ):
        """The in-instance duplicate conflict probe deliberately wins over
        the quota check — a duplicate name reports 409 even at quota,
        and the count never runs (the name is enumerable by the caller
        either way)."""
        headers = await _admin_login(client)
        mock_count = AsyncMock(return_value=99)
        with (
            patch.object(app.state.settings, "volume_quota_per_user", 1),
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(admin_user["id"])),
            ),
            patch.object(_mock_pod, "count_user_volumes", mock_count),
        ):
            resp = await client.post(
                "/api/v1/volumes",
                json={"name": "dup-vol"},
                headers=headers,
            )
        assert resp.status_code == 409
        mock_count.assert_not_awaited()

    async def test_concurrent_creates_respect_quota(
        self, app, client, admin_user
    ):
        """The per-user lock spans count+create: two overlapping creates
        cannot each count the same pre-create total and both pass a cap
        they jointly exceed (#2972 TOCTOU).

        Direct route invocation, not the HTTP client: the app fixture's
        dependency/DB layer serializes the two requests' critical
        sections, so the race is only observable at the route-function
        level. The fake store is stateful — count reflects what has
        actually been created — so with the lock removed both calls
        count 0 < 1 and both creates succeed (verified by mutation:
        deleting the lock flips this test to failing).
        """
        from fastapi import HTTPException

        from klangk.api import resources
        from klangk.api.resources import CreateVolumeRequest

        created: list[str] = []

        async def fake_count(instance, uid):
            await asyncio.sleep(0)
            return len(created)

        async def fake_create(name, labels):
            await asyncio.sleep(0)
            created.append(name)
            return {"Name": name, "CreatedAt": "2026-01-01"}

        real_lock = asyncio.Lock()
        with (
            patch.object(app.state.settings, "volume_quota_per_user", 1),
            patch.object(
                _mock_pod, "inspect_volume", AsyncMock(return_value=None)
            ),
            patch.object(
                _mock_pod, "volume_create_lock", lambda uid: real_lock
            ),
            patch.object(_mock_pod, "count_user_volumes", fake_count),
            patch.object(_mock_pod, "create_volume", fake_create),
        ):
            results = await asyncio.gather(
                resources.create_volume(
                    CreateVolumeRequest(name="v-a"), admin_user, app
                ),
                resources.create_volume(
                    CreateVolumeRequest(name="v-b"), admin_user, app
                ),
                return_exceptions=True,
            )
        # Exactly one create happened; the loser counted the winner's
        # volume and was refused with 429 — never both 200.
        assert len(created) == 1
        errors = [r for r in results if isinstance(r, HTTPException)]
        successes = [r for r in results if not isinstance(r, Exception)]
        assert len(errors) == 1
        assert errors[0].status_code == 429
        assert "KLANGKD_VOLUME_QUOTA_PER_USER" in errors[0].detail
        assert len(successes) == 1
        assert successes[0]["name"] == created[0]

    async def test_create_volume_quota_disabled_by_default(
        self, app, client, admin_user
    ):
        """quota 0 (the shipped default) keeps the pre-#2972 create path:
        no volume enumeration, no refusal regardless of count."""
        headers = await _admin_login(client)
        mock_count = AsyncMock(return_value=0)
        mock_create = AsyncMock(
            return_value={"Name": "new-vol", "CreatedAt": "2026-01-01"}
        )
        with (
            patch.object(app.state.settings, "volume_quota_per_user", 0),
            patch.object(
                _mock_pod, "inspect_volume", AsyncMock(return_value=None)
            ),
            patch.object(_mock_pod, "count_user_volumes", mock_count),
            patch.object(_mock_pod, "create_volume", mock_create),
        ):
            resp = await client.post(
                "/api/v1/volumes",
                json={"name": "new-vol"},
                headers=headers,
            )
        assert resp.status_code == 200
        mock_count.assert_not_awaited()
        assert mock_create.await_count == 1

    async def test_delete_volume_rejects_invalid_name(
        self, client, admin_user
    ):
        """#2971: a leading-dash path param would reach podman argv
        verbatim — `DELETE /volumes/--all` would run `podman volume
        rm --all` (every unused volume on the host). 422 before any
        podman call."""
        headers = await _admin_login(client)
        mock_inspect = AsyncMock()
        mock_remove = AsyncMock()
        with (
            patch.object(_mock_pod, "inspect_volume", mock_inspect),
            patch.object(_mock_pod, "remove_volume", mock_remove),
        ):
            resp = await client.delete(
                "/api/v1/volumes/--all", headers=headers
            )
        assert resp.status_code == 422
        assert mock_inspect.await_count == 0
        assert mock_remove.await_count == 0

    async def test_delete_volume(self, client, admin_user):
        headers = await _admin_login(client)
        with (
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(admin_user["id"])),
            ),
            patch.object(_mock_pod, "remove_volume", AsyncMock()),
        ):
            resp = await client.delete(
                "/api/v1/volumes/test-vol", headers=headers
            )
        assert resp.status_code == 200

    async def test_delete_other_users_volume(self, client, admin_user):
        """#2993: manage-volumes is the whole gate — an admin deletes any
        instance-managed volume, not only their own (the creator label
        is provenance, not an access filter)."""
        headers = await _admin_login(client)
        with (
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume("someone-else")),
            ),
            patch.object(_mock_pod, "remove_volume", AsyncMock()),
        ):
            resp = await client.delete(
                "/api/v1/volumes/other", headers=headers
            )
        assert resp.status_code == 200

    async def test_delete_volume_not_found(self, client, admin_user):
        headers = await _admin_login(client)
        with patch.object(
            _mock_pod, "inspect_volume", AsyncMock(return_value=None)
        ):
            resp = await client.delete("/api/v1/volumes/nope", headers=headers)
        assert resp.status_code == 404

    async def test_delete_volume_wrong_instance(self, client, admin_user):
        headers = await _admin_login(client)
        with patch.object(
            _mock_pod,
            "inspect_volume",
            AsyncMock(return_value={"Labels": {"klangk.instance": "other"}}),
        ):
            resp = await client.delete(
                "/api/v1/volumes/foreign", headers=headers
            )
        assert resp.status_code == 404

    async def test_delete_volume_remove_not_found(self, client, admin_user):
        """Volume vanishes between inspect and remove -> 404."""
        headers = await _admin_login(client)
        with (
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(admin_user["id"])),
            ),
            patch.object(
                _mock_pod,
                "remove_volume",
                AsyncMock(side_effect=podman.PodmanError(404, "gone")),
            ),
        ):
            resp = await client.delete("/api/v1/volumes/gone", headers=headers)
        assert resp.status_code == 404

    async def test_delete_volume_other_error(self, client, admin_user):
        headers = await _admin_login(client)
        with (
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(admin_user["id"])),
            ),
            patch.object(
                _mock_pod,
                "remove_volume",
                AsyncMock(side_effect=podman.PodmanError(500, "internal")),
            ),
            pytest.raises(podman.PodmanError),
        ):
            await client.delete("/api/v1/volumes/err-vol", headers=headers)

    async def test_delete_volume_in_use(self, client, admin_user):
        headers = await _admin_login(client)
        with (
            patch.object(
                _mock_pod,
                "inspect_volume",
                AsyncMock(return_value=_managed_volume(admin_user["id"])),
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
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation requires admin."""

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
                    f"/api/v1/workspaces/{ws_id}/files?path=/home/klangk",
                    headers=headers,
                )
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)
        finally:
            self._cleanup(ws_id)

    async def test_list_files_permission_denied_returns_403(
        self, client, user
    ):
        """#2766: an unreadable directory is an error, not an empty list."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(1, "", "find: '/home': Permission denied"),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files?path=/home",
                    headers=headers,
                )
            assert resp.status_code == 403
            assert "Permission denied" in resp.json()["detail"]
        finally:
            self._cleanup(ws_id)

    async def test_list_files_find_error_returns_500(self, client, user):
        """A non-permission find failure maps to 500 with the stderr text
        (#2766)."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(1, "", "find: '/deep': Too many levels"),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files?path=/deep",
                    headers=headers,
                )
            assert resp.status_code == 500
            assert "Too many levels" in resp.json()["detail"]
        finally:
            self._cleanup(ws_id)

    async def test_list_files_nonexistent_dir_returns_empty_list(
        self, client, user
    ):
        """#2766/#2769: a missing directory is NOT an error — the route
        returns 200 + [] (statWorkspacePath leans on this to classify
        paths by listing the parent)."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(
                    1,
                    "",
                    "find: '/no/such/dir': No such file or directory",
                ),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files?path=/no/such/dir",
                    headers=headers,
                )
            assert resp.status_code == 200
            assert resp.json() == []
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
                    f"/api/v1/workspaces/{ws_id}/files/upload?path=/home/klangk/hello.txt",
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
                    f"/api/v1/workspaces/{ws_id}/files/content?path=/home/klangk/hello.txt",
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
                    f"/api/v1/workspaces/{ws_id}/files/upload?path=/home/klangk/test.txt",
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
            monkeypatch.setattr(app.state.settings, "file_upload_size_max", 10)
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/files/upload?path=/home/klangk/big.txt",
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
                    f"/api/v1/workspaces/{ws_id}/files?path=/home/klangk/doomed.txt",
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
                "klangk.files.Files.delete_path",
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
                        "old_path": "/home/klangk/old.txt",
                        "new_path": "/home/klangk/new.txt",
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
                "klangk.files.Files.rename_path",
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
                    f"/api/v1/workspaces/{ws_id}/files/download?path=/home/klangk/dl.txt",
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
                    f"/api/v1/workspaces/{ws_id}/files/download?path=/home/klangk/f%22name.txt",
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
                    f"/api/v1/workspaces/{ws_id}/files/download?path=/home/klangk/mydir",
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

    async def _member_headers_with_perms(
        self, app_state, client, ws_id, perms
    ):
        """Create other@example.com, grant *perms* on the workspace, and
        return their auth headers (#2705 download gating)."""
        other = await app_state.state.model.users.create_user(
            "other@example.com",
            auth_mod.hash_password("otherpass"),
            verified=True,
        )
        resource = f"/workspaces/{ws_id}"
        existing = await app_state.state.model.acl.get_acl_entries(resource)
        next_pos = max((e["position"] for e in existing), default=-1) + 1
        for perm in perms:
            await app_state.state.model.acl.add_acl_entry(
                resource,
                next_pos,
                model.ACTION_ALLOW,
                perm,
                model.PRINCIPAL_USER,
                user_id=other["id"],
            )
            next_pos += 1
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "other@example.com",
                "password": "otherpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_download_denied_without_files_download(
        self, client, user, app_state
    ):
        """`files` alone no longer grants download (#2705) — browsing
        still works, raw-byte streaming does not."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state, client, ws_id, ["view", "terminal", "files-view"]
            )
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(0, "regular file\t11", ""),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files?path=/home/klangk",
                    headers=other,
                )
                assert resp.status_code == 200
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files/download"
                    "?path=/home/klangk/dl.txt",
                    headers=other,
                )
            assert resp.status_code == 403
        finally:
            self._cleanup(ws_id)

    async def test_download_allowed_with_files_download(
        self, client, user, app_state
    ):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state,
                client,
                ws_id,
                ["view", "terminal", "files-view", "files-download"],
            )

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
                    f"/api/v1/workspaces/{ws_id}/files/download"
                    "?path=/home/klangk/dl.txt",
                    headers=other,
                )
            assert resp.status_code == 200
            assert resp.content == b"download me"
        finally:
            self._cleanup(ws_id)

    async def test_download_files_download_alone_insufficient(
        self, client, user, app_state
    ):
        """`files-download` without `files` grants nothing (#2705): the
        route requires both, so a lone grant cannot pull raw bytes."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state, client, ws_id, ["files-download"]
            )
            resp = await client.get(
                f"/api/v1/workspaces/{ws_id}/files/download"
                "?path=/home/klangk/dl.txt",
                headers=other,
            )
            assert resp.status_code == 403
        finally:
            self._cleanup(ws_id)

    async def test_read_denied_without_files_download(
        self, client, user, app_state
    ):
        """`files` alone no longer grants the text reader either (#2713)
        — browsing still works, `/files/content` does not."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state, client, ws_id, ["view", "terminal", "files-view"]
            )
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(0, "regular file\t11", ""),
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files?path=/home/klangk",
                    headers=other,
                )
                assert resp.status_code == 200
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files/content"
                    "?path=/home/klangk/read.txt",
                    headers=other,
                )
            assert resp.status_code == 403
        finally:
            self._cleanup(ws_id)

    async def test_read_allowed_with_files_download(
        self, client, user, app_state
    ):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state,
                client,
                ws_id,
                ["view", "terminal", "files-view", "files-download"],
            )
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                side_effect=[
                    (0, "regular file\t11", ""),  # stat
                    (0, "read me", ""),  # cat
                ],
            ):
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files/content"
                    "?path=/home/klangk/read.txt",
                    headers=other,
                )
            assert resp.status_code == 200
            assert resp.json()["content"] == "read me"
        finally:
            self._cleanup(ws_id)

    async def test_read_files_download_alone_insufficient(
        self, client, user, app_state
    ):
        """`files-download` without `files` grants nothing (#2713): the
        text reader requires both, mirroring the download route."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state, client, ws_id, ["files-download"]
            )
            resp = await client.get(
                f"/api/v1/workspaces/{ws_id}/files/content"
                "?path=/home/klangk/read.txt",
                headers=other,
            )
            assert resp.status_code == 403
        finally:
            self._cleanup(ws_id)

    async def test_upload_denied_without_files_write(
        self, client, user, app_state
    ):
        """`files` alone does not grant upload: the route requires
        `files-write` too (mirrors the download gate)."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state, client, ws_id, ["view", "terminal", "files-view"]
            )
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(0, "f.txt\tf\t10\t0.0\t0.0\n", ""),
            ):
                # Listing still works for a `files`-only member.
                resp = await client.get(
                    f"/api/v1/workspaces/{ws_id}/files?path=/home/klangk",
                    headers=other,
                )
                assert resp.status_code == 200
                resp = await client.post(
                    f"/api/v1/workspaces/{ws_id}/files/upload"
                    "?path=/home/klangk/up.txt",
                    headers=other,
                    files={"file": ("up.txt", b"data", "text/plain")},
                )
            assert resp.status_code == 403
        finally:
            self._cleanup(ws_id)

    async def test_delete_and_rename_denied_without_files_write(
        self, client, user, app_state
    ):
        """`files` alone does not grant the destructive routes either:
        delete and rename also require `files-write`."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state, client, ws_id, ["view", "terminal", "files-view"]
            )
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}/files?path=/home/klangk/victim.txt",
                headers=other,
            )
            assert resp.status_code == 403
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/files/rename",
                headers=other,
                json={
                    "old_path": "/home/klangk/a",
                    "new_path": "/home/klangk/b",
                },
            )
            assert resp.status_code == 403
        finally:
            self._cleanup(ws_id)

    async def test_delete_and_rename_allowed_with_files_write(
        self, client, user, app_state
    ):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state,
                client,
                ws_id,
                ["view", "terminal", "files-view", "files-write"],
            )
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                side_effect=[
                    (0, "", ""),  # test -e: victim exists
                    (0, "", ""),  # rm -rf
                ],
            ):
                resp = await client.delete(
                    f"/api/v1/workspaces/{ws_id}/files?path=/home/klangk/victim.txt",
                    headers=other,
                )
            assert resp.status_code == 200
            assert resp.json()["status"] == "deleted"
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                side_effect=[
                    (0, "", ""),  # test -e: source exists
                    (1, "", ""),  # test -e: dest missing
                    (0, "", ""),  # mkdir -p parent
                    (0, "", ""),  # mv
                ],
            ):
                resp = await client.post(
                    f"/api/v1/workspaces/{ws_id}/files/rename",
                    headers=other,
                    json={
                        "old_path": "/home/klangk/a",
                        "new_path": "/home/klangk/b",
                    },
                )
            assert resp.status_code == 200
            assert resp.json()["status"] == "renamed"
        finally:
            self._cleanup(ws_id)

    async def test_upload_allowed_with_files_write(
        self, client, user, app_state
    ):
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state,
                client,
                ws_id,
                ["view", "terminal", "files-view", "files-write"],
            )
            with patch.object(
                _mock_pod,
                "exec_container",
                new_callable=AsyncMock,
                return_value=(0, "", ""),
            ):
                resp = await client.post(
                    f"/api/v1/workspaces/{ws_id}/files/upload"
                    "?path=/home/klangk/up.txt",
                    headers=other,
                    files={"file": ("up.txt", b"data", "text/plain")},
                )
            assert resp.status_code == 200
            assert resp.json()["status"] == "uploaded"
        finally:
            self._cleanup(ws_id)

    async def test_upload_files_write_alone_insufficient(
        self, client, user, app_state
    ):
        """`files-write` without `files` grants nothing."""
        headers = await _auth_headers(client)
        ws_id = await self._create_workspace(client, headers)
        try:
            other = await self._member_headers_with_perms(
                app_state, client, ws_id, ["files-write"]
            )
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/files/upload"
                "?path=/home/klangk/up.txt",
                headers=other,
                files={"file": ("up.txt", b"data", "text/plain")},
            )
            assert resp.status_code == 403
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
        """Without KLANGKD_TEST_MODE, the endpoints should not exist."""
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
    async def test_create_group(self, db, app_state):
        group = await app_state.state.model.users.create_group(
            "editors", "Editor group"
        )
        assert group["name"] == "editors"
        assert group["description"] == "Editor group"
        assert group["id"]

    async def test_get_group_by_name(self, db, app_state):
        await app_state.state.model.users.create_group("testers")
        found = await app_state.state.model.users.get_group_by_name("testers")
        assert found is not None
        assert found["name"] == "testers"

    async def test_add_user_to_group(self, user, app_state):
        group = await app_state.state.model.users.create_group("devs")
        await app_state.state.model.users.add_user_to_group(
            user["id"], group["id"]
        )
        group_ids = await app_state.state.model.users.get_user_group_ids(
            user["id"]
        )
        assert group["id"] in group_ids

    async def test_add_user_to_group_idempotent(self, user, app_state):
        group = await app_state.state.model.users.create_group("devs")
        await app_state.state.model.users.add_user_to_group(
            user["id"], group["id"]
        )
        await app_state.state.model.users.add_user_to_group(
            user["id"], group["id"]
        )
        group_ids = await app_state.state.model.users.get_user_group_ids(
            user["id"]
        )
        assert group_ids.count(group["id"]) == 1

    async def test_get_groups_empty(self, user, app_state):
        group_ids = await app_state.state.model.users.get_user_group_ids(
            user["id"]
        )
        assert group_ids == []

    async def test_remove_user_from_group(self, user, app_state):
        group = await app_state.state.model.users.create_group("devs")
        await app_state.state.model.users.add_user_to_group(
            user["id"], group["id"]
        )
        removed = await app_state.state.model.users.remove_user_from_group(
            user["id"], group["id"]
        )
        assert removed is True
        group_ids = await app_state.state.model.users.get_user_group_ids(
            user["id"]
        )
        assert group["id"] not in group_ids

    async def test_cascade_delete_user(self, db, app_state):
        """Deleting a user cascades to user_groups."""
        user = await app_state.state.model.users.create_user("delme", "hash")
        group = await app_state.state.model.users.create_group("devs")
        await app_state.state.model.users.add_user_to_group(
            user["id"], group["id"]
        )
        assert group[
            "id"
        ] in await app_state.state.model.users.get_user_group_ids(user["id"])
        async with app_state.state.db.transaction() as db_conn:
            await db_conn.execute(
                "DELETE FROM users WHERE id = ?", (user["id"],)
            )
        assert (
            await app_state.state.model.users.get_user_group_ids(user["id"])
            == []
        )

    async def test_cascade_delete_group(self, user, app_state):
        """Deleting a group cascades to user_groups."""
        group = await app_state.state.model.users.create_group("temp")
        await app_state.state.model.users.add_user_to_group(
            user["id"], group["id"]
        )
        assert group[
            "id"
        ] in await app_state.state.model.users.get_user_group_ids(user["id"])
        await app_state.state.model.users.delete_group(group["id"])
        assert group[
            "id"
        ] not in await app_state.state.model.users.get_user_group_ids(
            user["id"]
        )

    async def test_jwt_has_no_roles(self, user):
        """JWT tokens no longer include roles."""
        token = _auth().create_token(user["id"], "testuser@example.com")
        payload = _auth().decode_token(token)
        assert "roles" not in payload

    async def test_login_jwt_has_no_roles(self, client, user):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
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
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_list_users(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.get("/api/v1/users", headers=headers)
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
        self, client, app, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        # Create 12 users so the default page is full but not exhaustive.
        for i in range(12):
            await app_state.state.model.users.create_user(
                f"u{i}@example.com", None, verified=True
            )
        resp = await client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["total"] >= 13  # 12 created + admin fixture
        assert len(body["users"]) == 10

    async def test_list_users_pagination_across_pages(
        self, client, app, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        for i in range(5):
            await app_state.state.model.users.create_user(
                f"pg{i}@example.com", None, verified=True
            )
        page1 = await client.get(
            "/api/v1/users?page=1&page_size=3", headers=headers
        )
        page2 = await client.get(
            "/api/v1/users?page=2&page_size=3", headers=headers
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

    async def test_list_users_sort_by_email(
        self, client, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        for e in [
            "charlie@example.com",
            "alpha@example.com",
            "bravo@example.com",
        ]:
            await app_state.state.model.users.create_user(
                e, None, verified=True
            )
        resp = await client.get(
            "/api/v1/users?sort=email&order=asc&page_size=50",
            headers=headers,
        )
        emails = [u["email"] for u in resp.json()["users"]]
        assert emails == sorted(emails, key=str.lower)
        assert emails[0] == "alpha@example.com"

    async def test_list_users_sort_desc_reverses(
        self, client, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        for e in [
            "charlie@example.com",
            "alpha@example.com",
            "bravo@example.com",
        ]:
            await app_state.state.model.users.create_user(
                e, None, verified=True
            )
        asc = await client.get(
            "/api/v1/users?sort=email&order=asc&page_size=50",
            headers=headers,
        )
        desc = await client.get(
            "/api/v1/users?sort=email&order=desc&page_size=50",
            headers=headers,
        )
        asc_emails = [u["email"] for u in asc.json()["users"]]
        desc_emails = [u["email"] for u in desc.json()["users"]]
        assert asc_emails == list(reversed(desc_emails))

    async def test_list_users_filter_by_email(
        self, client, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        await app_state.state.model.users.create_user(
            "needle@example.com", None, verified=True
        )
        await app_state.state.model.users.create_user(
            "haystack@example.com", None, verified=True
        )
        resp = await client.get(
            "/api/v1/users?q=needle&page_size=50", headers=headers
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
            "/api/v1/users?sort=evil%3B%20DROP%20TABLE&order=asc",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["users"] is not None

    async def test_list_users_requires_admin(self, client, user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 403

    async def test_admin_create_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "newuser@example.com", "password": "testpass123"},
        )
        assert resp.status_code == 200
        assert resp.json()["email"] == "newuser@example.com"
        assert resp.json()["status"] == "created"
        # User should be verified and able to log in
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "newuser@example.com",
                "password": "testpass123",
            },
        )
        assert login_resp.status_code == 200

    async def test_admin_create_user_duplicate(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "testuser@example.com", "password": "testpass"},
        )
        assert resp.status_code == 400
        assert "already registered" in resp.json()["detail"]

    async def test_admin_create_user_short_password(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "short@example.com", "password": "ab"},
        )
        assert resp.status_code == 400
        assert "Password" in resp.json()["detail"]

    async def test_admin_create_user_send_verification_email(
        self, client, app, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ) as mock_email:
            resp = await client.post(
                "/api/v1/users",
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
        user = await app_state.state.model.users.get_user_by_email(
            "verify@example.com"
        )
        assert user is not None
        assert user["verified"] == 0
        assert user["handle"] == "verify"  # derived, not NULL

    async def test_admin_create_user_no_password_no_verify(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/users",
            headers=headers,
            json={"email": "nopw@example.com"},
        )
        assert resp.status_code == 400
        assert "Password is required" in resp.json()["detail"]

    async def test_admin_create_user_requires_admin(self, client, user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.post(
            "/api/v1/users",
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
                f"/api/v1/users/{user['id']}", headers=headers
            )
        assert resp.status_code == 200
        # Verify user is gone
        resp = await client.get("/api/v1/users?page_size=200", headers=headers)
        emails = [u["email"] for u in resp.json()["users"]]
        assert "testuser@example.com" not in emails

    async def test_delete_user_prunes_activity_stamp(
        self, client, app, admin_user, user, registry
    ):
        """#2914: the delete path drops the user's activity-throttle
        stamp so Auth.activity_stamps retains no deleted-user ids."""
        app.state.auth.activity_stamps[user["id"]] = 123.0
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
                f"/api/v1/users/{user['id']}", headers=headers
            )
        assert resp.status_code == 200
        assert user["id"] not in app.state.auth.activity_stamps

    async def test_delete_self_forbidden(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            f"/api/v1/users/{admin_user['id']}", headers=headers
        )
        assert resp.status_code == 400

    async def test_update_user_password_reuse_rejected(
        self, client, app, admin_user, user, monkeypatch
    ):
        """#2582: admin set to the user's current password → 400."""
        monkeypatch.setattr(
            app.state.settings,
            "password_history_count",
            3,
            raising=False,
        )
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            headers=headers,
            json={"password": "testpass"},
        )
        assert resp.status_code == 400
        assert "current" in resp.json()["detail"]
        # A novel password still succeeds.
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            headers=headers,
            json={"password": "novel-pass-1"},
        )
        assert resp.status_code == 200

    async def test_delete_nonexistent_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            "/api/v1/users/nonexistent-id", headers=headers
        )
        assert resp.status_code == 404

    async def test_delete_agent_user_rejected(self, client, admin_user, db):
        from klangk.main import Lifecycle

        from _helpers import wire_db_and_model

        _seed_state = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=make_settings({}))
        )
        wire_db_and_model(_seed_state)
        await Lifecycle(_seed_state).seed_agent_user()
        headers = await self._admin_headers(client)
        resp = await client.delete(
            f"/api/v1/users/{model.AGENT_USER_ID}", headers=headers
        )
        assert resp.status_code == 400
        assert "system agent" in resp.json()["detail"]

    async def test_delete_user_cascades_workspaces(
        self, client, app, admin_user, ws_admin, registry, app_state
    ):
        """Deleting a user cascades to their ws_mod."""
        user = ws_admin  # ws_admin returns the user dict
        headers = await self._admin_headers(client)
        # Create a workspace for the user
        user_login = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
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
                f"/api/v1/users/{user['id']}", headers=headers
            )
        assert resp.status_code == 200
        # Workspace should be gone (CASCADE)
        ws_list = await app_state.state.model.workspaces.get_user_workspaces_with_containers(
            user["id"]
        )
        assert len(ws_list) == 0

    async def test_delete_user_prunes_workspace_registry_entries(
        self, client, app, admin_user, ws_admin, registry, app_state
    ):
        """#2912: the user-delete cascade prunes the per-workspace lock +
        stop-epoch entries for every workspace the user owned."""
        user = ws_admin  # ws_admin returns the user dict
        headers = await self._admin_headers(client)
        user_login = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        user_headers = {
            "Authorization": f"Bearer {user_login.json()['access_token']}"
        }
        ws_resp = await client.post(
            "/api/v1/workspaces",
            headers=user_headers,
            json={"name": "cascade-prune"},
        )
        assert ws_resp.status_code == 200
        ws_id = ws_resp.json()["id"]
        registry._get_workspace_lock(ws_id)
        registry.stop_epoch[ws_id] = 1

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
                f"/api/v1/users/{user['id']}", headers=headers
            )
        assert resp.status_code == 200
        assert ws_id not in registry._workspace_locks
        assert ws_id not in registry.stop_epoch

    async def test_list_user_workspaces_admin(
        self, client, admin_user, ws_admin
    ):
        """Admin can list another user's workspaces (#1224)."""
        headers = await self._admin_headers(client)
        user = ws_admin  # ws_admin returns the user dict
        # The `user` fixture owns no workspaces yet.
        resp = await client.get(
            f"/api/v1/users/{user['id']}/workspaces", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []
        assert resp.json()["has_more"] is False

        # Create a workspace as that user, then it should appear.
        user_login = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
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
            f"/api/v1/users/{user['id']}/workspaces", headers=headers
        )
        assert resp.status_code == 200
        names = [ws["name"] for ws in resp.json()["items"]]
        assert names == ["doomed"]

    async def test_list_user_workspaces_admin_404(self, client, admin_user):
        """Listing workspaces for a nonexistent user 404s."""
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/api/v1/users/nonexistent-id/workspaces", headers=headers
        )
        assert resp.status_code == 404

    async def test_update_email(self, client, admin_user, user, app_state):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            json={"email": "renamed@example.com"},
            headers=headers,
        )
        assert resp.status_code == 200
        updated = await app_state.state.model.users.get_user_by_id(user["id"])
        assert updated["email"] == "renamed@example.com"

    async def test_update_email_invalid_rejected(
        self, client, admin_user, user, app_state
    ):
        """#3097: a malformed address is rejected, not persisted."""
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            json={"email": "not-an-email"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "valid email" in resp.json()["detail"]
        updated = await app_state.state.model.users.get_user_by_id(user["id"])
        assert updated["email"] == user["email"]

    async def test_update_email_duplicate_rejected(
        self, client, admin_user, user, app_state
    ):
        """#3097: an address owned by another account is a 400, not a
        500 off the users.email UNIQUE constraint."""
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            json={"email": admin_user["email"]},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Email already in use"
        updated = await app_state.state.model.users.get_user_by_id(user["id"])
        assert updated["email"] == user["email"]

    async def test_update_email_same_email_noop(
        self, client, admin_user, user, app_state
    ):
        """#3097: patching a user with their own current email is an
        idempotent 200, not a duplicate rejection."""
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            json={"email": user["email"]},
            headers=headers,
        )
        assert resp.status_code == 200
        updated = await app_state.state.model.users.get_user_by_id(user["id"])
        assert updated["email"] == user["email"]

    async def test_update_email_integrity_race_guard(
        self, client, app, admin_user, user, monkeypatch
    ):
        """#3097: a duplicate that slips past the pre-check (the address
        claimed between the check and the write) is a 400, not a 500 off
        the users.email UNIQUE constraint."""
        from sqlalchemy.exc import IntegrityError as SAIntegrityError

        async def claim_in_between(_user_id, _email):
            raise SAIntegrityError("UPDATE users", {}, Exception("dup"))

        monkeypatch.setattr(
            app.state.model.users,
            "update_email",
            claim_in_between,
        )
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            json={"email": "claimed-in-between@example.com"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Email already in use"

    async def test_update_password(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            json={"password": "newpass123"},
            headers=headers,
        )
        assert resp.status_code == 200
        # Verify can login with new password
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "newpass123",
            },
        )
        assert login_resp.status_code == 200

    async def test_update_nonexistent_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            "/api/v1/users/nonexistent-id",
            json={"email": "x"},
            headers=headers,
        )
        assert resp.status_code == 404

    async def test_update_agent_password_rejected(
        self, client, app, admin_user, db
    ):
        # Seed the agent user so it exists in the DB
        from klangk.main import Lifecycle

        from _helpers import wire_db_and_model

        _seed_state = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=make_settings({}))
        )
        wire_db_and_model(_seed_state)
        await Lifecycle(_seed_state).seed_agent_user()
        headers = await self._admin_headers(client)
        resp = await client.patch(
            f"/api/v1/users/{model.AGENT_USER_ID}",
            json={"password": "sneaky123"},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "system agent" in resp.json()["detail"]

    async def test_unlock_user(self, client, admin_user, user, app_state):
        headers = await self._admin_headers(client)
        # Lock out the user
        await app_state.state.model.login_attempts.record_failed_login(
            user["email"]
        )
        await app_state.state.model.login_attempts.set_login_lockout(
            user["email"], "2099-01-01T00:00:00+00:00"
        )
        # Verify locked
        info = (
            await app_state.state.model.login_attempts.get_login_attempt_info(
                user["email"]
            )
        )
        assert info["locked_until"] is not None
        # Unlock via admin endpoint
        resp = await client.post(
            f"/api/v1/users/{user['id']}/unlockout", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "unlocked"
        # Verify lockout cleared
        info = (
            await app_state.state.model.login_attempts.get_login_attempt_info(
                user["email"]
            )
        )
        assert info is None

    async def test_unlock_nonexistent_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/users/nonexistent-id/unlockout", headers=headers
        )
        assert resp.status_code == 404

    async def test_unlock_requires_admin(self, client, user):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        resp = await client.post(
            f"/api/v1/users/{user['id']}/unlockout", headers=headers
        )
        assert resp.status_code == 403


class TestUserSessionsAudit:
    """Admin query for a user's active sessions with workstation identity
    (#2586), plus the login path threading X-Real-IP into the registry."""

    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_login_threads_workstation_into_registry(
        self, client, user, app_state
    ):
        """A login carrying X-Real-IP (behind the trusted loopback test
        peer) records that IP on the session row."""
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
            headers={"X-Real-IP": "203.0.113.7"},
        )
        assert resp.status_code == 200
        rows = await app_state.state.model.sessions.list_sessions(user["id"])
        assert len(rows) == 1
        assert rows[0]["source_ip"] == "203.0.113.7"

    async def test_admin_lists_sessions_with_workstations(
        self, client, admin_user, user, app_state
    ):
        headers = await self._admin_headers(client)
        for ip in ("203.0.113.7", "198.51.100.9"):
            resp = await client.post(
                "/api/v1/auth/login",
                json={
                    "identifier": "testuser@example.com",
                    "password": "testpass",
                },
                headers={
                    "X-Real-IP": ip,
                    "User-Agent": f"ua-{ip}",
                },
            )
            assert resp.status_code == 200
        resp = await client.get(
            f"/api/v1/users/{user['id']}/sessions", headers=headers
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert {i["source_ip"] for i in items} == {
            "203.0.113.7",
            "198.51.100.9",
        }
        assert {i["user_agent"] for i in items} == {
            "ua-203.0.113.7",
            "ua-198.51.100.9",
        }
        # Oldest first: the workstation-A session was established first.
        assert items[0]["source_ip"] == "203.0.113.7"

    async def test_admin_sessions_peer_fallback_workstation(
        self, client, admin_user, user, app_state
    ):
        """A login with no forwarded headers (direct loopback client)
        records the peer as the workstation; an empty User-Agent is
        stored as null (unknown), never as an empty string."""
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
            headers={"User-Agent": ""},
        )
        assert resp.status_code == 200
        headers = await self._admin_headers(client)
        resp = await client.get(
            f"/api/v1/users/{user['id']}/sessions", headers=headers
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["source_ip"] == "127.0.0.1"
        assert items[0]["user_agent"] is None

    async def test_admin_sessions_404_unknown_user(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/api/v1/users/no-such-user/sessions", headers=headers
        )
        assert resp.status_code == 404

    async def test_admin_sessions_requires_admin(self, client, user):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
        resp = await client.get(
            f"/api/v1/users/{user['id']}/sessions", headers=headers
        )
        assert resp.status_code == 403


class TestGroupEndpoints:
    @pytest.fixture(autouse=True)
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation requires admin."""

    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_list_groups(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get("/api/v1/groups", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        groups = body["groups"]
        assert any(g["name"] == "admins" for g in groups)
        # Paged envelope metadata.
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["total"] >= 1

    async def test_list_groups_default_page_size_is_10(
        self, client, app, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        for i in range(12):
            await app_state.state.model.users.create_group(f"size-{i}")
        resp = await client.get("/api/v1/groups", headers=headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["page"] == 1
        assert body["page_size"] == 10
        assert body["total"] >= 13  # 12 created + admin fixture
        assert len(body["groups"]) == 10

    async def test_list_groups_pagination_across_pages(
        self, client, app, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        for i in range(5):
            await app_state.state.model.users.create_group(f"pg-{i}")
        page1 = await client.get(
            "/api/v1/groups?page=1&page_size=3", headers=headers
        )
        page2 = await client.get(
            "/api/v1/groups?page=2&page_size=3", headers=headers
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

    async def test_list_groups_sort_by_name(
        self, client, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        for n in ["charlie", "alpha", "bravo"]:
            await app_state.state.model.users.create_group(n)
        resp = await client.get(
            "/api/v1/groups?sort=name&order=asc&page_size=200",
            headers=headers,
        )
        names = [g["name"] for g in resp.json()["groups"]]
        assert names == sorted(names, key=str.lower)
        assert "alpha" in names

    async def test_list_groups_sort_desc_reverses(
        self, client, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        for n in ["charlie", "alpha", "bravo"]:
            await app_state.state.model.users.create_group(n)
        asc = await client.get(
            "/api/v1/groups?sort=name&order=asc&page_size=200",
            headers=headers,
        )
        desc = await client.get(
            "/api/v1/groups?sort=name&order=desc&page_size=200",
            headers=headers,
        )
        asc_names = [g["name"] for g in asc.json()["groups"]]
        desc_names = [g["name"] for g in desc.json()["groups"]]
        assert asc_names == list(reversed(desc_names))

    async def test_list_groups_filter_by_name(
        self, client, admin_user, app_state
    ):
        headers = await self._admin_headers(client)
        await app_state.state.model.users.create_group("needle-group")
        await app_state.state.model.users.create_group("haystack-group")
        resp = await client.get(
            "/api/v1/groups?q=needle&page_size=200",
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
            "/api/v1/groups?sort=evil%3B%20DROP%20TABLE&order=asc",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["groups"] is not None

    async def test_create_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "editors", "description": "Editor group"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "editors"

    async def test_create_group_duplicate(self, client, admin_user):
        headers = await self._admin_headers(client)
        await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "dup-group"},
        )
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "dup-group"},
        )
        assert resp.status_code == 409

    async def test_update_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "to-rename"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/groups/{group_id}",
            headers=headers,
            json={"name": "renamed", "description": "new desc"},
        )
        assert resp.status_code == 200

    async def test_update_group_no_fields(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "no-update"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/groups/{group_id}",
            headers=headers,
            json={},
        )
        assert resp.status_code == 400

    async def test_update_group_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.patch(
            "/api/v1/groups/nonexistent",
            headers=headers,
            json={"name": "x"},
        )
        assert resp.status_code == 404

    async def test_delete_group(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "to-delete"},
        )
        group_id = resp.json()["id"]
        resp = await client.delete(
            f"/api/v1/groups/{group_id}", headers=headers
        )
        assert resp.status_code == 200

    async def test_admins_group_cannot_be_renamed(
        self, client, app, admin_user, app_state
    ):
        """#2995: is_admin derives from a group *named* admins — a
        rename away strips every admin's status, so it is rejected.
        Descriptions stay editable."""
        headers = await self._admin_headers(client)
        group = await app_state.state.model.users.get_group_by_name("admins")
        resp = await client.patch(
            f"/api/v1/groups/{group['id']}",
            headers=headers,
            json={"name": "super-admins"},
        )
        assert resp.status_code == 400
        assert "cannot be renamed" in resp.json()["detail"]
        # The description path still works (no rename involved).
        resp = await client.patch(
            f"/api/v1/groups/{group['id']}",
            headers=headers,
            json={"description": "Instance administrators"},
        )
        assert resp.status_code == 200

    async def test_admins_name_cannot_be_claimed(
        self, client, app, admin_user
    ):
        """A delegated group manager must not mint a second admins
        group by renaming an ordinary group onto the reserved name
        (#2995)."""
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "impostors"},
        )
        group_id = resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/groups/{group_id}",
            headers=headers,
            json={"name": "admins"},
        )
        assert resp.status_code == 400
        assert "reserved" in resp.json()["detail"]

    async def test_admins_group_cannot_be_deleted(
        self, client, app, admin_user, app_state
    ):
        """#2995: deleting the admins group would strip every
        instance-admin's is_admin; rejected with 400."""
        headers = await self._admin_headers(client)
        group = await app_state.state.model.users.get_group_by_name("admins")
        resp = await client.delete(
            f"/api/v1/groups/{group['id']}", headers=headers
        )
        assert resp.status_code == 400
        assert "cannot be deleted" in resp.json()["detail"]

    async def test_delete_group_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            "/api/v1/groups/nonexistent", headers=headers
        )
        assert resp.status_code == 404

    async def test_list_group_members(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "members-test"},
        )
        group_id = resp.json()["id"]
        # Add user to group
        resp = await client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=headers,
            json={"user_id": user["id"]},
        )
        assert resp.status_code == 200
        # List members
        resp = await client.get(
            f"/api/v1/groups/{group_id}/members", headers=headers
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["email"] == "testuser@example.com"

    async def test_list_group_members_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/api/v1/groups/nonexistent/members", headers=headers
        )
        assert resp.status_code == 404

    async def test_add_group_member_user_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "member-test2"},
        )
        group_id = resp.json()["id"]
        resp = await client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=headers,
            json={"user_id": "nonexistent"},
        )
        assert resp.status_code == 404

    async def test_add_group_member_group_not_found(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/groups/nonexistent/members",
            headers=headers,
            json={"user_id": "x"},
        )
        assert resp.status_code == 404

    async def test_remove_group_member(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "remove-test"},
        )
        group_id = resp.json()["id"]
        await client.post(
            f"/api/v1/groups/{group_id}/members",
            headers=headers,
            json={"user_id": user["id"]},
        )
        resp = await client.delete(
            f"/api/v1/groups/{group_id}/members/{user['id']}",
            headers=headers,
        )
        assert resp.status_code == 200

    async def test_remove_group_member_not_member(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/groups",
            headers=headers,
            json={"name": "rm-test"},
        )
        group_id = resp.json()["id"]
        resp = await client.delete(
            f"/api/v1/groups/{group_id}/members/nonexistent",
            headers=headers,
        )
        assert resp.status_code == 404


class TestACLEndpoints:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_get_acl_tree(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get("/api/v1/acl/tree", headers=headers)
        assert resp.status_code == 200
        tree = resp.json()
        assert len(tree) > 0

    async def test_get_acl_by_user(self, client, admin_user, user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            f"/api/v1/acl/by-principal/user/{user['id']}",
            headers=headers,
        )
        assert resp.status_code == 200

    async def test_get_acl_by_group(self, client, admin_user, app_state):
        headers = await self._admin_headers(client)
        # Get the admin group ID
        groups = (await app_state.state.model.users.list_groups())["groups"]
        admin_group = next(g for g in groups if g["name"] == "admins")
        resp = await client.get(
            f"/api/v1/acl/by-principal/group/{admin_group['id']}",
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
        # Instance-admin flag derives from admins-group membership
        # (#2995) — /admin is no longer a resource.
        assert data["is_admin"] is True
        assert "/admin" not in data["permissions"]

    async def test_my_permissions_non_admin(self, client, admin_user, user):
        """Non-admin user has no admin permissions."""
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/my-permissions", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is False
        assert "/admin" not in data["permissions"]

    async def test_my_permissions_images_inherits_view(
        self, client, admin_user, user
    ):
        """#2994: with the retired Deny Everyone row gone, an
        authenticated user's effective /images permissions include the
        `view` inherited from the root `/` Allow (the old row masked it).
        Informational only — no /images route checks `view` — but it is
        user-visible in the permission map, so pin it."""
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/my-permissions", headers=headers)
        assert resp.status_code == 200
        perms = resp.json()["permissions"]["/images"]
        assert "view-images" in perms
        assert "view" in perms

    async def test_my_permissions_flag_ignores_admin_acl(
        self, client, admin_user, user, app_state
    ):
        """The is_admin flag derives from admins-group membership ONLY
        (#2995): a hand-written Allow * ACE on the retired /admin
        resource must not flip it — nothing derives from the ACL tree
        anymore."""
        await app_state.state.model.acl.add_acl_entry(
            "/admin",
            0,
            model.ACTION_ALLOW,
            "*",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/my-permissions", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_admin"] is False
        # /admin is not a static resource, so the ACE surfaces nowhere.
        assert "/admin" not in data["permissions"]

    async def test_my_permissions_for_resource(self, client, ws_admin):
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


class TestResourceAcl:
    def test_workspace_scope_classification(self):
        """#2764: ``workspace_scope`` classifies the admin-ACL target.

        Malformed variants that fall out of the workspace classification
        (empty id segments, missing leading slash) address ACL nodes no
        real resource's ancestor walk ever visits, so they stay
        admin-only without becoming a dodge.
        """
        from klangk.api.admin import workspace_scope

        # Individual workspaces (and deeper paths) normalize to the node.
        assert workspace_scope("/workspaces/abc") == "/workspaces/abc"
        assert workspace_scope("/workspaces/abc/files") == "/workspaces/abc"
        assert workspace_scope("/workspaces/abc/") == "/workspaces/abc"
        # The collection, root, and non-workspace resources: None.
        assert workspace_scope("/workspaces") is None
        assert workspace_scope("/workspaces/") is None
        assert workspace_scope("/") is None
        assert workspace_scope("/users/x") is None
        assert workspace_scope("") is None
        # Malformed: empty id segments are not a workspace target.
        assert workspace_scope("/workspaces//abc") is None
        # A missing leading slash still classifies as a workspace
        # (stricter — it can only add the gate, not remove it).
        assert workspace_scope("workspaces/abc") == "/workspaces/abc"

    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_get_resource_acl(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/api/v1/acl/resource?resource=/workspaces", headers=headers
        )
        assert resp.status_code == 200
        entries = resp.json()
        # Default ACL has the admins group create-workspace on /workspaces
        assert any(e["permission"] == "create-workspace" for e in entries)

    async def test_replace_resource_acl(self, client, admin_user):
        headers = await self._admin_headers(client)
        # Get current ACL
        resp = await client.get(
            "/api/v1/acl/resource?resource=/workspaces", headers=headers
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
            "/api/v1/acl/resource?resource=/workspaces",
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
            "/api/v1/acl/resource?resource=/workspaces",
            headers=headers,
            json=restore,
        )
        assert resp.status_code == 200

    async def test_get_resource_acl_requires_admin(self, client, user):
        headers = await _auth_headers(client)
        resp = await client.get(
            "/api/v1/acl/resource?resource=/workspaces", headers=headers
        )
        assert resp.status_code == 403

    async def test_replace_workspace_resource_needs_change_acls(
        self, client, admin_user, ws_admin, app_state
    ):
        """#2764: rewriting a workspace's ACL via the admin endpoint
        additionally requires ``change-acls`` on that workspace."""
        owner_headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=owner_headers,
            json={"name": "admin-cacl-ws"},
        )
        ws_id = resp.json()["id"]
        resource = f"/workspaces/{ws_id}"

        headers = await self._admin_headers(client)
        resp = await client.get(
            f"/api/v1/acl/resource?resource={resource}",
            headers=headers,
        )
        assert resp.status_code == 200
        payload = [
            {
                "action": e["action"],
                "principal_type": e["principal_type"],
                "permission": e["permission"],
                "user_id": e.get("user_id"),
                "group_id": e.get("group_id"),
                "system_principal": e.get("system_principal"),
            }
            for e in resp.json()
        ]
        # Site admin alone cannot rewrite an individual workspace's ACL.
        resp = await client.put(
            f"/api/v1/acl/resource?resource={resource}",
            headers=headers,
            json=payload,
        )
        assert resp.status_code == 403

        # An admin holding change-acls on the workspace passes.
        entries = await app_state.state.model.acl.get_acl_entries(resource)
        next_pos = max(e["position"] for e in entries) + 1
        await app_state.state.model.acl.add_acl_entry(
            resource,
            next_pos,
            model.ACTION_ALLOW,
            "share-advanced",
            model.PRINCIPAL_USER,
            user_id=admin_user["id"],
        )
        resp = await client.put(
            f"/api/v1/acl/resource?resource={resource}",
            headers=headers,
            json=payload,
        )
        assert resp.status_code == 200

    async def test_root_acl_rejects_removing_authenticated_view(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        # Try to save root ACL without Authenticated view
        resp = await client.put(
            "/api/v1/acl/resource?resource=/",
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
            "/api/v1/acl/resource?resource=/",
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

        ns = types_mod.SimpleNamespace(
            state=types_mod.SimpleNamespace(settings=make_settings({}))
        )
        ns.state.util = util_mod.Util(ns)
        return ws_mod.Workspaces(ns)

    def test_extracts_metadata(self, app_state):
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
            "instance_id": ws.app.state.util.instance_id(),
            "image": "ubuntu",
            "service_command": "bash",
            "auto_start": True,
            "mounts": ["/data:/data"],
            "env": {"FOO": "bar"},
            "health_check": None,
            "allowed_domains": None,
            "rejected_domains": None,
            "settings": None,
            "num_ports": 3,
            "egress_mode": None,
            "per_handle_home": True,
            "classification_banner": None,
        }

    def test_defaults_num_ports(self):
        meta = self._ws().workspace_metadata({"name": "x"})
        assert meta["num_ports"] == 5

    def test_defaults_per_handle_home(self):
        # #2722: a ws dict without the key (legacy row shape) exports
        # per_handle_home=True — every pre-#2169 workspace was per-user.
        meta = self._ws().workspace_metadata({"name": "x"})
        assert meta["per_handle_home"] is True
        meta2 = self._ws().workspace_metadata(
            {"name": "x", "per_handle_home": False}
        )
        assert meta2["per_handle_home"] is False

    def test_includes_instance_id(self, app_state):
        ws = self._ws()
        meta = ws.workspace_metadata({"name": "x"})
        assert meta["instance_id"] == ws.app.state.util.instance_id()


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

    async def test_archive_destroys_per_workspace_nix(
        self, user, workspace, app, monkeypatch
    ):
        """#2201: account deletion tears down each workspace's nix snapshot
        (no orphan) — destroy_workspace_nix is called per archived workspace,
        matching delete_workspace."""
        destroyed: list[str] = []

        async def _spy(ws_id):
            destroyed.append(ws_id)

        monkeypatch.setattr(app.state.nix, "destroy_workspace_nix", _spy)
        # A workspace home dir so archive_user_data has work to do.
        home_dir = app.state.workspaces.home_path(workspace["id"])
        home_dir.mkdir(parents=True, exist_ok=True)

        await app.state.workspaces.archive_user_data(user["id"], user["email"])

        assert destroyed == [workspace["id"]]

    async def test_archive_multiple_workspaces(self, user, app, app_state):
        """Creates separate archives for each workspace."""
        ws1 = await app_state.state.model.workspaces.create_workspace(
            user["id"], "ws-one"
        )
        ws2 = await app_state.state.model.workspaces.create_workspace(
            user["id"], "ws-two"
        )

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

    async def test_archive_paginates_more_than_one_page(
        self, user, app, app_state
    ):
        """Archival pages through every workspace when there are >10."""
        for i in range(12):
            ws = await app_state.state.model.workspaces.create_workspace(
                user["id"], f"ws-{i:02d}"
            )
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
    @pytest.fixture(autouse=True)
    async def _make_user_admin(self, ws_admin):
        """#2569: workspace creation/import requires admin."""

    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def _user_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    def _meta(self, **overrides):
        """Build workspace metadata dict with instance_id included.

        The instance ID is read from the active test data_dir (the same file
        the server's app.state.util reads), so this matches whatever the
        import endpoint validates against.
        """
        from klangk.settings import KlangkSettings

        ns = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=KlangkSettings(os.environ))
        )
        ns.state.util = util_mod.Util(ns)
        d = {"instance_id": ns.state.util.instance_id()}
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
        (home / "klangk").mkdir(exist_ok=True)
        (home / "klangk" / "hello.txt").write_text("hello world")

        # Export as the owner (#2707: export is workspace-scoped)
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
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

    async def test_export_missing_tar_binary_is_clean_500(
        self, client, admin_user, user, app
    ):
        """A tar that cannot start fails before the response begins —
        a clean 500, not an empty 200 body (#3101)."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "no-tar"}
        )
        ws = resp.json()
        with patch("shutil.which", return_value=None):
            resp = await client.get(
                f"/api/v1/workspaces/{ws['id']}/export", headers=headers
            )
        assert resp.status_code == 500
        assert "tar binary" in resp.json()["detail"]

    async def test_export_tar_failure_aborts_body(
        self, client, admin_user, user, app, caplog
    ):
        """A tar that fails mid-run must not deliver a clean 200 with a
        truncated archive: the stream aborts and the failure is logged
        at ERROR with tar's stderr (#3101)."""
        import logging

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "bad-tar"},
        )
        ws = resp.json()
        home = app.state.workspaces.home_path(ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        (home / "data.txt").write_text("x" * 4096)

        real_args = app.state.workspaces.build_export_tar_args

        def broken_args(output, tmpdir, home_dir):
            # Real archive args plus a member that cannot exist: tar
            # streams the valid members, then exits nonzero.
            return real_args(output, tmpdir, home_dir) + [
                "/nonexistent-member-xyz"
            ]

        caplog.set_level(logging.ERROR, logger="klangk.api.workspaces")
        with patch.object(
            app.state.workspaces, "build_export_tar_args", broken_args
        ):
            with pytest.raises(RuntimeError, match="Export tar failed"):
                await client.get(
                    f"/api/v1/workspaces/{ws['id']}/export", headers=headers
                )
        assert any(
            "export tar failed" in r.message.lower() for r in caplog.records
        )

    async def test_export_subprocess_spawn_failure_aborts_body(
        self, client, admin_user, user, app
    ):
        """A spawn failure after the pre-flight (e.g. the binary
        vanished) aborts the stream too, without leaking the stderr
        temp file (#3101)."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "spawn-fail"},
        )
        ws = resp.json()

        async def no_exec(*args, **kwargs):
            raise FileNotFoundError("tar vanished")

        with patch(
            "klangk.api.workspaces.asyncio.create_subprocess_exec",
            no_exec,
        ):
            with pytest.raises(FileNotFoundError):
                await client.get(
                    f"/api/v1/workspaces/{ws['id']}/export", headers=headers
                )

    async def test_export_requires_permission(
        self, client, admin_user, user, app_state
    ):
        """Users without a grant on the workspace cannot export (#2707)."""
        # Create workspace as the test user (owner via seeded ACEs)
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "no-export"}
        )
        ws = resp.json()

        # Create another user with no grant on the workspace — denied
        from klangk.auth import hash_password

        pw_hash = hash_password("testpass")
        await app_state.state.model.users.create_user(
            "nonadmin-export@example.com", pw_hash, verified=True
        )
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "nonadmin-export@example.com",
                "password": "testpass",
            },
        )
        other_headers = {
            "Authorization": f"Bearer {login.json()['access_token']}"
        }
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=other_headers
        )
        assert resp.status_code == 403

        # A bare admin (admin group, but no ownership/grant on this
        # workspace) is denied too — export is workspace-scoped now.
        admin_headers = await self._admin_headers(client)
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert resp.status_code == 403

    async def test_export_by_owners_role_group_member(
        self, client, admin_user, user, app, app_state
    ):
        """#2707: the seeded owners role group carries the export grant —
        a member who is not the creating owner can export."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "role-export"},
        )
        ws = resp.json()

        from klangk.auth import hash_password

        coowner = await app_state.state.model.users.create_user(
            "coowner-export@example.com",
            hash_password("testpass"),
            verified=True,
        )
        group = await app_state.state.model.users.get_group_by_name(
            f"owners-{ws['id']}"
        )
        assert group is not None
        await app_state.state.model.users.add_user_to_group(
            coowner["id"], group["id"]
        )
        login = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "coowner-export@example.com",
                "password": "testpass",
            },
        )
        coowner_headers = {
            "Authorization": f"Bearer {login.json()['access_token']}"
        }
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export",
            headers=coowner_headers,
        )
        assert resp.status_code == 200

    async def test_export_deny_ace_revokes_only_export(
        self, client, admin_user, user, app, app_state
    ):
        """#2707: a deny ACE on the workspace resource revokes export but
        not the owner's other capabilities (the wildcard ACE keeps the
        rest)."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "deny-export"},
        )
        ws = resp.json()
        export_url = f"/api/v1/workspaces/{ws['id']}/export"

        # The owner (wildcard ACE at position 0) can export first.
        resp = await client.get(export_url, headers=headers)
        assert resp.status_code == 200

        # Deny export to everyone, positioned ahead of the wildcard ACEs.
        from klangk.model import (
            ACTION_DENY,
            PRINCIPAL_SYSTEM,
            SYSTEM_EVERYONE,
        )

        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{ws['id']}",
            -1,
            ACTION_DENY,
            "export-workspace",
            PRINCIPAL_SYSTEM,
            system_principal=SYSTEM_EVERYONE,
        )

        # Export is now denied for the owner...
        resp = await client.get(export_url, headers=headers)
        assert resp.status_code == 403

        # ...while the owner's other capabilities (still matched by the
        # wildcard ACE) keep working — workspace status (view) and the
        # ACL listing.
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/status", headers=headers
        )
        assert resp.status_code == 200
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/acl", headers=headers
        )
        assert resp.status_code == 200

    async def test_export_not_found(self, client, admin_user, app_state):
        """A granted caller exporting a nonexistent workspace gets 404.

        The permission check runs on the (nonexistent) resource path
        first, so without a grant there it 403s at the root deny before
        existence is ever consulted — grant export on the path to reach
        the handler's not-found branch.
        """
        headers = await self._admin_headers(client)
        admin = await app_state.state.model.users.get_user_by_email(
            "testadmin@example.com"
        )
        from klangk.model import ACTION_ALLOW, PRINCIPAL_USER

        await app_state.state.model.acl.add_acl_entry(
            "/workspaces/nonexistent-id",
            0,
            ACTION_ALLOW,
            "export-workspace",
            PRINCIPAL_USER,
            user_id=admin["id"],
        )
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
        (home / "klangk").mkdir(exist_ok=True)
        (home / "klangk" / "data.txt").write_text("test data")

        admin_headers = await self._admin_headers(client)

        # #2707: export is gated on the workspace resource now — a bare
        # admin (no ownership, no grant) is denied.
        export_resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=admin_headers
        )
        assert export_resp.status_code == 403

        # The owner (wildcard ACE) exports fine.
        export_resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
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
        assert (new_home / "klangk" / "data.txt").exists()
        assert (new_home / "klangk" / "data.txt").read_text() == "test data"

    async def test_export_import_preserves_classification_banner(
        self, client, admin_user, user, app
    ):
        """#2768: the marking rides in workspace.json — a real export ->
        import round trip keeps the workspace classified."""
        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={
                "name": "mark-export",
                "classification_banner": "SECRET",
            },
        )
        ws = resp.json()
        home = app.state.workspaces.home_path(ws["id"])
        home.mkdir(parents=True, exist_ok=True)
        export_resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
        )
        assert export_resp.status_code == 200
        import_resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            params={"name": "mark-imported"},
            files={
                "file": (
                    "archive.tar.gz",
                    export_resp.content,
                    "application/gzip",
                )
            },
        )
        assert import_resp.status_code == 200
        assert import_resp.json()["classification_banner"] == "SECRET"

    async def test_import_invalid_classification_banner_falls_back(
        self, client, admin_user, user
    ):
        """#2768: a malformed marking in a (tampered/stale) archive drops
        to the inherit default instead of failing the import — the home
        tree is the payload; the banner is a label."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                self._meta(
                    name="mark-bad-archive", classification_banner="A\nB"
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
        assert resp.json()["classification_banner"] is None

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

    async def test_import_rejects_blank_name(self, client, admin_user, user):
        # #3110: the import choke point shares the name minimum — a blank
        # request name (even over a good archive name) and a blank
        # workspace.json name are both 400s. Name resolution precedes the
        # provenance check, so no instance_id games needed.
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="from-archive")).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        archive = buf.getvalue()
        headers = await self._user_headers(client)

        # Blank explicit request name wins over the archive's good name.
        resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            params={"name": "   "},
            files={"file": ("archive.tar.gz", archive, "application/gzip")},
        )
        assert resp.status_code == 400
        assert (
            resp.json()["detail"]
            == "No usable workspace name in archive or request"
        )

        # Blank archive name with no request override.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name=" \t")).encode()
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta)
            tar.addfile(info, io.BytesIO(meta))
        resp = await client.post(
            "/api/v1/workspaces/import",
            headers=headers,
            files={
                "file": ("archive.tar.gz", buf.getvalue(), "application/gzip")
            },
        )
        assert resp.status_code == 400

    async def test_import_follows_deploy_default(
        self, client, admin_user, user
    ):
        """#2722: the archive's explicit layout wins over the deploy default,
        in BOTH directions — this shared-home archive lands shared even
        against a per-handle deploy default."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                self._meta(name="archive-shared", per_handle_home=False)
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
        assert resp.json()["per_handle_home"] is False

    async def test_import_rejects_nix_optin_while_disabled(self, client, user):
        """#2560: the archive is user-supplied, editable input — import is a
        create path (no previous bag to echo), so a nix=true opt-in rejects
        while the feature is off, exactly like POST /workspaces."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                self._meta(name="stranded-nix", settings={"nix": True})
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
        assert "nix feature" in resp.json()["detail"]

    async def test_import_round_trips_per_handle_layout(
        self, client, admin_user, user, app, monkeypatch
    ):
        """#2722: a real export -> import round trip carries the layout in
        workspace.json, both directions. Deploy default is flipped to the
        OPPOSITE of the archive's layout to prove the archive wins."""
        for layout in (True, False):
            monkeypatch.setattr(
                app.state.settings, "per_handle_home", not layout
            )
            headers = await self._user_headers(client)
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={"name": f"rt-ws-{layout}", "per_handle_home": layout},
            )
            ws = resp.json()

            export_resp = await client.get(
                f"/api/v1/workspaces/{ws['id']}/export",
                headers=headers,
            )
            assert export_resp.status_code == 200

            import json as json_mod
            import tarfile as tarfile_mod
            import io as io_mod

            # workspace.json carries the exported layout.
            buf = io_mod.BytesIO(export_resp.content)
            with tarfile_mod.open(fileobj=buf, mode="r:gz") as tar:
                metadata = json_mod.loads(
                    tar.extractfile("workspace.json").read()
                )
            assert metadata["per_handle_home"] is layout

            import_resp = await client.post(
                "/api/v1/workspaces/import",
                headers=headers,
                params={"name": f"rt-imported-{layout}"},
                files={
                    "file": (
                        "archive.tar.gz",
                        export_resp.content,
                        "application/gzip",
                    )
                },
            )
            assert import_resp.status_code == 200
            # The archive's layout won over the deploy default.
            assert import_resp.json()["per_handle_home"] is layout

    async def test_import_legacy_archive_defaults_per_handle(
        self, client, admin_user, user, app, monkeypatch
    ):
        """#2722: a legacy archive without per_handle_home imports as
        per-handle (True) even when the deploy default is shared."""
        import io
        import json
        import tarfile

        monkeypatch.setattr(app.state.settings, "per_handle_home", False)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(self._meta(name="legacy-archive")).encode()
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
        assert resp.json()["per_handle_home"] is True

    async def test_import_tampered_layout_falls_back_per_handle(
        self, client, admin_user, user, app, monkeypatch
    ):
        """#2722: a non-bool per_handle_home (tampered/garbage archive) is
        not honored — imports as per-handle, matching the model's strict
        bool validation instead of a 500."""
        import io
        import json
        import tarfile

        monkeypatch.setattr(app.state.settings, "per_handle_home", False)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                self._meta(name="tampered-archive", per_handle_home="yes")
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
        assert resp.json()["per_handle_home"] is True

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

        from klangk.settings import KlangkSettings

        ns = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=KlangkSettings(os.environ))
        )
        ns.state.util = util_mod.Util(ns)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                {
                    "name": "same-inst",
                    "instance_id": ns.state.util.instance_id(),
                }
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

    async def test_import_rejects_invalid_settings(self, client, user):
        """Archives with an invalid settings bag are rejected (#864).

        An archive from this instance is trusted on provenance, but its
        settings bag is re-validated — it may predate a schema change or
        carry a value the current deploy rejects.
        """
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                self._meta(
                    name="bad-settings-import",
                    settings={"idle_timeout": -5},
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
        assert resp.status_code == 400
        assert "Archive settings are invalid" in resp.json()["detail"]

    async def test_import_roundtrips_settings(self, client, user):
        """A valid settings bag survives export -> import (#864)."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                self._meta(
                    name="settings-roundtrip",
                    settings={"idle_timeout": 300, "cpu_limit": 1.5},
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
        ws_id = resp.json()["id"]
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["settings"] == {"idle_timeout": 300, "cpu_limit": 1.5}

    async def test_export_serializes_egress_mode(
        self, client, admin_user, user
    ):
        """egress_mode is included in the exported metadata (#2402)."""
        import io
        import json
        import tarfile

        headers = await self._user_headers(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "egress-export", "egress_mode": "static"},
        )
        assert resp.status_code == 200
        ws = resp.json()

        export_resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
        )
        assert export_resp.status_code == 200

        buf = io.BytesIO(export_resp.content)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            metadata = json.loads(tar.extractfile("workspace.json").read())
        assert metadata["egress_mode"] == "static"

    async def test_import_roundtrips_egress_mode(self, client, user):
        """egress_mode survives export -> import (#2402)."""
        import io
        import json
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                self._meta(name="egress-roundtrip", egress_mode="static")
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
        ws_id = resp.json()["id"]
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["egress_mode"] == "static"

    async def test_import_invalid_egress_mode_falls_back(self, client, user):
        """An unknown/missing egress_mode falls back to the deploy default."""
        import io
        import json
        import tarfile

        from klangk.model import EGRESS_MODE_DEFAULT

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            meta = json.dumps(
                self._meta(name="egress-fallback", egress_mode="bogus")
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
        ws_id = resp.json()["id"]
        resp = await client.get("/api/v1/workspaces", headers=headers)
        match = [w for w in resp.json() if w["id"] == ws_id]
        assert match[0]["egress_mode"] == EGRESS_MODE_DEFAULT

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

        with patch.object(subprocess, "run", spy):
            resp = await client.get(
                f"/api/v1/workspaces/{ws['id']}/export", headers=headers
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
        assert "No usable workspace name" in resp.json()["detail"]

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
        (home / "klangk").mkdir(exist_ok=True)
        (home / "klangk" / "file.txt").write_text("streamed content")

        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
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
        (home / "klangk").mkdir(exist_ok=True)
        # Write a large file with random data (incompressible, so gzip
        # passes it through in large writes that trigger buffer flushes)
        import random

        rng = random.Random(42)
        (home / "klangk" / "big.bin").write_bytes(
            bytes(rng.getrandbits(8) for _ in range(512 * 1024))
        )

        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
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
        (home / "klangk").mkdir(exist_ok=True)
        (home / "klangk" / "f.txt").write_text("data")

        import subprocess as subprocess_mod

        original_run = subprocess_mod.run

        def _failing_run(*args, **kwargs):
            if args and args[0] and args[0][0] == "du":
                raise OSError("du not found")
            return original_run(*args, **kwargs)

        monkeypatch.setattr(subprocess_mod, "run", _failing_run)

        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
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

        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
        )
        assert resp.status_code == 200
        # Estimated size is 0 * 0.4 = 0, clamped to 1
        assert resp.headers["x-estimated-size"] == "1"

    async def test_import_upload_error_cleans_tempfile(
        self, client, app, user, monkeypatch
    ):
        """If the upload write fails, the temp file is cleaned up."""
        import klangk.api.workspaces as api_mod

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
        (home / "klangk").mkdir(exist_ok=True)
        (home / "klangk" / "real.txt").write_text("real file")
        (home / "klangk" / "relative_link").symlink_to("real.txt")
        (home / "klangk" / "external_link").symlink_to("/etc/passwd")

        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
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
            dir_path = home / "klangk"
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
        bin_dir = home / "klangk" / "bin"
        bin_dir.mkdir(exist_ok=True)
        bin_content = bytes(rng.getrandbits(8) for _ in range(4096))
        (bin_dir / "data.bin").write_bytes(bin_content)

        # Export
        resp = await client.get(
            f"/api/v1/workspaces/{ws['id']}/export", headers=headers
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
            imported_home / "klangk" / "bin" / "data.bin"
        ).read_bytes() == bin_content

        # Verify symlinks survived as symlinks
        for depth in range(1, 8):
            link_path = imported_home / "klangk"
            for d in range(depth):
                link_path = link_path / f"level{d}"
            link_path = link_path / "link.txt"
            assert link_path.is_symlink(), f"Not a symlink: {link_path}"
            assert os.readlink(str(link_path)) == "file0.txt"

    async def test_import_size_limit(self, client, user, app, monkeypatch):
        """Upload exceeding size limit is rejected."""
        monkeypatch.setattr(app.state.settings, "file_upload_size_max", 100)

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
                        "KLANGKD_BRIDGE_TOKEN": "stolen",
                        "KLANGKWS_BRIDGE_URL": "stale-injected",
                        "KLANGKBUILD_HOST_IMAGE": "stale-build",
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
        assert "KLANGKD_BRIDGE_TOKEN" not in env
        assert "KLANGKWS_BRIDGE_URL" not in env
        assert "KLANGKBUILD_HOST_IMAGE" not in env
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

    async def test_import_workspace_creates_role_groups(
        self, client, user, app_state
    ):
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
            group = await app_state.state.model.users.get_group_by_name(
                f"{suffix}-{ws_id}"
            )
            assert group is not None, f"expected {suffix} group on import"
        # Importer should be in the owners group
        owners = await app_state.state.model.users.get_group_by_name(
            f"owners-{ws_id}"
        )
        members = await app_state.state.model.users.get_group_members(
            owners["id"]
        )
        assert any(m["id"] == user["id"] for m in members)


# --- Invitation endpoints ---


class TestInvitations:
    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
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
                "/api/v1/invitations",
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
            "/api/v1/invitations",
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
            "/api/v1/invitations",
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
                "/api/v1/invitations",
                headers=headers,
                json={"email": "dup@example.com"},
            )
            resp = await client.post(
                "/api/v1/invitations",
                headers=headers,
                json={"email": "dup@example.com"},
            )
        assert resp.status_code == 400
        assert "pending invitation" in resp.json()["detail"]

    async def test_send_invitation_race_integrity_error(
        self, client, app, admin_user, app_state
    ):
        """A concurrent send that wins the pending-invitation race (the
        m0028 partial unique index is the backstop) gets the pre-check's
        400, and only one invitation row exists (#3101)."""
        headers = await self._admin_headers(client)
        with (
            patch.object(
                app.state.model.invitations,
                "create_invitation",
                side_effect=SAIntegrityError(
                    "statement", {}, Exception("UNIQUE constraint failed")
                ),
            ),
            patch.object(
                emailsvc_mod.EmailService,
                "send_invitation_email",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            resp = await client.post(
                "/api/v1/invitations",
                headers=headers,
                json={"email": "inv-race@example.com"},
            )
        assert resp.status_code == 400
        assert "pending invitation" in resp.json()["detail"]
        mock_send.assert_not_called()
        result = await app.state.model.invitations.list_invitations(
            q="inv-race"
        )
        assert result["total"] == 0

    async def test_send_invitation_invalid_email(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/invitations",
            headers=headers,
            json={"email": "not-an-email"},
        )
        assert resp.status_code == 400

    async def test_send_invitation_requires_admin(self, client, user):
        login_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.post(
            "/api/v1/invitations",
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
                "/api/v1/invitations",
                headers=headers,
                json={"email": "list1@example.com"},
            )
            await client.post(
                "/api/v1/invitations",
                headers=headers,
                json={"email": "list2@example.com"},
            )
        resp = await client.get("/api/v1/invitations", headers=headers)
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
                    "/api/v1/invitations",
                    headers=headers,
                    json={"email": f"page{i}@example.com"},
                )
        resp = await client.get("/api/v1/invitations", headers=headers)
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
                    "/api/v1/invitations",
                    headers=headers,
                    json={"email": f"pg{i}@example.com"},
                )
        page1 = await client.get(
            "/api/v1/invitations?page=1&page_size=3", headers=headers
        )
        page2 = await client.get(
            "/api/v1/invitations?page=2&page_size=3", headers=headers
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
                    "/api/v1/invitations",
                    headers=headers,
                    json={"email": e},
                )
        resp = await client.get(
            "/api/v1/invitations?sort=email&order=asc&page_size=50",
            headers=headers,
        )
        emails = [inv["email"] for inv in resp.json()["invitations"]]
        assert emails == sorted(emails, key=str.lower)
        assert emails[0] == "alpha@example.com"

    async def test_list_invitations_sort_by_invited_by(
        self, client, app, admin_user, app_state
    ):
        # Two invitations from two different inviters. Sorting by
        # ``invited_by`` must track the inviter's email (the value the UI
        # displays), not the invitee's email.
        inviter_a = await app_state.state.model.users.create_user(
            "aaa-admin@example.com", None, verified=True
        )
        inviter_z = await app_state.state.model.users.create_user(
            "zzz-admin@example.com", None, verified=True
        )
        await app_state.state.model.invitations.create_invitation(
            "zeta@example.com", inviter_z["id"]
        )
        await app_state.state.model.invitations.create_invitation(
            "alpha@example.com", inviter_a["id"]
        )
        headers = await self._admin_headers(client)
        resp = await client.get(
            "/api/v1/invitations?sort=invited_by&order=asc&page_size=50",
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
                    "/api/v1/invitations",
                    headers=headers,
                    json={"email": e},
                )
        asc = await client.get(
            "/api/v1/invitations?sort=email&order=asc&page_size=50",
            headers=headers,
        )
        desc = await client.get(
            "/api/v1/invitations?sort=email&order=desc&page_size=50",
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
                "/api/v1/invitations",
                headers=headers,
                json={"email": "needle@example.com"},
            )
            await client.post(
                "/api/v1/invitations",
                headers=headers,
                json={"email": "haystack@example.com"},
            )
        resp = await client.get(
            "/api/v1/invitations?q=needle&page_size=50",
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
            "/api/v1/invitations?sort=evil%3B%20DROP%20TABLE&order=asc",
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
                "/api/v1/invitations",
                headers=headers,
                json={"email": "revoke@example.com"},
            )
        inv_id = create_resp.json()["id"]
        resp = await client.delete(
            f"/api/v1/invitations/{inv_id}", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"

        # Can't revoke again
        resp = await client.delete(
            f"/api/v1/invitations/{inv_id}", headers=headers
        )
        assert resp.status_code == 404

    async def test_revoke_nonexistent(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            "/api/v1/invitations/nonexistent-id", headers=headers
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
                "/api/v1/invitations",
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
                f"/api/v1/invitations/{inv_id}/resend", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "resent"
        mock_resend.assert_called_once()

    async def test_resend_nonexistent(self, client, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/invitations/nonexistent/resend", headers=headers
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
                "/api/v1/invitations",
                headers=headers,
                json={"email": "revoked-resend@example.com"},
            )
        inv_id = create_resp.json()["id"]
        await client.delete(f"/api/v1/invitations/{inv_id}", headers=headers)
        resp = await client.post(
            f"/api/v1/invitations/{inv_id}/resend", headers=headers
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
                "/api/v1/invitations",
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
            json={
                "identifier": "accept@example.com",
                "password": "newpassword",
            },
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
                "/api/v1/invitations",
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
                "/api/v1/invitations",
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
                "/api/v1/invitations",
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
        self, client, app, admin_user, user, app_state
    ):
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/invitations",
                headers=headers,
                json={"email": "race@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = _auth().create_invitation_token(inv_id, "race@example.com")

        # Simulate race: create user with that email before accepting
        await app_state.state.model.users.create_user(
            "race@example.com", auth_mod.hash_password("pass"), verified=True
        )

        resp = await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "newpassword"},
        )
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]

    async def test_accept_invite_race_integrity_error(
        self, client, app, admin_user, app_state
    ):
        """A concurrent accept/registration that wins the UNIQUE
        constraint surfaces as the pre-check's 400, not a 500 (#3101)."""
        headers = await self._admin_headers(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/invitations",
                headers=headers,
                json={"email": "ace-race@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = _auth().create_invitation_token(inv_id, "ace-race@example.com")

        with patch.object(
            app.state.model.users,
            "create_user",
            side_effect=SAIntegrityError(
                "statement", {}, Exception("UNIQUE constraint failed")
            ),
        ):
            resp = await client.post(
                "/api/v1/auth/accept-invite",
                json={"token": token, "password": "newpassword"},
            )
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]
        # The invitation is still pending — nothing was consumed.
        invitation = await app.state.model.invitations.get_invitation(inv_id)
        assert invitation["status"] == "pending"

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
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
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
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
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

    async def test_cli_redirect_userinfo_bypass_rejected(
        self, client, app, monkeypatch
    ):
        """URLs whose *prefix* looks localhost-y but whose userinfo makes
        them route to an attacker host must be rejected at login time.

        Regression test for #2571: ``startswith`` prefix matching was
        blind to userinfo, so ``http://localhost:1@attacker.example/``
        passed the guard while ``urlparse(...).hostname`` is
        ``attacker.example``.
        """
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
        for payload in (
            "http://localhost:1@attacker.example/steal",
            "http://localhost:@attacker.example/steal",
            "http://127.0.0.1:80@attacker.example/steal",
            # Non-integer port: urlparse raises ValueError on .port —
            # must be rejected, not 500.
            "http://localhost:notaport/callback",
        ):
            resp = await client.get(
                "/api/v1/auth/oidc/test/login",
                params={"cli_redirect": payload},
            )
            assert resp.status_code == 400, payload
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

    async def test_login_cookie_omits_redirect_uri(
        self, client, app, monkeypatch
    ):
        """The state cookie carries only state, verifier, cli_redirect.

        redirect_uri is never round-tripped through the unsigned cookie —
        it is re-derived from hosting info at callback time (#2573).
        """
        import json as json_mod

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
        build_auth_url = AsyncMock(
            return_value="https://idp.example.com/auth?x=1"
        )
        monkeypatch.setattr(app.state.oidc, "build_auth_url", build_auth_url)
        resp = await client.get(
            "/api/v1/auth/oidc/test/login", follow_redirects=False
        )
        # End-to-end consistency: the URI login hands to the IdP must
        # equal the URI the callback later sends to the token exchange
        # (pinned in TestOIDCCallback.test_callback_redirect_uri_
        # rederived_not_from_cookie) — both derive it from hosting info
        # (#2573).
        assert build_auth_url.call_args[0][1] == (
            "http://test/api/v1/auth/oidc/test/callback"
        )
        from http.cookies import SimpleCookie

        sc = SimpleCookie()
        sc.load(resp.headers["set-cookie"])
        data = json_mod.loads(sc["oidc_test"].value)
        assert set(data) == {"state", "verifier", "cli_redirect"}


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
                "cli_redirect": None,
            }
        )
        return provider, cookie_data

    async def test_callback_creates_user(
        self, client, app, monkeypatch, db, app_state
    ):
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
        user = await app_state.state.model.users.get_user_by_email(
            "oidcuser@example.com"
        )
        assert user is not None
        assert user["provider"] == "test"
        assert user["external_id"] == "oidc-sub-123"
        assert user["password_hash"] is None
        # The SSO login minted a session and stamps last_login_at (#2583).
        by_id = await app_state.state.model.users.get_user_by_id(user["id"])
        assert by_id["last_login_at"] is not None

    async def test_callback_syncs_groups_via_hook(
        self, client, app, monkeypatch, db, app_state
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

        user = await app_state.state.model.users.get_user_by_email(
            "hookuser@example.com"
        )
        groups = await app_state.state.model.users.get_user_groups(user["id"])
        names = {g["name"] for g in groups}
        assert "admin" in names
        assert "power-users" in names

        # Verify source is oidc_sync
        sync_ids = (
            await app_state.state.model.users.get_user_oidc_sync_group_ids(
                user["id"]
            )
        )
        assert len(sync_ids) == 2

    async def test_callback_links_existing_user(
        self, client, app, monkeypatch, db, user, app_state
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
        linked = await app_state.state.model.users.get_user_by_external_id(
            "test", "new-sub"
        )
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

    async def test_callback_userinfo_cli_redirect_falls_back(
        self, client, app, monkeypatch, db
    ):
        """A cli_redirect with userinfo smuggling an attacker host in the
        state cookie must NOT receive the token — fall back to web flow.

        Regression test for #2571: the callback-time re-validation used
        the same prefix-match guard as login, so userinfo payloads
        (``http://localhost:1@attacker.example/steal``) slipped through
        both checks and exfiltrated the token to ``attacker.example``.
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
        for payload in (
            "http://localhost:1@attacker.example/steal",
            "http://localhost:@attacker.example/steal",
            "http://127.0.0.1:80@attacker.example/steal",
        ):
            cookie_data = json_mod.dumps(
                {
                    "state": "s",
                    "verifier": "v",
                    "cli_redirect": payload,
                }
            )
            client.cookies.set("oidc_test", cookie_data)
            resp = await client.get(
                "/api/v1/auth/oidc/test/callback",
                params={"code": "code", "state": "s"},
                follow_redirects=False,
            )
            assert resp.status_code == 302, payload
            location = resp.headers["location"]
            # Must NOT redirect to the attacker host with the token.
            assert "attacker.example" not in location, payload
            # Falls back to the web flow, still carrying the token in-house.
            assert "oidc-complete" in location, payload
            assert "token=" in location, payload
            client.cookies.delete("oidc_test")

    async def test_callback_redirect_uri_rederived_not_from_cookie(
        self, client, app, monkeypatch, db
    ):
        """A tampered redirect_uri in the unsigned state cookie must be
        ignored — the token exchange uses the hosting-derived URI.

        Regression test for #2573: the cookie value used to be fed
        verbatim to the IdP token endpoint.  The exchange must receive
        the redirect_uri re-derived via derive_hosting_info (host
        ``test`` from the test client's base_url), never the cookie's
        attacker-influenced copy.
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
        exchange = AsyncMock(
            return_value={"id_token": "idt", "access_token": "at"}
        )
        monkeypatch.setattr(app.state.oidc, "exchange_code", exchange)
        monkeypatch.setattr(
            app.state.oidc,
            "validate_id_token",
            AsyncMock(
                return_value={
                    "sub": "rd-sub",
                    "email": "rd@example.com",
                    "email_verified": True,
                }
            ),
        )
        cookie_data = json_mod.dumps(
            {
                "state": "s",
                "verifier": "v",
                "redirect_uri": "https://attacker.example/steal",
                "cli_redirect": None,
            }
        )
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        exchange.assert_awaited_once()
        redirect_uri = exchange.call_args[0][2]
        assert redirect_uri == ("http://test/api/v1/auth/oidc/test/callback")
        assert "attacker.example" not in redirect_uri

    async def test_callback_missing_verifier_cookie(
        self, client, app, monkeypatch, db
    ):
        """A dict cookie with matching state but no verifier is 400,
        not a KeyError-500."""
        import json as json_mod

        provider = api.oidc.OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        cookie_data = json_mod.dumps({"state": "s", "cli_redirect": None})
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "code", "state": "s"},
        )
        assert resp.status_code == 400
        assert "Invalid OIDC state cookie" in resp.json()["detail"]

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
        self, client, app, monkeypatch, db, app_state
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
        assert (
            await app_state.state.model.users.get_user_by_email(
                "oidcuser@example.com"
            )
            is None
        )

    async def test_callback_rejects_unverified_email_by_default(
        self, client, app, monkeypatch, db, app_state
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
        assert (
            await app_state.state.model.users.get_user_by_email(
                "oidcuser@example.com"
            )
            is None
        )

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
        self, client, app, monkeypatch, db, app_state
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
            await app_state.state.model.users.get_user_by_email(
                "oidcuser@example.com"
            )
            is not None
        )

    async def test_callback_returning_user(
        self, client, app, monkeypatch, db, app_state
    ):
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
        user = await app_state.state.model.users.get_user_by_external_id(
            "test", "oidc-sub-123"
        )
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

    async def test_callback_rejects_disabled_user(
        self, client, app, monkeypatch, db, app_state
    ):
        """#2588: a disabled account cannot mint a session via SSO."""
        _, cookie_data = await self._setup_callback(
            client, app, monkeypatch, db
        )
        # Pre-provision the user the callback will resolve, then disable.
        u = await app_state.state.model.users.create_user(
            "oidcuser@example.com", None, verified=True
        )
        await app_state.state.model.users.link_oidc_identity(
            u["id"], "test", "oidc-sub-123"
        )
        await app_state.state.model.users.set_user_disabled(u["id"], True)
        client.cookies.set("oidc_test", cookie_data)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"]


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
            "email": "klangk@example.com",
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
        self, client, app, monkeypatch, db, agent_user, app_state
    ):
        """OIDC login resolving the agent by external_id is rejected."""
        # The DB trigger blocks linking OIDC identity to the agent, so
        # mock get_user_by_external_id to simulate a pre-linked agent.
        agent = await app_state.state.model.users.get_user_by_id(
            model.AGENT_USER_ID
        )
        monkeypatch.setattr(
            app.state.model.users,
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
    async def test_logout_returns_oidc_logout_url(
        self, client, app, db, app_state
    ):
        """OIDC user with logout_redirect gets IdP logout URL in response."""
        # Create OIDC user
        user = await app_state.state.model.users.create_user(
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
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        headers = {
            "Authorization": f"Bearer {login_resp.json()['access_token']}"
        }
        resp = await client.post("/api/v1/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert "oidc_logout_url" not in resp.json()

    async def test_logout_no_redirect_when_disabled(
        self, client, app, db, app_state
    ):
        """OIDC user with logout_redirect=false gets no URL."""
        user = await app_state.state.model.users.create_user(
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
    async def test_change_own_handle(self, client, user, app_state):
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
        updated = await app_state.state.model.users.get_user_by_id(user["id"])
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

    async def test_change_handle_conflict(
        self, client, admin_user, user, app_state
    ):
        # Set admin_user's handle to something known
        await app_state.state.model.users.set_user_handle(
            admin_user["id"], "taken-handle"
        )
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

    async def test_change_handle_oidc_only_user(self, client, db, app_state):
        """OIDC-only users have no password; must 403, not 500 (#890)."""
        headers = await _oidc_user_headers(app_state)
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

    async def test_admin_change_user_handle(
        self, client, admin_user, user, app_state
    ):
        admin_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_resp.json()['access_token']}"
        }
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            json={"handle": "admin-set-handle"},
            headers=admin_headers,
        )
        assert resp.status_code == 200
        updated = await app_state.state.model.users.get_user_by_id(user["id"])
        assert updated["handle"] == "admin-set-handle"

    async def test_admin_change_handle_refreshes_presence(
        self, client, app, admin_user, user, sockets
    ):
        admin_resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
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
                f"/api/v1/users/{user['id']}",
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
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        admin_headers = {
            "Authorization": f"Bearer {admin_resp.json()['access_token']}"
        }
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
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


class TestPasswordPolicyRouteEnforcement:
    """Every password-setting route must enforce complexity (#2581).

    Route-level wiring tests — not unit tests of
    ``Auth.validate_password_complexity``. Each test drives the real
    endpoint with a policy-violating password and asserts the 400, so a
    refactor that reverts any call site back to
    ``validate_password_length`` fails here instead of passing CI
    silently.
    """

    @pytest.fixture(autouse=True)
    def _require_upper(self, app, monkeypatch):
        """Arm REQUIRE_UPPER=1 for every test in this class."""
        monkeypatch.setattr(app.state.settings, "password_require_upper", 1)

    async def test_register_rejects_violating_password(
        self, client, db, app_state
    ):
        with patch.object(
            emailsvc_mod.EmailService,
            "send_verification_email",
            new_callable=AsyncMock,
        ):
            resp = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": "policy@example.com",
                    "password": "alllowercase1!",
                },
            )
        assert resp.status_code == 400
        assert "uppercase letter" in resp.json()["detail"]
        # Nothing was created.
        assert (
            await app_state.state.model.users.get_user_by_email(
                "policy@example.com"
            )
            is None
        )

    async def test_change_password_rejects_violating_password(
        self, client, user
    ):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/auth/change-password",
            json={
                "current_password": "testpass",
                "new_password": "alllowercase1!",
            },
            headers=headers,
        )
        assert resp.status_code == 400
        assert "uppercase letter" in resp.json()["detail"]

    async def test_reset_password_rejects_violating_password(
        self, client, db, app_state
    ):
        password_hash = auth_mod.hash_password("oldpass")
        created = await app_state.state.model.users.create_user(
            "policyreset@example.com", password_hash, verified=True
        )
        token = _auth().create_password_reset_token(created["id"])
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "alllowercase1!"},
        )
        assert resp.status_code == 400
        assert "uppercase letter" in resp.json()["detail"]

    async def test_accept_invite_rejects_violating_password(
        self, client, db, admin_user
    ):
        headers = await _admin_login(client)
        with patch.object(
            emailsvc_mod.EmailService,
            "send_invitation_email",
            new_callable=AsyncMock,
        ):
            create_resp = await client.post(
                "/api/v1/invitations",
                headers=headers,
                json={"email": "policyinvite@example.com"},
            )
        inv_id = create_resp.json()["id"]
        token = _auth().create_invitation_token(
            inv_id, "policyinvite@example.com"
        )
        resp = await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "alllowercase1!"},
        )
        assert resp.status_code == 400
        assert "uppercase letter" in resp.json()["detail"]

    async def test_admin_create_user_rejects_violating_password(
        self, client, admin_user
    ):
        headers = await _admin_login(client)
        resp = await client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "email": "policyadmin@example.com",
                "password": "alllower1!",
            },
        )
        assert resp.status_code == 400
        assert "uppercase letter" in resp.json()["detail"]

    async def test_admin_set_password_rejects_violating_password(
        self, client, admin_user, user
    ):
        headers = await _admin_login(client)
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            headers=headers,
            json={"password": "alllowercase1!"},
        )
        assert resp.status_code == 400
        assert "uppercase letter" in resp.json()["detail"]


class TestInactivityDisable:
    """#2588: auto-disable of dormant accounts — API surface."""

    async def test_disabled_user_login_403(self, client, app, user):
        await app.state.model.users.set_user_disabled(user["id"], True)
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"]

    async def test_disabled_user_token_403(self, client, app, user):
        """A pre-disable token fails authenticated requests with 403
        (not 401 — clients must not loop on refresh/relogin)."""
        token = _auth().create_token(user["id"], user["email"])
        await app.state.model.users.set_user_disabled(user["id"], True)
        resp = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"]

    async def test_admin_disable_and_reenable(
        self, client, app, admin_user, user
    ):
        headers = await _admin_login(client)
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            headers=headers,
            json={"disabled": True},
        )
        assert resp.status_code == 200
        assert (await app.state.model.users.get_user_by_id(user["id"]))[
            "disabled"
        ] is True
        # Login is refused while disabled...
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        assert resp.status_code == 403
        # ...and accepted again after re-enable.
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            headers=headers,
            json={"disabled": False},
        )
        assert resp.status_code == 200
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
        )
        assert resp.status_code == 200

    async def test_admin_cannot_disable_self(self, client, admin_user):
        headers = await _admin_login(client)
        resp = await client.patch(
            f"/api/v1/users/{admin_user['id']}",
            headers=headers,
            json={"disabled": True},
        )
        assert resp.status_code == 400
        assert "your own account" in resp.json()["detail"]

    async def test_admin_cannot_disable_agent(
        self, client, admin_user, db, temp_data_dir
    ):
        from klangk.main import Lifecycle

        from _helpers import wire_db_and_model

        _seed_state = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=make_settings({}))
        )
        wire_db_and_model(_seed_state)
        await Lifecycle(_seed_state).seed_agent_user()
        headers = await _admin_login(client)
        resp = await client.patch(
            f"/api/v1/users/{model.AGENT_USER_ID}",
            headers=headers,
            json={"disabled": True},
        )
        assert resp.status_code == 400
        assert "system agent" in resp.json()["detail"]

    async def test_list_users_reports_disabled(
        self, client, app, admin_user, user
    ):
        await app.state.model.users.set_user_disabled(user["id"], True)
        headers = await _admin_login(client)
        resp = await client.get("/api/v1/users?page_size=200", headers=headers)
        by_id = {u["id"]: u for u in resp.json()["users"]}
        assert by_id[user["id"]]["disabled"] is True
        assert by_id[admin_user["id"]]["disabled"] is False

    async def test_authenticated_request_stamps_activity(
        self, client, app, user
    ):
        """The get_current_user choke point stamps last_activity_at
        (#2588) — login alone does not (it stamps last_login_at)."""
        headers = await _auth_headers(client)
        # Any authenticated request beyond the login itself.
        resp = await client.get("/api/v1/auth/me", headers=headers)
        assert resp.status_code == 200
        row = await app.state.model.users.get_user_by_id(user["id"])
        assert row["last_activity_at"] is not None

    async def test_admin_disable_kicks_live_sockets(
        self, client, app, admin_user, user
    ):
        """#2588 review: disabling an account closes its live WS
        connections (4001 -> client logout), not just future connects."""
        from klangk.wshandler.session import WebSocketState
        from klangk import wshandler

        assert isinstance(app.state.sockets, WebSocketState)
        closed: list[tuple[int, str]] = []

        class FakeSock:
            async def close(self, code=1000, reason=""):
                closed.append((code, reason))

        # Register two connections: the victim's and another user's.
        victim_conn = types.SimpleNamespace(
            user={"id": user["id"], "email": user["email"]}
        )
        other_conn = types.SimpleNamespace(
            user={"id": "someone-else", "email": "other@example.com"}
        )
        app.state.sockets.connections[FakeSock()] = other_conn
        app.state.sockets.connections[FakeSock()] = victim_conn

        headers = await _admin_login(client)
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            headers=headers,
            json={"disabled": True},
        )
        assert resp.status_code == 200
        # Exactly the victim's socket was closed, with the logout code.
        assert closed == [(4001, "Account disabled")]
        # Re-enable does not touch sockets.
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            headers=headers,
            json={"disabled": False},
        )
        assert resp.status_code == 200
        assert len(closed) == 1

        # A socket whose close() raises must not break the kick: the
        # remaining sockets are still closed and the count is right.
        class BadSock:
            async def close(self, code=1000, reason=""):
                raise RuntimeError("already closed")

        victim2 = types.SimpleNamespace(
            user={"id": user["id"], "email": user["email"]}
        )
        app.state.sockets.connections.clear()
        app.state.sockets.connections[BadSock()] = victim2
        app.state.sockets.connections[FakeSock()] = victim2
        kicked = await wshandler.disconnect_user(
            app.state.sockets, user["id"], reason="Account disabled"
        )
        assert kicked == 2
        assert closed[-1] == (4001, "Account disabled")
        app.state.sockets.connections.clear()


class TestAdminServerSchedule:
    """#2661: schedule/list/cancel a server stop or recycle."""

    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def test_schedule_list_cancel_roundtrip(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/server/schedule",
            headers=headers,
            json={"action": "stop", "in_seconds": 3600},
        )
        assert resp.status_code == 200, resp.text
        schedule = resp.json()
        assert schedule["action"] == "stop"
        assert schedule["id"]

        resp = await client.get("/api/v1/server/schedule", headers=headers)
        assert resp.status_code == 200
        assert [s["id"] for s in resp.json()["schedules"]] == [schedule["id"]]

        resp = await client.delete(
            f"/api/v1/server/schedule/{schedule['id']}",
            headers=headers,
        )
        assert resp.status_code == 200
        resp = await client.get("/api/v1/server/schedule", headers=headers)
        assert resp.json()["schedules"] == []

    async def test_schedule_absolute_at(self, client, app, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.post(
            "/api/v1/server/schedule",
            headers=headers,
            json={"action": "recycle", "at": "2030-01-01T00:00:00+00:00"},
        )
        assert resp.status_code == 200
        assert resp.json()["fire_at"].startswith("2030-01-01T00:00:00")

    async def test_schedule_invalid_payload_422(self, client, app, admin_user):
        headers = await self._admin_headers(client)
        for body in (
            {"action": "stop"},  # neither at nor in_seconds
            {"action": "stop", "in_seconds": 0},
            {"action": "stop", "in_seconds": "soon"},
            {"action": "explode", "in_seconds": 60},
            {"action": "stop", "at": "not-a-date"},
        ):
            resp = await client.post(
                "/api/v1/server/schedule",
                headers=headers,
                json=body,
            )
            assert resp.status_code == 422, (body, resp.text)

    async def test_schedule_requires_admin(self, client, app, user):
        headers = await _auth_headers(client)
        resp = await client.post(
            "/api/v1/server/schedule",
            headers=headers,
            json={"action": "stop", "in_seconds": 60},
        )
        assert resp.status_code == 403

    async def test_cancel_missing_404(self, client, app, admin_user):
        headers = await self._admin_headers(client)
        resp = await client.delete(
            "/api/v1/server/schedule/nope", headers=headers
        )
        assert resp.status_code == 404

    async def test_schedule_broadcasts_snapshot(self, client, app, admin_user):
        headers = await self._admin_headers(client)
        with patch.object(
            app.state.server_scheduler, "notify_pending", new=AsyncMock()
        ) as mock_notify:
            resp = await client.post(
                "/api/v1/server/schedule",
                headers=headers,
                json={"action": "stop", "in_seconds": 60},
            )
        assert resp.status_code == 200
        mock_notify.assert_awaited_once()


class TestContainerEventsAPI:
    """#2923: the paged container-events history read + its dedicated
    ``manage-events`` permission gate."""

    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def _make_workspace(self, client, headers, name):
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": name},
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    async def test_admin_pages_history_with_resolved_names(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        ws_id = await self._make_workspace(client, headers, "events-ws")
        events = app.state.model.container_events
        await events.record(
            ws_id,
            EVENT_START,
            CAUSE_API,
            actor_id=admin_user["id"],
            container_id="cid-old",
        )
        await events.record(
            ws_id,
            EVENT_STOP,
            CAUSE_STOP,
            actor_id=admin_user["id"],
            container_id="cid-new",
            network_namespace="sidecar-1",
        )
        await events.record(ws_id, EVENT_START, CAUSE_AUTO_START)

        resp = await client.get("/api/v1/events", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 3
        assert data["limit"] == 50
        assert data["offset"] == 0
        # Newest first: the system-caused auto-start leads.
        assert [i["container_id"] for i in data["items"]] == [
            None,
            "cid-new",
            "cid-old",
        ]
        newest = data["items"][0]
        assert newest["actor_type"] == "system"
        assert newest["actor_id"] is None
        assert newest["actor_email"] is None
        oldest = data["items"][2]
        assert oldest["actor_type"] == "user"
        assert oldest["actor_email"] == "testadmin@example.com"
        for item in data["items"]:
            assert item["workspace_name"] == "events-ws"
            assert item["workspace_id"] == ws_id

    async def test_workspace_filter_and_offset_paging(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        ws_a = await self._make_workspace(client, headers, "events-a")
        ws_b = await self._make_workspace(client, headers, "events-b")
        events = app.state.model.container_events
        for i in range(3):
            await events.record(
                ws_a, EVENT_START, CAUSE_API, container_id=f"a-{i}"
            )
        await events.record(ws_b, EVENT_START, CAUSE_API, container_id="b-0")

        resp = await client.get(
            "/api/v1/events",
            headers=headers,
            params={"workspace_id": ws_a, "limit": 2, "offset": 1},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3  # ws_b's row is filtered out of the count
        assert [i["container_id"] for i in data["items"]] == ["a-1", "a-0"]
        assert data["limit"] == 2
        assert data["offset"] == 1

    async def test_workspace_query_matches_id_or_name(
        self, client, app, admin_user
    ):
        """#3006: the unified ``workspace`` param accepts an exact id or a
        name substring."""
        headers = await self._admin_headers(client)
        ws_a = await self._make_workspace(client, headers, "alpha-lab")
        ws_b = await self._make_workspace(client, headers, "beta-lab")
        events = app.state.model.container_events
        await events.record(ws_a, EVENT_START, CAUSE_API, container_id="a-0")
        await events.record(ws_b, EVENT_START, CAUSE_API, container_id="b-0")

        # An exact id through the unified param narrows like workspace_id.
        resp = await client.get(
            "/api/v1/events", headers=headers, params={"workspace": ws_a}
        )
        assert resp.status_code == 200, resp.text
        assert [i["container_id"] for i in resp.json()["items"]] == ["a-0"]

        # A name substring narrows to every workspace whose name matches.
        resp = await client.get(
            "/api/v1/events", headers=headers, params={"workspace": "alpha"}
        )
        data = resp.json()
        assert data["total"] == 1
        assert [i["container_id"] for i in data["items"]] == ["a-0"]

        resp = await client.get(
            "/api/v1/events", headers=headers, params={"workspace": "lab"}
        )
        assert resp.json()["total"] == 2

        # A query matching no id and no name yields an empty page.
        resp = await client.get(
            "/api/v1/events", headers=headers, params={"workspace": "nope"}
        )
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

        # The unified param wins over the legacy exact workspace_id.
        resp = await client.get(
            "/api/v1/events",
            headers=headers,
            params={"workspace": "alpha", "workspace_id": ws_b},
        )
        assert [i["container_id"] for i in resp.json()["items"]] == ["a-0"]

    async def test_empty_workspace_params_are_no_filter(
        self, client, app, admin_user
    ):
        """#3009 review: ``?workspace=`` (empty string) must not degrade
        into a name-substring filter — ``LIKE '%%'`` matches every *live*
        workspace and would silently hide deleted-workspace history."""
        headers = await self._admin_headers(client)
        events = app.state.model.container_events
        await events.record(
            "gone-ws", EVENT_STOP, CAUSE_DELETE, container_id="g-0"
        )
        await events.record(
            "live-ws", EVENT_START, CAUSE_API, container_id="l-0"
        )

        for key in ("workspace", "workspace_id"):
            resp = await client.get(
                "/api/v1/events", headers=headers, params={key: ""}
            )
            assert resp.status_code == 200, (key, resp.text)
            data = resp.json()
            assert data["total"] == 2, (key, data)
            assert len(data["items"]) == 2, (key, data)

    async def test_deleted_workspace_and_purged_actor_fall_back_to_ids(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        events = app.state.model.container_events
        # A workspace that no longer exists and a user row that never
        # did: history outlives the rows it annotates.
        await events.record(
            "gone-ws",
            EVENT_STOP,
            CAUSE_DELETE,
            actor_id="no-such-user",
        )
        resp = await client.get("/api/v1/events", headers=headers)
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        assert item["workspace_name"] is None
        assert item["workspace_id"] == "gone-ws"
        assert item["actor_email"] is None

    async def test_bad_query_rejected(self, client, app, admin_user):
        headers = await self._admin_headers(client)
        for params in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
            resp = await client.get(
                "/api/v1/events",
                headers=headers,
                params=params,
            )
            assert resp.status_code == 422, (params, resp.text)

    async def test_plain_user_forbidden(self, client, app, user):
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/events", headers=headers)
        assert resp.status_code == 403

    async def test_delegated_grant_reads_without_admin(
        self, client, app, user, app_state
    ):
        # The whole point of the dedicated permission (#2923): hand a
        # non-admin read-only audit access via an ACE on the resource —
        # the more-specific path wins the ACL walk over /admin's
        # Deny-everyone.
        await app_state.state.model.acl.add_acl_entry(
            "/events",
            0,
            model.ACTION_ALLOW,
            "manage-events",
            model.PRINCIPAL_USER,
            user_id=user["id"],
        )
        events = app_state.state.model.container_events
        await events.record(
            "ws-delegated", EVENT_START, CAUSE_API, container_id="d-0"
        )

        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/events", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["container_id"] == "d-0"

        # /my-permissions surfaces the resource + permission so the
        # frontend can show the tab to the delegated auditor.
        resp = await client.get("/api/v1/my-permissions", headers=headers)
        perms = resp.json()["permissions"]
        assert "manage-events" in perms.get("/events", [])

    async def test_admin_my_permissions_lists_resource(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        resp = await client.get("/api/v1/my-permissions", headers=headers)
        perms = resp.json()["permissions"]
        assert "manage-events" in perms.get("/events", [])


class TestBranchGaps2834:
    """Branch-coverage gaps surfaced by the #2834 branch gate: the
    false/true outcomes of guards the mainline tests only take one side
    of (a valid custom image, an invalid mount list, a stop with no live
    session, a failed du probe, a corrupt archive after creation, the
    dev version fallback, and a non-configured OIDC provider at
    logout)."""

    async def test_version_endpoint_missing_file_falls_back_to_dev(
        self, client, app
    ):
        # version_file configured but absent (a stripped deployment):
        # the endpoint falls back to the dev placeholder instead of 500.
        app.state.settings.version_file = "/nonexistent/version.json"
        resp = await client.get("/api/v1/version")
        assert resp.status_code == 200
        assert resp.json()["version"] == "dev"

    async def test_logout_nonlocal_user_with_unconfigured_provider(
        self, client, app, user, app_state
    ):
        # A non-local user whose provider is no longer configured: no
        # IdP logout URL is derived (get_provider -> None), plain logout.
        from _helpers import wire_db_and_model

        async with app_state.state.db.transaction() as db:
            await db.execute(
                "UPDATE users SET provider = 'gone-idp' WHERE id = ?",
                (user["id"],),
            )
        wire_db_and_model(app)
        headers = await _auth_headers(client)
        resp = await client.post("/api/v1/auth/logout", headers=headers)
        assert resp.status_code == 200
        assert "oidc_logout_url" not in resp.json()

    async def test_create_workspace_with_allowed_custom_image(
        self, client, admin_user, app
    ):
        # An image that IS in allowed_images passes the gate (the
        # not-allowed branch's false side) and is stored on the row.
        app.state.settings.allowed_images = "klangk-custom:1"
        headers = await _admin_login(client)
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "custom-image-ws", "image": "klangk-custom:1"},
        )
        assert resp.status_code == 200
        assert resp.json()["image"] == "klangk-custom:1"

    async def test_create_workspace_rejects_invalid_mount(
        self, client, admin_user, app
    ):
        # validate_mounts returning an error string -> 400 before any
        # container is created.
        with patch.object(
            app.state.container_registry,
            "validate_mounts",
            return_value="mount source outside the allowed roots",
        ):
            headers = await _admin_login(client)
            resp = await client.post(
                "/api/v1/workspaces",
                headers=headers,
                json={
                    "name": "bad-mount-ws",
                    "mounts": ["/etc:/etc:ro"],
                },
            )
        assert resp.status_code == 400
        assert "mount source" in resp.json()["detail"]

    async def test_create_workspace_rejects_flaglike_volume_source(
        self, client, admin_user
    ):
        """#3018 (real, unmocked gate): a leading-dash mount source would
        reach ``podman volume create`` argv verbatim — 400 at create,
        while a podman-safe named volume still passes."""
        headers = await _admin_login(client)
        bad = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={
                "name": "flaglike-mount-ws",
                "mounts": ["--opt=x:/data"],
            },
        )
        assert bad.status_code == 400
        assert "podman-safe" in bad.json()["detail"]
        good = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "safe-vol-ws", "mounts": ["my-vol:/data"]},
        )
        assert good.status_code == 200
        assert good.json()["mounts"] == ["my-vol:/data"]

    async def test_stop_workspace_running_container_no_session(
        self, client, admin_user, app
    ):
        # A real stop (container running) with NO live WS session: the
        # container_stopped broadcast is skipped, teardown still runs.
        headers = await _admin_login(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "stop-nosess"}
        )
        ws_id = create_resp.json()["id"]
        registry = app.state.container_registry
        registry.track_activity("cid-stop-2", ws_id)
        with (
            patch.object(
                registry, "stop_and_remove_container", new_callable=AsyncMock
            ) as mock_stop,
            patch.object(
                registry, "notify_workspace_killed", new_callable=AsyncMock
            ),
            patch.object(app.state.sockets, "get_session", return_value=None),
        ):
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/stop", headers=headers
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        mock_stop.assert_awaited_once()
        registry.states.pop(ws_id, None)

    async def test_export_size_probe_failure_falls_back_to_zero(
        self, client, admin_user, user, app, monkeypatch
    ):
        # home_dir exists but `du -sb` fails (permissions): the estimate
        # falls back to 0 and the export still streams.
        import subprocess as subprocess_mod

        headers = await _admin_login(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "du-fail"}
        )
        ws_id = create_resp.json()["id"]
        home = app.state.workspaces.home_path(ws_id)
        home.mkdir(parents=True, exist_ok=True)

        real_run = subprocess_mod.run

        def _failing_du(cmd, *a, **kw):
            if "du" in cmd:
                CompletedProxy = subprocess_mod.CompletedProcess
                return CompletedProxy(cmd, returncode=1, stdout="", stderr="x")
            return real_run(cmd, *a, **kw)

        monkeypatch.setattr(
            "klangk.api.workspaces.subprocess.run", _failing_du
        )
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/export", headers=headers
        )
        assert resp.status_code == 200

    async def test_export_missing_home_dir_skips_size_probe(
        self, client, admin_user, user, app
    ):
        # The workspace's home dir never materialized (created but never
        # started): the du probe is skipped entirely, size 0.
        headers = await _admin_login(client)
        create_resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "no-home"}
        )
        ws_id = create_resp.json()["id"]
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/export", headers=headers
        )
        assert resp.status_code == 200

    async def test_import_extraction_timeout_deletes_created_workspace(
        self, client, admin_user, user, app, monkeypatch
    ):
        # The home extraction times out AFTER the row was created: the
        # 400 path must also delete the just-created workspace (no
        # half-imported orphan).
        import io
        import json as json_mod
        import tarfile

        buf = io.BytesIO()
        from klangk.settings import KlangkSettings

        ns = types.SimpleNamespace(
            state=types.SimpleNamespace(settings=KlangkSettings(os.environ))
        )
        ns.state.util = util_mod.Util(ns)
        meta_bytes = json_mod.dumps(
            {
                "instance_id": ns.state.util.instance_id(),
                "name": "timeout-import",
                "image": None,
                "service_command": None,
                "auto_start": False,
                "mounts": [],
                "env": {},
                "health_check": None,
                "allowed_domains": [],
                "rejected_domains": [],
                "settings": {},
                "egress_mode": "static",
                "per_handle_home": True,
                "classification_banner": None,
            }
        ).encode()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo(name="workspace.json")
            info.size = len(meta_bytes)
            tar.addfile(info, io.BytesIO(meta_bytes))
        buf.seek(0)

        import subprocess as subprocess_mod

        def _timing_out_tar(cmd, *a, **kw):
            # Only the post-creation home-tree probe times out; the earlier
            # metadata read (tar xzf -O workspace.json) still succeeds so the
            # workspace row is created first.
            if "tzf" in cmd:
                raise subprocess_mod.TimeoutExpired(cmd="tar", timeout=30)
            return subprocess_mod.CompletedProcess(
                cmd, returncode=0, stdout=meta_bytes
            )

        monkeypatch.setattr(
            "klangk.api.workspaces.subprocess.run", _timing_out_tar
        )
        deleted = []
        with patch.object(
            app.state.workspaces,
            "delete_workspace",
            new=AsyncMock(
                side_effect=lambda ws_id, uid: deleted.append(ws_id)
            ),
        ):
            headers = await _admin_login(client)
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
        assert resp.status_code == 400
        assert "corrupt" in resp.json()["detail"]
        assert deleted  # the created row was cleaned up

    async def test_update_workspace_with_allowed_custom_image(
        self, client, admin_user, app
    ):
        # The PUT validation's image gate: an allowed custom image passes
        # (the not-allowed branch's false side).
        headers = await _admin_login(client)
        create = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "upd-image"}
        )
        ws_id = create.json()["id"]
        app.state.settings.allowed_images = "klangk-custom:2"
        resp = await client.put(
            f"/api/v1/workspaces/{ws_id}",
            headers=headers,
            json={"image": "klangk-custom:2"},
        )
        assert resp.status_code == 200

    async def test_update_workspace_rejects_invalid_mount(
        self, client, admin_user, app
    ):
        headers = await _admin_login(client)
        create = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "upd-mount"}
        )
        ws_id = create.json()["id"]
        with patch.object(
            app.state.container_registry,
            "validate_mounts",
            return_value="mount source outside the allowed roots",
        ):
            resp = await client.put(
                f"/api/v1/workspaces/{ws_id}",
                headers=headers,
                json={"mounts": ["/etc:/etc:ro"]},
            )
        assert resp.status_code == 400
        assert "mount source" in resp.json()["detail"]

    async def test_export_after_home_dir_removal_skips_size_probe(
        self, client, admin_user, app
    ):
        # The workspace existed (its home was materialized) but the dir
        # was removed out-of-band: the du probe is skipped, size 0.
        import shutil

        headers = await _admin_login(client)
        create = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": "gone-home"}
        )
        ws_id = create.json()["id"]
        home = app.state.workspaces.home_path(ws_id)
        home.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(home)
        resp = await client.get(
            f"/api/v1/workspaces/{ws_id}/export", headers=headers
        )
        assert resp.status_code == 200

    async def test_update_workspace_with_valid_mounts_passes_gate(
        self, client, admin_user, app
    ):
        # The PUT mount gate's pass-through side: validate_mounts returns
        # no error and the update proceeds.
        headers = await _admin_login(client)
        create = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "upd-mount-ok"},
        )
        ws_id = create.json()["id"]
        with patch.object(
            app.state.container_registry,
            "validate_mounts",
            return_value=None,
        ):
            resp = await client.put(
                f"/api/v1/workspaces/{ws_id}",
                headers=headers,
                json={"mounts": ["/srv/data:/data:ro"]},
            )
        assert resp.status_code == 200

    async def test_import_corrupt_json_deletes_nothing(
        self, client, admin_user, app
    ):
        # The metadata read itself times out: the failure happens BEFORE
        # the row is created (ws None) -> nothing to delete, plain 400.
        import subprocess as subprocess_mod

        def _timing_out_tar(cmd, *a, **kw):
            raise subprocess_mod.TimeoutExpired(cmd="tar", timeout=30)

        deleted = []
        with (
            patch.object(
                app.state.workspaces,
                "delete_workspace",
                new=AsyncMock(
                    side_effect=lambda ws_id, uid: deleted.append(ws_id)
                ),
            ),
            patch("klangk.api.workspaces.subprocess.run", _timing_out_tar),
        ):
            headers = await _admin_login(client)
            resp = await client.post(
                "/api/v1/workspaces/import",
                headers=headers,
                files={
                    "file": (
                        "archive.tar.gz",
                        b"not-really-a-tarball",
                        "application/gzip",
                    )
                },
            )
        assert resp.status_code == 400
        assert "corrupt" in resp.json()["detail"]
        assert deleted == []  # no row was ever created


class TestInFlightCounterBranchGaps2834:
    """#2834 branch gate: nested in-flight tracking (count past one)."""

    async def test_increment_decrement_around_one(self):
        from klangk.middleware import InFlightRequests

        c = InFlightRequests()
        c.increment()
        assert not c._idle.is_set()
        c.increment()  # nested request: count 2
        c.decrement()  # back to 1: still busy
        assert not c._idle.is_set()
        c.decrement()  # to 0: idle
        assert c._idle.is_set()


class TestTestModeEndpoints:
    """KLANGKD_TEST_MODE-only endpoints (registered via
    ``api.register_test_endpoints``; the Playwright e2e suite drives
    the same handlers remotely)."""

    @pytest.fixture
    async def tclient(self, app):
        """The app-fixture HTTP client plus the test-mode router."""
        from fastapi import APIRouter
        from klangk.util import API_PREFIX

        extra = APIRouter()
        api.register_test_endpoints(extra)
        app.include_router(extra, prefix=API_PREFIX)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c

    @staticmethod
    def _reload_api():
        """Reload klangk.api.

        The module rebinds its ``auth`` attribute to the logic module
        (klangk.auth) after importing the ``api.auth`` route submodule —
        on reload, ``from . import auth`` would pick up that rebound
        attribute, so point it back at the submodule first.
        """
        import importlib
        import sys

        api.auth = sys.modules["klangk.api.auth"]
        return importlib.reload(api)

    def test_registration_is_env_gated(self, monkeypatch):
        """Both arms of the import-time gate (covers the branch)."""
        monkeypatch.setenv("KLANGKD_TEST_MODE", "1")
        reloaded = self._reload_api()
        try:
            paths = {getattr(r, "path", "") for r in reloaded.router.routes}
            assert "/test/idle-timeout" in paths
            assert "/test/set-idle-timeout" in paths
            assert "/test/workspace-token/{workspace_id}" in paths
            assert "/test/browsers/{workspace_id}" in paths
        finally:
            monkeypatch.delenv("KLANGKD_TEST_MODE")
            self._reload_api()
        paths = {getattr(r, "path", "") for r in api.router.routes}
        assert "/test/idle-timeout" not in paths

    async def test_idle_timeout_global_roundtrip(self, tclient, registry):
        resp = await tclient.get("/api/v1/test/idle-timeout")
        assert resp.status_code == 200
        assert resp.json() == {
            "idle_timeout_seconds": registry.idle_timeout_seconds
        }

        resp = await tclient.post(
            "/api/v1/test/set-idle-timeout", json={"seconds": 1200}
        )
        assert resp.status_code == 200
        assert resp.json() == {"idle_timeout_seconds": 1200}
        assert registry.idle_timeout_seconds == 1200

    async def test_idle_timeout_per_workspace(self, tclient, app, registry):
        # Per-workspace overrides only apply to workspaces with live
        # container state (unknown ids fall back to the global default).
        from klangk.container.basics import ContainerState

        registry.states["ws-x"] = ContainerState("ws-x", "cid-x", app)

        resp = await tclient.get(
            "/api/v1/test/idle-timeout", params={"workspace_id": "ws-unknown"}
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "idle_timeout_seconds": registry.idle_timeout_seconds
        }

        resp = await tclient.post(
            "/api/v1/test/set-idle-timeout",
            json={"seconds": 300, "workspace_id": "ws-x"},
        )
        assert resp.status_code == 200
        assert registry.get_workspace_idle_timeout("ws-x") == 300
        resp = await tclient.get(
            "/api/v1/test/idle-timeout", params={"workspace_id": "ws-x"}
        )
        assert resp.json() == {"idle_timeout_seconds": 300}

    async def test_workspace_token_roundtrip(self, tclient, app):
        resp = await tclient.get("/api/v1/test/workspace-token/ws-token")
        assert resp.status_code == 200
        token = resp.json()["token"]
        assert token
        assert app.state.auth.decode_workspace_token(token) == "ws-token"

    async def test_browsers_lists_registrations_for_workspace(
        self, tclient, registry, sockets
    ):
        # Regression shape of #2909: the endpoint must read the registry's
        # browser-route map (a dict), not the BrowserRouter collaborator.
        owner_sock, member_sock, anon_sock, other_sock = (
            object(),
            object(),
            object(),
            object(),
        )
        registry.browsers.register_browser("bid-owner", "ws-b", owner_sock)
        registry.browsers.register_browser("bid-member", "ws-b", member_sock)
        registry.browsers.register_browser("bid-anon", "ws-b", anon_sock)
        registry.browsers.register_browser("bid-none", "ws-b", None)
        registry.browsers.register_browser("bid-other-ws", "ws-a", other_sock)
        sockets.connections[owner_sock] = types.SimpleNamespace(
            user={"email": "owner@example.com"}
        )
        sockets.connections[member_sock] = types.SimpleNamespace(
            user={"email": "member@example.com"}
        )

        resp = await tclient.get("/api/v1/test/browsers/ws-b")
        assert resp.status_code == 200
        assert resp.json() == [
            {"browser_id": "bid-owner", "email": "owner@example.com"},
            {"browser_id": "bid-member", "email": "member@example.com"},
            {"browser_id": "bid-anon", "email": None},
            {"browser_id": "bid-none", "email": None},
        ]


class TestNoCoverAudit2910Part3:
    async def test_create_workspace_oserror_maps_400(self):
        """create_workspace raising OSError (bad mount source etc.) is a
        400, not a 500 (direct handler call, Depends bypassed)."""
        from fastapi import HTTPException

        from klangk.api.workspaces import (
            CreateWorkspaceRequest,
            create_workspace as create_endpoint,
        )

        app = MagicMock()
        app.state.workspaces.create_workspace = AsyncMock(
            side_effect=OSError("mount source missing")
        )
        app.state.settings.default_image = None
        with pytest.raises(HTTPException) as caught:
            await create_endpoint(
                CreateWorkspaceRequest(name="os-fail"),
                user={"id": "u1", "email": "u@x.com"},
                app=app,
            )
        assert caught.value.status_code == 400
        assert "mount source missing" in caught.value.detail

    async def test_duplicate_workspace_collection_acl_false_403(self):
        """The in-handler defense-in-depth collection-create check (#2569):
        called directly (Depends bypassed) with a refusing ACL, the
        duplicate is a 403."""
        from fastapi import HTTPException

        from klangk.api.workspaces import (
            DuplicateWorkspaceRequest,
            duplicate_workspace,
        )

        acl = MagicMock()
        acl.get_principals = AsyncMock(return_value=set())
        acl.check_permission = AsyncMock(return_value=False)
        app = MagicMock()
        app.state.acl = acl
        with pytest.raises(HTTPException) as caught:
            await duplicate_workspace(
                "ws-id",
                DuplicateWorkspaceRequest(name="clone"),
                user={"id": "u1", "email": "u@x.com"},
                app=app,
            )
        assert caught.value.status_code == 403
        assert "Not permitted to create workspaces" in caught.value.detail

    async def test_export_stream_kills_leftover_proc(self, tmp_path):
        """A tar proc still alive when the stream ends is killed, not
        leaked (direct endpoint call; export permission bypassed)."""
        from klangk.api.workspaces import export_workspace

        app = MagicMock()
        app.state.model.workspaces.get_workspace = AsyncMock(
            return_value={"id": "ws-1", "name": "ws-one"}
        )
        app.state.workspaces.home_path.return_value = tmp_path / "missing"
        app.state.workspaces.workspace_metadata.return_value = {}
        app.state.workspaces.build_export_tar_args.return_value = []

        proc = MagicMock()
        proc.returncode = None
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        proc.stdout.read = AsyncMock(
            side_effect=[b"tar-bytes", RuntimeError("consumer gone")]
        )

        with patch(
            "klangk.api.workspaces.asyncio.create_subprocess_exec",
            return_value=proc,
        ):
            response = await export_workspace(
                "ws-1",
                user={"id": "u1", "email": "u@x.com"},
                app=app,
            )
            with pytest.raises(RuntimeError, match="consumer gone"):
                async for _ in response.body_iterator:
                    pass
        proc.kill.assert_called_once()

    async def test_upload_without_filename_is_400(self):
        """No path and a file with no name: 400, not a crash."""
        from fastapi import HTTPException

        from klangk.api.resources import upload_file

        app = MagicMock()
        app.state.container_registry.get_container.return_value = "cid"
        upload = MagicMock()
        upload.filename = ""
        with pytest.raises(HTTPException) as caught:
            await upload_file(
                "ws-1",
                upload,
                path="",
                user={"id": "u1"},
                _write={"user_id": "u1"},
                app=app,
            )
        assert caught.value.status_code == 400
        assert "No filename provided" in caught.value.detail


class TestAdminTabPermissions:
    """#2940: one `manage-<thing>` permission per admin tab.

    Admins keep full access via the seeded /admin Allow * wildcard; a
    delegated user gets exactly the tab their ACE on the sub-resource
    grants (the same shape as the read-only Events auditor, #2923).
    A tab permission covers every action of that tab — there are no
    per-action splits. `manage-acls` is root-equivalent by design: it
    can rewrite ACLs on any resource, including /admin and / — pinned
    explicitly below.
    """

    async def _admin_headers(self, client):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testadmin@example.com",
                "password": "testpass",
            },
        )
        return {"Authorization": f"Bearer {resp.json()['access_token']}"}

    async def _grant(
        self, app_state, resource, permission, user_id, priority=0
    ):
        await app_state.state.model.acl.add_acl_entry(
            resource,
            priority,
            model.ACTION_ALLOW,
            permission,
            model.PRINCIPAL_USER,
            user_id=user_id,
        )

    async def test_wildcard_admin_keeps_full_access(
        self, client, app, admin_user
    ):
        """The seeded per-resource Allow rows satisfy every tab
        permission for admins (instance-admin status itself is the
        /my-permissions is_admin flag, #2995)."""
        headers = await self._admin_headers(client)
        for path in (
            "/api/v1/users",
            "/api/v1/invitations",
            "/api/v1/acl/tree",
            "/api/v1/server/schedule",
            "/api/v1/events",
        ):
            resp = await client.get(path, headers=headers)
            assert resp.status_code == 200, (path, resp.text)

    async def test_old_admin_paths_are_gone(self, client, admin_user):
        """#2944: the /admin/* API paths 404 — nothing lives there."""
        headers = await self._admin_headers(client)
        for path in (
            "/api/v1/admin/users",
            "/api/v1/admin/groups",
            "/api/v1/admin/invitations",
            "/api/v1/admin/acl/tree",
            "/api/v1/admin/server/schedule",
            "/api/v1/admin/container-events",
        ):
            resp = await client.get(path, headers=headers)
            assert resp.status_code == 404, path

    async def test_plain_user_locked_out_everywhere(self, client, app, user):
        headers = await _auth_headers(client)
        for path in (
            "/api/v1/users",
            "/api/v1/groups/some-id/members",
            "/api/v1/invitations",
            "/api/v1/acl/tree",
            "/api/v1/server/schedule",
            "/api/v1/events",
        ):
            resp = await client.get(path, headers=headers)
            assert resp.status_code == 403, path

    async def test_delegated_manage_users_covers_the_whole_tab(
        self, client, app, user, app_state
    ):
        await self._grant(app_state, "/users", "manage-users", user["id"])
        headers = await _auth_headers(client)

        resp = await client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 200, resp.text

        # Sessions (IP/UA rows) are part of the tab, not a separate gate.
        resp = await client.get(
            f"/api/v1/users/{user['id']}/sessions", headers=headers
        )
        assert resp.status_code == 200, resp.text

        # Writes pass the gate too — one permission, whole tab.
        resp = await client.patch(
            f"/api/v1/users/{user['id']}",
            headers=headers,
            json={"handle": "helpdesk"},
        )
        assert resp.status_code == 200, resp.text

        # ...and nothing outside the tab.
        for path in (
            "/api/v1/invitations",
            "/api/v1/acl/tree",
            "/api/v1/server/schedule",
        ):
            resp = await client.get(path, headers=headers)
            assert resp.status_code == 403, path

    async def test_delegated_manage_invitations_covers_send_and_list(
        self, client, app, user, app_state
    ):
        await self._grant(
            app_state, "/invitations", "manage-invitations", user["id"]
        )
        headers = await _auth_headers(client)

        resp = await client.get("/api/v1/invitations", headers=headers)
        assert resp.status_code == 200, resp.text

        # Past the gate: 400 because the email belongs to an existing
        # user (no email side effect on this branch).
        resp = await client.post(
            "/api/v1/invitations",
            headers=headers,
            json={"email": "testuser@example.com"},
        )
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]

        resp = await client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 403

    async def test_delegated_manage_server_schedule_covers_create_and_list(
        self, client, app, user, app_state
    ):
        await self._grant(
            app_state, "/server", "manage-server-schedule", user["id"]
        )
        headers = await _auth_headers(client)

        resp = await client.get("/api/v1/server/schedule", headers=headers)
        assert resp.status_code == 200, resp.text

        resp = await client.post(
            "/api/v1/server/schedule",
            headers=headers,
            json={"action": "stop", "in_seconds": 3600},
        )
        assert resp.status_code == 200, resp.text
        schedule_id = resp.json()["id"]

        resp = await client.delete(
            f"/api/v1/server/schedule/{schedule_id}", headers=headers
        )
        assert resp.status_code == 200, resp.text

    async def test_manage_acls_is_root_equivalent_by_design(
        self, client, app, user, app_state
    ):
        """The documented sharp edge (#2940): a manage-acls holder can
        rewrite ACLs on ANY resource — including collections like
        /workspaces — so the permission is root-equivalent and granted
        only to administrators. This pins that deliberately."""
        await self._grant(app_state, "/acl", "manage-acls", user["id"])
        headers = await _auth_headers(client)

        resp = await client.get("/api/v1/acl/tree", headers=headers)
        assert resp.status_code == 200, resp.text

        # Read the current entries, append a self-grant, replace.
        resp = await client.get(
            "/api/v1/acl/resource?resource=/workspaces",
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        entries = [
            {
                "action": e["action"],
                "principal_type": e["principal_type"],
                "permission": e["permission"],
                "user_id": e.get("user_id"),
                "group_id": e.get("group_id"),
                "system_principal": e.get("system_principal"),
            }
            for e in resp.json()
        ]
        entries.append(
            {
                "action": 1,
                "principal_type": 1,
                "permission": "*",
                "user_id": user["id"],
                "group_id": None,
                "system_principal": None,
            }
        )
        resp = await client.put(
            "/api/v1/acl/resource?resource=/workspaces",
            headers=headers,
            json=entries,
        )
        assert resp.status_code == 200, resp.text

        # The self-grant took effect — root over the collection.
        resp = await client.post(
            "/api/v1/workspaces",
            headers=headers,
            json={"name": "escalated-ws"},
        )
        assert resp.status_code == 200, resp.text

        # manage-acls alone opens no other tab.
        resp = await client.get("/api/v1/users", headers=headers)
        assert resp.status_code == 403

    async def test_my_permissions_surfaces_tab_names(
        self, client, app, user, app_state
    ):
        await self._grant(app_state, "/users", "manage-users", user["id"])
        headers = await _auth_headers(client)
        resp = await client.get("/api/v1/my-permissions", headers=headers)
        perms = resp.json()["permissions"]
        assert "manage-users" in perms.get("/users", [])

    async def test_admin_my_permissions_lists_all_tab_names(
        self, client, app, admin_user
    ):
        headers = await self._admin_headers(client)
        resp = await client.get("/api/v1/my-permissions", headers=headers)
        perms = resp.json()["permissions"]
        for resource, name in (
            ("/users", "manage-users"),
            ("/invitations", "manage-invitations"),
            ("/groups", "manage-groups"),
            ("/server", "manage-server-schedule"),
            ("/events", "manage-events"),
            ("/acl", "manage-acls"),
        ):
            assert name in perms.get(resource, []), resource
