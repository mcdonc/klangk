"""Tests for the web-session DPoP bind deadline (#3230).

A session minted for the web SPA (marked by the ``Klangk-Web-Client``
header on the minting request) carries a ``wbd`` claim — mint time plus
``KLANGKD_WEB_BIND_GRACE_SECONDS``. If the session never DPoP-binds
within that window, every later use (API request, refresh, bind,
WebSocket connect) is refused with 401 / close 4001. CLI/TUI mints are
unmarked and stay unbound indefinitely. The claim carries unchanged
across refresh and bind swaps, so a rotation can never reset it.
"""

import time
import types

import pytest

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from klangk import api
from klangk import auth as auth_mod
from klangk import dpop as dpop_mod
from klangk import oidc as oidc_mod
from klangk import util as util_mod
from klangk.main import register_exception_handlers
from klangk.util import API_PREFIX

from _helpers import make_binding_key, make_dpop_proof, make_settings

#: Headers marking a mint request as coming from the web SPA (#3230).
WEB_HEADERS = {auth_mod.WEB_CLIENT_HEADER: "1"}


def _standalone_auth(env=None):
    """A standalone Auth for token forging (same default secret as the
    app fixture, so tokens round-trip through app.state.auth.decode_*)."""
    from _helpers import wire_db_and_model

    state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=make_settings(env or {}))
    )
    wire_db_and_model(state)
    return auth_mod.Auth(state)


@pytest.fixture
async def app(db, temp_data_dir):
    """The auth router on a minimal FastAPI app (test_api's shape)."""
    application = FastAPI()
    application.state.settings = make_settings(
        env={
            "KLANGKD_AUTH_MODES": "password",
            "KLANGKD_DATA_DIR": str(temp_data_dir),
            "KLANGKD_CUSTOMIZE_DIR": str(temp_data_dir / "customize"),
        }
    )
    application.state.util = util_mod.Util(application)
    application.state.oidc = oidc_mod.OIDC(application)
    application.state.auth = auth_mod.Auth(application)
    from _helpers import wire_db_and_model

    wire_db_and_model(application)
    application.include_router(api.router, prefix=API_PREFIX)
    register_exception_handlers(application)
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _login(client, headers=None):
    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": "testuser@example.com", "password": "testpass"},
        headers=headers,
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestMintMarking:
    """The header marks the mint; the token carries the deadline."""

    async def test_web_login_carries_deadline(self, client, app, user):
        token = await _login(client, WEB_HEADERS)
        wbd = app.state.auth.decode_token(token)[auth_mod.BIND_DEADLINE_CLAIM]
        assert wbd == pytest.approx(time.time() + 300, abs=10)

    async def test_cli_login_carries_no_deadline(self, client, app, user):
        token = await _login(client)
        assert auth_mod.BIND_DEADLINE_CLAIM not in app.state.auth.decode_token(
            token
        )

    async def test_zero_grace_mints_no_deadline(self, user, db):
        """Grace 0 disables the deadline entirely (#3230's off switch)."""
        a = _standalone_auth({"KLANGKD_WEB_BIND_GRACE_SECONDS": "0"})
        token = await a.issue_token(user["id"], user["email"], web_client=True)
        assert auth_mod.BIND_DEADLINE_CLAIM not in a.decode_token(token)


class TestDeadlineEnforcementHTTP:
    """Unbound web-minted tokens past the deadline are refused."""

    async def test_past_deadline_rejected(self, client, user):
        token = _standalone_auth().create_token(
            user["id"], user["email"], web_deadline=time.time() - 10
        )
        resp = await client.get("/api/v1/auth/me", headers=_bearer(token))
        assert resp.status_code == 401
        assert resp.json()["detail"] == (
            "Session expired: DPoP binding required"
        )

    async def test_future_deadline_allowed(self, client, user):
        token = _standalone_auth().create_token(
            user["id"], user["email"], web_deadline=time.time() + 600
        )
        resp = await client.get("/api/v1/auth/me", headers=_bearer(token))
        assert resp.status_code == 200

    async def test_unmarked_token_allowed_forever(self, client, user):
        """The CLI/TUI posture: unmarked tokens never hit the gate."""
        token = _standalone_auth().create_token(user["id"], user["email"])
        resp = await client.get("/api/v1/auth/me", headers=_bearer(token))
        assert resp.status_code == 200

    async def test_bound_past_deadline_allowed_with_proof(self, client, user):
        """Binding is the cure: a bound token with a valid proof works
        regardless of the deadline."""
        private, jwk = make_binding_key()
        jkt = dpop_mod.jwk_thumbprint(jwk)
        token = _standalone_auth().create_token(
            user["id"],
            user["email"],
            jkt=jkt,
            web_deadline=time.time() - 10,
        )
        proof = make_dpop_proof(
            private,
            jwk,
            method="GET",
            uri="http://test/api/v1/auth/me",
            token=token,
        )
        resp = await client.get(
            "/api/v1/auth/me",
            headers={**_bearer(token), "DPoP": proof},
        )
        assert resp.status_code == 200

    async def test_bound_without_proof_rejected_as_dpop(self, client, user):
        """A bound token past deadline without a proof fails the DPoP
        check first — the deadline gate never even runs."""
        _, jwk = make_binding_key()
        jkt = dpop_mod.jwk_thumbprint(jwk)
        token = _standalone_auth().create_token(
            user["id"],
            user["email"],
            jkt=jkt,
            web_deadline=time.time() - 10,
        )
        resp = await client.get("/api/v1/auth/me", headers=_bearer(token))
        assert resp.status_code == 401
        assert "DPoP" in resp.json()["detail"]

    async def test_optional_dependency_rejects_too(self, client, user):
        """get_current_user_optional (the /config surface) raises 401 for
        a past-deadline token, mirroring the bad-proof posture — it is
        presented auth that is dead, not anonymous browsing."""
        token = _standalone_auth().create_token(
            user["id"], user["email"], web_deadline=time.time() - 10
        )
        resp = await client.get("/api/v1/config", headers=_bearer(token))
        assert resp.status_code == 401

    async def test_logout_still_works_past_deadline(self, client, user):
        """Logout is lenient: a dead session can still clean itself up."""
        token = _standalone_auth().create_token(
            user["id"], user["email"], web_deadline=time.time() - 10
        )
        resp = await client.post("/api/v1/auth/logout", headers=_bearer(token))
        assert resp.status_code == 200


class TestDeadlineEnforcementWS:
    """The WebSocket gate refuses past-deadline tokens with 4001."""

    @staticmethod
    def _ws_app_state():
        from test_wshandler import _make_app_state

        return _make_app_state()

    async def test_ws_authenticate_closes_past_deadline(self, user, db):
        from klangk.wshandler import ws_authenticate
        from test_wshandler import _mock_raw_sock

        app_state = self._ws_app_state()
        token = app_state.state.auth.create_token(
            user["id"], user["email"], web_deadline=time.time() - 10
        )
        websocket = _mock_raw_sock(
            headers={"sec-websocket-protocol": f"bearer, {token}"}
        )
        assert await ws_authenticate(websocket, app_state) is None
        websocket.close.assert_awaited_once_with(
            code=4001, reason="Invalid token"
        )

    async def test_ws_authenticate_allows_future_deadline(self, user, db):
        from klangk.wshandler import ws_authenticate
        from test_wshandler import _mock_raw_sock

        app_state = self._ws_app_state()
        token = app_state.state.auth.create_token(
            user["id"], user["email"], web_deadline=time.time() + 600
        )
        websocket = _mock_raw_sock(
            headers={"sec-websocket-protocol": f"bearer, {token}"}
        )
        result = await ws_authenticate(websocket, app_state)
        assert result is not None
        assert result[0]["id"] == user["id"]


class TestDeadlineEnforcementRefresh:
    """Refresh refuses past-deadline tokens; a within-grace refresh
    carries the deadline unchanged onto the replacement."""

    async def test_refresh_past_deadline_rejected(self, client, user):
        token = _standalone_auth().create_token(
            user["id"], user["email"], web_deadline=time.time() - 10
        )
        resp = await client.post(
            "/api/v1/auth/refresh", headers=_bearer(token)
        )
        assert resp.status_code == 401
        assert resp.json()["detail"] == (
            "Session expired: DPoP binding required"
        )

    async def test_refresh_carries_deadline_unchanged(self, client, app, user):
        """A rotation must never reset the clock: the replacement keeps
        the original deadline (the sabotage-resistance core — the
        attacker lets refreshes happen and the session still dies)."""
        wbd = time.time() + 120
        token = _standalone_auth().create_token(
            user["id"], user["email"], web_deadline=wbd
        )
        resp = await client.post(
            "/api/v1/auth/refresh", headers=_bearer(token)
        )
        assert resp.status_code == 200
        payload = app.state.auth.decode_token(resp.json()["access_token"])
        assert payload[auth_mod.BIND_DEADLINE_CLAIM] == wbd

    async def test_refresh_of_web_login_token_keeps_claim(
        self, client, app, user
    ):
        """The full mint→refresh chain keeps the deadline: login with
        the marker, refresh, assert the claim survives with its original
        value."""
        token = await _login(client, WEB_HEADERS)
        wbd = app.state.auth.decode_token(token)[auth_mod.BIND_DEADLINE_CLAIM]
        resp = await client.post(
            "/api/v1/auth/refresh", headers=_bearer(token)
        )
        assert resp.status_code == 200
        payload = app.state.auth.decode_token(resp.json()["access_token"])
        assert payload[auth_mod.BIND_DEADLINE_CLAIM] == wbd


class TestDeadlineEnforcementBind:
    """Bind is gated by the authenticated dependency, so a past-deadline
    token cannot resurrect itself by binding late; a within-grace bind
    keeps the deadline on the replacement."""

    async def test_bind_past_deadline_rejected(self, client, user):
        _, jwk = make_binding_key()
        token = _standalone_auth().create_token(
            user["id"], user["email"], web_deadline=time.time() - 10
        )
        resp = await client.post(
            "/api/v1/auth/bind", json={"jwk": jwk}, headers=_bearer(token)
        )
        assert resp.status_code == 401

    async def test_bind_within_grace_carries_deadline(self, client, app, user):
        _, jwk = make_binding_key()
        wbd = time.time() + 120
        token = _standalone_auth().create_token(
            user["id"], user["email"], web_deadline=wbd
        )
        resp = await client.post(
            "/api/v1/auth/bind", json={"jwk": jwk}, headers=_bearer(token)
        )
        assert resp.status_code == 200
        payload = app.state.auth.decode_token(resp.json()["access_token"])
        assert payload["cnf"]["jkt"] == dpop_mod.jwk_thumbprint(jwk)
        assert payload[auth_mod.BIND_DEADLINE_CLAIM] == wbd


class TestSabotageScenario:
    """The #3230 attack end to end: the SPA mints (marked), an in-page
    script reads the JWT and sabotages every bind call — the session
    still dies at the deadline across every surface."""

    async def test_never_bound_session_dies_at_deadline(
        self, client, app, user, monkeypatch
    ):
        token = await _login(client, WEB_HEADERS)
        wbd = app.state.auth.decode_token(token)[auth_mod.BIND_DEADLINE_CLAIM]
        # Works while inside the grace window (bind sabotaged, unused).
        resp = await client.get("/api/v1/auth/me", headers=_bearer(token))
        assert resp.status_code == 200
        # Time passes the baked-in deadline.
        real_time = time.time
        monkeypatch.setattr(
            auth_mod.time, "time", lambda: wbd + 1, raising=True
        )
        assert real_time() < wbd + 1  # sanity: the patch moves us past
        # Every surface refuses the still-unbound token.
        resp = await client.get("/api/v1/auth/me", headers=_bearer(token))
        assert resp.status_code == 401
        resp = await client.post(
            "/api/v1/auth/refresh", headers=_bearer(token)
        )
        assert resp.status_code == 401
        _, jwk = make_binding_key()
        resp = await client.post(
            "/api/v1/auth/bind", json={"jwk": jwk}, headers=_bearer(token)
        )
        assert resp.status_code == 401


class TestBindDeadlineExpiryUnit:
    """Branch coverage for the boolean gate."""

    def test_bound_token_never_deadline_expired(self):
        a = _standalone_auth()
        payload = {"cnf": {"jkt": "x"}, "wbd": time.time() - 100}
        assert a.bind_deadline_expired(payload) is False

    def test_non_numeric_deadline_ignored(self):
        a = _standalone_auth()
        assert a.bind_deadline_expired({"wbd": "soon"}) is False

    def test_missing_deadline_ignored(self):
        a = _standalone_auth()
        assert a.bind_deadline_expired({}) is False

    def test_enforce_raises_past_deadline(self):
        a = _standalone_auth()
        with pytest.raises(Exception) as exc_info:
            a.enforce_bind_deadline({"wbd": time.time() - 1})
        assert exc_info.value.status_code == 401
