"""Tests for step-up (sudo-mode) reauthentication (#3196).

Covers :mod:`klangk.stepup` (the gate + its helpers), the
``POST /auth/step-up`` endpoint, the gate on the admin write surface,
the conditional non-owner workspace-deletion gate, the session-stamp
model (including the refresh carry-over), and the migration that adds
the column.
"""

import asyncio
import types

import aiosqlite
import pytest
from unittest.mock import AsyncMock

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from klangk import (
    api,
    auth as auth_mod,
    stepup,
)
from klangk.util import API_PREFIX
from _helpers import make_settings, wire_db_and_model

TEST_PASSWORD = "testpass"


def _auth():
    """A standalone Auth for token forging (same default secret as the
    app fixture, so tokens round-trip through app.state.auth.decode_*)."""
    state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=make_settings({}))
    )
    wire_db_and_model(state)
    return auth_mod.Auth(state)


@pytest.fixture
async def app(db, temp_data_dir):
    """A minimal FastAPI app with the API router (mirrors test_api)."""
    app = FastAPI()
    from klangk.main import register_exception_handlers
    from klangk import emailsvc as emailsvc_mod
    from klangk import features as features_mod
    from klangk import netfilter as netfilter_mod
    from klangk import nix as nix_mod
    from klangk import oidc as oidc_mod
    from klangk import terminal as terminal_mod
    from klangk import util as util_mod
    from klangk import workspaces as ws_mod
    from klangk import hooks as hooks_mod
    from klangk import files as files_mod
    from klangk.container import ContainerRegistry
    from klangk.wshandler.session import WebSocketState
    from klangk import server_schedule as server_schedule_mod
    from unittest.mock import MagicMock

    mock_pod = MagicMock()
    mock_pod.list_volumes = AsyncMock(return_value=[])
    mock_pod.remove_volume = AsyncMock()
    settings = make_settings(
        env={
            "KLANGKD_AUTH_MODES": "password",
            "KLANGKD_DATA_DIR": str(temp_data_dir),
            "KLANGKD_CUSTOMIZE_DIR": str(temp_data_dir / "customize"),
        }
    )
    app.state.settings = settings
    app.state.podman = mock_pod
    app.state.sockets = WebSocketState(app)
    app.state.container_registry = ContainerRegistry(app)
    app.state.oidc = oidc_mod.OIDC(app)
    app.state.features = features_mod.Features(app)
    app.state.workspaces = ws_mod.Workspaces(app)
    app.state.hooks = hooks_mod.Hooks(app)
    app.state.files = files_mod.Files(app)
    app.state.email = emailsvc_mod.EmailService(app)
    app.state.util = util_mod.Util(app)
    app.state.netfilter = netfilter_mod.NetFilter(app)
    app.state.nix = nix_mod.Nix(app)
    app.state.auth = auth_mod.Auth(app)
    app.state.terminal = terminal_mod.Terminal(app)
    app.state.server_scheduler = server_schedule_mod.ServerScheduler(app)
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
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _login(client, email="testadmin@example.com"):
    """Log in and return auth headers (a session row is created)."""
    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _arm(app, minutes=15):
    app.state.settings.step_up_window_minutes = minutes


def _is_step_up(resp) -> bool:
    detail = resp.json().get("detail")
    return (
        resp.status_code == 403
        and isinstance(detail, dict)
        and detail.get("error") == stepup.STEP_UP_REQUIRED
    )


async def _step_up(client, headers, password=TEST_PASSWORD):
    return await client.post(
        "/api/v1/auth/step-up", headers=headers, json={"password": password}
    )


async def _write(client, headers):
    """One gated admin write (create a group)."""
    return await client.post(
        "/api/v1/groups", headers=headers, json={"name": "step-up-probe"}
    )


class TestGateOnAdminWrites:
    async def test_disabled_window_write_passes(self, client, admin_user, app):
        """Default (0): the gate is off — writes pass on the bearer
        token alone, exactly as before #3196."""
        headers = await _login(client)
        resp = await _write(client, headers)
        assert resp.status_code == 200

    async def test_armed_window_write_refused(self, client, admin_user, app):
        _arm(app)
        headers = await _login(client)
        resp = await _write(client, headers)
        assert _is_step_up(resp)

    async def test_armed_window_read_passes(self, client, admin_user, app):
        """Reads are never gated — listings stay on the permission
        check."""
        _arm(app)
        headers = await _login(client)
        resp = await client.get("/api/v1/users?page_size=5", headers=headers)
        assert resp.status_code == 200

    async def test_permission_denied_precedes_step_up(self, client, user, app):
        """A non-admin gets the plain permission 403, not the step-up
        error (the ACL dependency runs first)."""
        _arm(app)
        headers = await _login(client, "testuser@example.com")
        resp = await _write(client, headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Permission denied"

    async def test_step_up_unlocks_write(self, client, admin_user, app):
        _arm(app)
        headers = await _login(client)
        assert _is_step_up(await _write(client, headers))
        resp = await _step_up(client, headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "stepped_up"
        resp = await _write(client, headers)
        assert resp.status_code == 200

    async def test_gated_surfaces(self, client, admin_user, app, user):
        """Every privileged write surface carries the gate: users,
        invitations, groups, ACL replace, server schedules. (Volumes
        delete needs podman state; its dependency wiring is identical
        and covered by the module-level dependency test below.)"""
        _arm(app)
        headers = await _login(client)
        acl_put = client.put(
            "/api/v1/acl/resource",
            params={"resource": "/users"},
            headers=headers,
            json=[],
        )
        cases = (
            client.post(
                "/api/v1/users",
                headers=headers,
                json={"email": "newuser@example.com", "password": "Pw123456"},
            ),
            client.patch(
                f"/api/v1/users/{user['id']}",
                headers=headers,
                json={"handle": "handled"},
            ),
            client.delete(f"/api/v1/users/{user['id']}", headers=headers),
            client.post(
                f"/api/v1/users/{user['id']}/unlockout", headers=headers
            ),
            client.post(
                "/api/v1/invitations",
                headers=headers,
                json={"email": "invite@example.com"},
            ),
            client.patch(
                "/api/v1/groups/some-id",
                headers=headers,
                json={"name": "renamed"},
            ),
            client.delete("/api/v1/groups/some-id", headers=headers),
            client.post(
                "/api/v1/groups/some-id/members",
                headers=headers,
                json={"user_id": user["id"]},
            ),
            client.delete(
                f"/api/v1/groups/some-id/members/{user['id']}",
                headers=headers,
            ),
            client.post(
                "/api/v1/server/schedule",
                headers=headers,
                json={"action": "stop", "in_seconds": 3600},
            ),
            client.delete("/api/v1/server/schedule/some-id", headers=headers),
            acl_put,
        )
        for resp in await asyncio.gather(*cases):
            assert _is_step_up(resp), (resp.request.url, resp.text)

    async def test_volume_delete_gated(self, client, admin_user, app):
        """DELETE /volumes/{name} carries the dependency (checked before
        the podman reachability — the step-up 403 fires first)."""
        from unittest.mock import MagicMock

        _arm(app)
        headers = await _login(client)
        app.state.podman = MagicMock(inspect_volume=AsyncMock())
        resp = await client.delete("/api/v1/volumes/somevol", headers=headers)
        assert _is_step_up(resp)


class TestStepUpEndpoint:
    async def test_step_up_requires_auth(self, client, admin_user, app):
        _arm(app)
        resp = await client.post(
            "/api/v1/auth/step-up", json={"password": TEST_PASSWORD}
        )
        assert resp.status_code == 401

    async def test_step_up_disabled_400(self, client, admin_user, app):
        """Window off: there is nothing to confirm — an explicit 400,
        not a silent stamp."""
        headers = await _login(client)
        resp = await _step_up(client, headers)
        assert resp.status_code == 400
        assert "not enabled" in resp.json()["detail"]

    async def test_wrong_password_401(self, client, admin_user, app):
        _arm(app)
        headers = await _login(client)
        resp = await _step_up(client, headers, password="wrong")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid credentials"

    async def test_wrong_password_counts_toward_lockout(
        self, client, admin_user, app, app_state
    ):
        """The endpoint is a password-guessing oracle for a session
        holder; failures ride the login lockout (#3196)."""
        app.state.settings.login_lockout_failures = 2
        _arm(app)
        headers = await _login(client)
        await _step_up(client, headers, password="wrong")
        resp = await _step_up(client, headers, password="wrong")
        assert resp.status_code == 429

    async def test_success_clears_failures(
        self, client, admin_user, app, app_state
    ):
        app.state.settings.login_lockout_failures = 2
        _arm(app)
        headers = await _login(client)
        await _step_up(client, headers, password="wrong")
        resp = await _step_up(client, headers)
        assert resp.status_code == 200
        # A later failure starts from a clean count (not a lockout).
        resp = await _step_up(client, headers, password="wrong")
        assert resp.status_code == 401

    async def test_no_session_row_401(self, client, admin_user, app):
        """A token without a session row cannot be stamped — 401, and
        the gate fails closed for such tokens."""
        _arm(app)
        forged = _auth().create_token(admin_user["id"], admin_user["email"])
        headers = {"Authorization": f"Bearer {forged}"}
        resp = await _step_up(client, headers)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Session not found"
        assert _is_step_up(await _write(client, headers))

    async def test_oidc_managed_403(self, client, app, admin_group, app_state):
        """OIDC-only accounts cannot confirm a password — the same
        clear 403 as the change-password flow."""
        user = await app_state.state.model.users.create_user(
            "oidc-admin@example.com", None, verified=True, provider="oidc"
        )
        await app_state.state.model.users.add_user_to_group(
            user["id"], admin_group["id"]
        )
        _arm(app)
        forged = _auth().create_token(user["id"], user["email"])
        headers = {"Authorization": f"Bearer {forged}"}
        resp = await _step_up(client, headers)
        assert resp.status_code == 403
        assert "identity provider" in resp.json()["detail"]


class TestStampLifetime:
    async def test_expiry(self, client, admin_user, app, app_state):
        """A stamp older than the window no longer satisfies the
        gate."""
        from datetime import datetime, timedelta, timezone

        _arm(app, minutes=15)
        headers = await _login(client)
        assert (await _step_up(client, headers)).status_code == 200
        old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
        async with app_state.state.db.transaction() as tx:
            await tx.execute(
                "UPDATE user_sessions SET stepped_up_at = ?", (old,)
            )
        assert _is_step_up(await _write(client, headers))

    async def test_refresh_carries_stamp(self, client, admin_user, app):
        """A refresh re-keys the session row; the confirmation must
        survive it (a refresh is the same session, not a new login)."""
        _arm(app)
        headers = await _login(client)
        assert (await _step_up(client, headers)).status_code == 200
        resp = await client.post("/api/v1/auth/refresh", headers=headers)
        assert resp.status_code == 200
        new_headers = {
            "Authorization": f"Bearer {resp.json()['access_token']}"
        }
        resp = await _write(client, new_headers)
        assert resp.status_code == 200

    async def test_stamp_is_per_session(self, client, admin_user, app):
        """A confirmation on one session never unlocks another session
        of the same user."""
        _arm(app)
        first = await _login(client)
        second = await _login(client)
        assert (await _step_up(client, first)).status_code == 200
        assert _is_step_up(await _write(client, second))

    async def test_new_login_starts_unstamped(self, client, admin_user, app):
        """Logout ends the elevated state with the session; the next
        login needs a fresh confirmation."""
        _arm(app)
        headers = await _login(client)
        assert (await _step_up(client, headers)).status_code == 200
        await client.post("/api/v1/auth/logout", headers=headers)
        fresh = await _login(client)
        assert _is_step_up(await _write(client, fresh))


class TestWorkspaceDeleteGate:
    async def _make_workspace(self, client, headers, name):
        resp = await client.post(
            "/api/v1/workspaces", headers=headers, json={"name": name}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    async def test_owner_delete_ungated(self, client, app, ws_admin, registry):
        """Deleting your own workspace is self-service: no step-up even
        when armed."""
        _arm(app)
        headers = await _login(client, "testuser@example.com")
        ws_id = await self._make_workspace(client, headers, "own-ws")
        with patch_stop(registry):
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}", headers=headers
            )
        assert resp.status_code == 200

    async def test_non_owner_delete_gated(
        self, client, app, ws_admin, admin_user, registry, app_state
    ):
        """An admin deleting another user's workspace is a privileged
        cross-principal write (#3196)."""
        from klangk.model import (
            ACTION_ALLOW,
            PRINCIPAL_USER,
        )

        _arm(app)
        owner_headers = await _login(client, "testuser@example.com")
        ws_id = await self._make_workspace(client, owner_headers, "victim")
        # Grant the admin delete rights on the workspace (the stock
        # seed gives admins no cross-workspace delete; the owner's own
        # wildcard is what usually satisfies this permission).
        await app_state.state.model.acl.add_acl_entry(
            f"/workspaces/{ws_id}",
            100,
            ACTION_ALLOW,
            "delete-workspace",
            PRINCIPAL_USER,
            user_id=admin_user["id"],
        )
        admin_headers = await _login(client)
        resp = await client.delete(
            f"/api/v1/workspaces/{ws_id}", headers=admin_headers
        )
        assert _is_step_up(resp)
        assert (await _step_up(client, admin_headers)).status_code == 200
        with patch_stop(registry):
            resp = await client.delete(
                f"/api/v1/workspaces/{ws_id}", headers=admin_headers
            )
        assert resp.status_code == 200


def patch_stop(registry):
    from unittest.mock import patch

    return patch.object(
        registry, "stop_and_remove_container", new_callable=AsyncMock
    )


class TestOidcExemption:
    async def test_oidc_admin_exempt(
        self, client, app, admin_group, app_state
    ):
        """OIDC-managed accounts have no password to confirm; the gate
        exempts them (a blocked write would lock OIDC deployments out
        of administration entirely)."""
        user = await app_state.state.model.users.create_user(
            "oidc-admin2@example.com", None, verified=True, provider="oidc"
        )
        await app_state.state.model.users.add_user_to_group(
            user["id"], admin_group["id"]
        )
        _arm(app)
        forged = _auth().create_token(user["id"], user["email"])
        headers = {"Authorization": f"Bearer {forged}"}
        resp = await _write(client, headers)
        assert resp.status_code == 200


class TestConfigExposure:
    async def test_authenticated_config_exposes_window(
        self, client, admin_user, app
    ):
        _arm(app, minutes=15)
        headers = await _login(client)
        resp = await client.get("/api/v1/config", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["step_up_window_minutes"] == 15

    async def test_anonymous_config_hides_window(
        self, client, admin_user, app
    ):
        """The window reveals auth-hardening posture — authenticated
        payload only (same posture as the netfilter fields)."""
        _arm(app, minutes=15)
        resp = await client.get("/api/v1/config")
        assert resp.status_code == 200
        assert "step_up_window_minutes" not in resp.json()


class TestHelpers:
    """Unit tests for the pure-ish helpers in klangk.stepup."""

    def _request(self, authorization="Bearer goodtoken", app=None):
        app = app or types.SimpleNamespace(
            state=types.SimpleNamespace(
                auth=types.SimpleNamespace(
                    decode_token=lambda tok: {"jti": "jti-1"}
                )
            )
        )
        return types.SimpleNamespace(
            app=app,
            headers={"authorization": authorization} if authorization else {},
        )

    def test_step_up_required_error_shape(self):
        err = stepup.step_up_required_error()
        assert err.status_code == 403
        assert err.detail["error"] == stepup.STEP_UP_REQUIRED
        assert "message" in err.detail

    def test_jti_from_request_valid(self):
        req = self._request("Bearer goodtoken")
        assert stepup.jti_from_request(req.app, req) == "jti-1"

    def test_jti_from_request_lowercase_scheme(self):
        req = self._request("bearer goodtoken")
        assert stepup.jti_from_request(req.app, req) == "jti-1"

    def test_jti_from_request_missing_header(self):
        req = self._request(None)
        assert stepup.jti_from_request(req.app, req) is None

    def test_jti_from_request_wrong_scheme(self):
        req = self._request("Basic zzz")
        assert stepup.jti_from_request(req.app, req) is None

    def test_jti_from_request_undecodable_token(self):
        from jose import JWTError

        def boom(tok):
            raise JWTError("no")

        app = types.SimpleNamespace(
            state=types.SimpleNamespace(
                auth=types.SimpleNamespace(decode_token=boom)
            )
        )
        req = types.SimpleNamespace(
            app=app, headers={"authorization": "Bearer x"}
        )
        assert stepup.jti_from_request(app, req) is None

    async def test_stepped_up_within_jti_none(self, app_state):
        assert not await stepup.stepped_up_within(app_state, None, 15)

    async def test_stepped_up_within_malformed_stamp(
        self, app_state, db, user
    ):
        from datetime import datetime, timezone

        await app_state.state.model.sessions.record_session(
            user["id"], "jti-mal", "2099-01-01T00:00:00+00:00"
        )
        # NULL stamp (never confirmed) — not stepped up.
        assert not await stepup.stepped_up_within(app_state, "jti-mal", 15)
        async with app_state.state.db.transaction() as tx:
            await tx.execute(
                "UPDATE user_sessions SET stepped_up_at = 'not-a-date'"
                " WHERE jti = 'jti-mal'"
            )
        # A corrupt stamp is treated as never stepped up (fail closed).
        assert not await stepup.stepped_up_within(app_state, "jti-mal", 15)
        # A fresh naive (legacy SQLite form) stamp is judged as UTC.
        async with app_state.state.db.transaction() as tx:
            await tx.execute(
                "UPDATE user_sessions SET stepped_up_at = ?"
                " WHERE jti = 'jti-mal'",
                (
                    datetime.now(timezone.utc)
                    .replace(tzinfo=None)
                    .isoformat(" "),
                ),
            )
        assert await stepup.stepped_up_within(app_state, "jti-mal", 15)

    async def test_ensure_step_up_disabled_noop(self, app_state, user):
        """Window off: never raises, whatever the session state."""
        req = self._request(app=app_state)
        await stepup.ensure_step_up(req, user, None)


class TestSessionsModel:
    async def test_stamp_unknown_jti(self, app_state, db):
        stamped = await app_state.state.model.sessions.stamp_step_up(
            "no-such-jti"
        )
        assert stamped is False

    async def test_stamp_and_read(self, app_state, db, user):
        sessions = app_state.state.model.sessions
        await sessions.record_session(
            user["id"], "jti-a", "2099-01-01T00:00:00+00:00"
        )
        assert await sessions.get_stepped_up_at("jti-a") is None
        assert await sessions.stamp_step_up("jti-a") is True
        assert await sessions.get_stepped_up_at("jti-a") is not None

    async def test_replace_carries_stamp(self, app_state, db, user):
        sessions = app_state.state.model.sessions
        await sessions.record_session(
            user["id"], "jti-old", "2099-01-01T00:00:00+00:00"
        )
        await sessions.stamp_step_up("jti-old")
        await sessions.replace_session(
            "jti-old", user["id"], "jti-new", "2099-01-02T00:00:00+00:00"
        )
        assert await sessions.get_stepped_up_at("jti-new") is not None
        assert await sessions.get_stepped_up_at("jti-old") is None

    async def test_replace_inserts_unstamped_row(self, app_state, db, user):
        """The no-row INSERT path (a pre-#2585 token) creates a fresh,
        unstepped session — no retroactive credit."""
        sessions = app_state.state.model.sessions
        await sessions.replace_session(
            "jti-none", user["id"], "jti-fresh", "2099-01-02T00:00:00+00:00"
        )
        assert await sessions.get_stepped_up_at("jti-fresh") is None


class TestSettings:
    def test_default_off(self):
        s = make_settings({})
        assert s.step_up_window_minutes == 0

    def test_env_armed(self):
        s = make_settings({"KLANGKD_STEP_UP_WINDOW_MINUTES": "15"})
        assert s.step_up_window_minutes == 15

    def test_negative_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            make_settings({"KLANGKD_STEP_UP_WINDOW_MINUTES": "-1"})


class TestMigrationM0035:
    async def _old_shape_db(self, tmp_path):
        db = aiosqlite.connect(str(tmp_path / "m0035.db"))
        await db.__aenter__()
        await db.execute("""
            CREATE TABLE user_sessions (
                jti TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                expires_at TEXT NOT NULL,
                last_seen_at TEXT,
                session_id TEXT
            )
        """)
        await db.execute(
            "INSERT INTO user_sessions (jti, user_id, expires_at)"
            " VALUES ('jti-a', 'u1', '2099-01-01T00:00:00+00:00')"
        )
        await db.commit()
        return db

    async def test_adds_column_no_backfill(self, tmp_path):
        """Existing rows stay NULL — arming the feature must not
        retroactively credit sessions with a confirmation they never
        made."""
        db = await self._old_shape_db(tmp_path)
        try:
            from klangk.model.migrations import m0035_user_sessions_step_up

            await m0035_user_sessions_step_up.migration.apply(db)
            info = await db.execute("PRAGMA table_info(user_sessions)")
            cols = {r[1] for r in await info.fetchall()}
            assert "stepped_up_at" in cols
            cursor = await db.execute(
                "SELECT stepped_up_at FROM user_sessions WHERE jti = 'jti-a'"
            )
            assert await cursor.fetchone() == (None,)
        finally:
            await db.__aexit__(None, None, None)
