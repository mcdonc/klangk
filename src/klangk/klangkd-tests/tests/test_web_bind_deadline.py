"""Tests for the web-session DPoP bind deadline (#3230).

Sessions minted for the web SPA are **born bound**: the SPA marks its
minting requests with the ``Klangk-Web-Client`` header and ships its
public binding JWK alongside (``Klangk-Binding-Jwk``, base64url compact
JSON), so the minted token carries ``cnf.jkt`` from the first byte plus
a ``wbd`` bind deadline — mint time plus
``KLANGKD_WEB_BIND_GRACE_SECONDS``. There is no unbound window to read,
sabotage, or bind-first with a substituted key. The deadline is the
backstop for the paths that can still mint unbound (an OIDC web login
whose navigation carried no binding key, or a stripped request): such a
session, still unbound past the deadline, is refused everywhere — API
request, refresh, bind, WebSocket connect. CLI/TUI mints are unmarked
and stay unbound indefinitely. The deadline claim carries unchanged
across refresh and bind swaps, so a rotation can never reset it, and a
WebSocket opened inside the window closes at the deadline, not at the
token's natural expiry.
"""

import base64
import json
import time
import types
from unittest.mock import AsyncMock

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


def _standalone_auth(env=None):
    """A standalone Auth for token forging (same default secret as the
    app fixture, so tokens round-trip through app.state.auth.decode_*)."""
    from _helpers import wire_db_and_model

    state = types.SimpleNamespace(
        state=types.SimpleNamespace(settings=make_settings(env or {}))
    )
    wire_db_and_model(state)
    return auth_mod.Auth(state)


def _b64url(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def _mint_headers():
    """(headers, jwk, private) marking a mint as a web SPA bind."""
    private, jwk = make_binding_key()
    headers = {
        auth_mod.WEB_CLIENT_HEADER: "1",
        auth_mod.BINDING_JWK_HEADER: _b64url(json.dumps(jwk)),
    }
    return headers, jwk, private


@pytest.fixture
async def make_app(db, temp_data_dir):
    """Factory for the auth router on a minimal FastAPI app (test_api's
    shape), with per-test env overrides."""

    def _make(env: dict | None = None) -> FastAPI:
        settings_env = {
            "KLANGKD_AUTH_MODES": "password",
            "KLANGKD_DATA_DIR": str(temp_data_dir),
            "KLANGKD_CUSTOMIZE_DIR": str(temp_data_dir / "customize"),
        }
        settings_env.update(env or {})
        application = FastAPI()
        application.state.settings = make_settings(env=settings_env)
        application.state.util = util_mod.Util(application)
        application.state.oidc = oidc_mod.OIDC(application)
        application.state.auth = auth_mod.Auth(application)
        from _helpers import wire_db_and_model

        wire_db_and_model(application)
        application.include_router(api.router, prefix=API_PREFIX)
        register_exception_handlers(application)
        return application

    return _make


@pytest.fixture
async def app(make_app):
    return make_app()


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
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestMintMarking:
    """A marked mint is born bound with a deadline; an unmarked one is
    exactly the pre-#3230 CLI token."""

    async def test_web_login_mints_born_bound_with_deadline(
        self, client, app, user
    ):
        headers, jwk, _ = _mint_headers()
        token = await _login(client, headers)
        payload = app.state.auth.decode_token(token)
        assert payload["cnf"]["jkt"] == dpop_mod.jwk_thumbprint(jwk)
        assert payload[auth_mod.BIND_DEADLINE_CLAIM] == pytest.approx(
            time.time() + 300, abs=10
        )

    async def test_cli_login_mints_plain_token(self, client, app, user):
        token = await _login(client)
        payload = app.state.auth.decode_token(token)
        assert "cnf" not in payload
        assert auth_mod.BIND_DEADLINE_CLAIM not in payload

    async def test_marker_without_binding_key_rejected(self, client, user):
        """The marker without a usable key is a 400 — a page script
        cannot strip the key from the SPA's request and receive a
        merely-deadline-limited token."""
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
            headers={auth_mod.WEB_CLIENT_HEADER: "1"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid binding key"

    async def test_marker_with_garbage_binding_key_rejected(
        self, client, user
    ):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
            headers={
                auth_mod.WEB_CLIENT_HEADER: "1",
                auth_mod.BINDING_JWK_HEADER: "!!!not-base64!!!",
            },
        )
        assert resp.status_code == 400

    async def test_marker_with_symmetric_key_rejected(self, client, user):
        """A JWK carrying private material ('d') is refused, exactly like
        /auth/bind."""
        _, jwk, _ = _mint_headers()
        jwk = {**jwk, "d": "private-material"}
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
            headers={
                auth_mod.WEB_CLIENT_HEADER: "1",
                auth_mod.BINDING_JWK_HEADER: _b64url(json.dumps(jwk)),
            },
        )
        assert resp.status_code == 400

    async def test_marker_with_non_string_coordinates_rejected(
        self, client, user
    ):
        """#3230 round-3: numeric coordinates are not a usable key — the
        mint is refused rather than binding a token nobody can prove."""
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "identifier": "testuser@example.com",
                "password": "testpass",
            },
            headers={
                auth_mod.WEB_CLIENT_HEADER: "1",
                auth_mod.BINDING_JWK_HEADER: _b64url(
                    json.dumps({"kty": "EC", "crv": "P-256", "x": 1, "y": 2})
                ),
            },
        )
        assert resp.status_code == 400

    async def test_zero_grace_mints_deadline_only(self, user, db):
        """Grace 0 keeps born-bound minting but drops the deadline."""
        a = _standalone_auth({"KLANGKD_WEB_BIND_GRACE_SECONDS": "0"})
        token = await a.issue_token(
            user["id"], user["email"], web_client=True, jkt="jkt-x"
        )
        payload = a.decode_token(token)
        assert payload["cnf"]["jkt"] == "jkt-x"
        assert auth_mod.BIND_DEADLINE_CLAIM not in payload


class TestEveryMintPathBornBound:
    """Table-driven pin: every SPA-reachable mint endpoint threads the
    marker + binding key into a born-bound, deadline-limited token
    (#3230 review — dropping the threading on any route would silently
    reopen the window on that flow)."""

    async def _assert_marked(self, client, app, token, jwk):
        payload = app.state.auth.decode_token(token)
        assert payload["cnf"]["jkt"] == dpop_mod.jwk_thumbprint(jwk)
        assert auth_mod.BIND_DEADLINE_CLAIM in payload

    async def test_verify_mint(self, client, app, db):
        headers, jwk, _ = _mint_headers()
        unverified = await app.state.model.users.create_user(
            "verify-mint@example.com", "pw-hash", verified=False
        )
        token = app.state.auth.create_verification_token(
            unverified["id"], unverified["email"]
        )
        resp = await client.post(
            "/api/v1/auth/verify",
            json={"token": token},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await self._assert_marked(
            client, app, resp.json()["access_token"], jwk
        )

    async def test_reset_password_mint(self, client, app, user):
        headers, jwk, _ = _mint_headers()
        stored = await app.state.model.users.get_user_by_email(user["email"])
        token = app.state.auth.create_password_reset_token(
            stored["id"],
            app.state.auth.reset_token_binding(stored["password_hash"]),
        )
        resp = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": token, "password": "NewPassword123!"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await self._assert_marked(
            client, app, resp.json()["access_token"], jwk
        )

    async def test_accept_invite_mint(self, client, app, user):
        headers, jwk, _ = _mint_headers()
        invitation = await app.state.model.invitations.create_invitation(
            "invited@example.com", user["id"]
        )
        token = app.state.auth.create_invitation_token(
            invitation["id"], "invited@example.com"
        )
        resp = await client.post(
            "/api/v1/auth/accept-invite",
            json={"token": token, "password": "NewPassword123!"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        await self._assert_marked(
            client, app, resp.json()["access_token"], jwk
        )

    async def test_local_mint(self, make_app, db, temp_data_dir):
        app = make_app(
            {
                "KLANGKD_AUTH_MODES": "none",
                "KLANGKD_DEFAULT_USER": "local@example.com",
            }
        )
        await app.state.model.users.create_user(
            "local@example.com", "pw-hash", verified=True
        )
        headers, jwk, _ = _mint_headers()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as c:
            resp = await c.post("/api/v1/auth/local", headers=headers)
        assert resp.status_code == 200, resp.text
        payload = app.state.auth.decode_token(resp.json()["access_token"])
        assert payload["cnf"]["jkt"] == dpop_mod.jwk_thumbprint(jwk)
        assert auth_mod.BIND_DEADLINE_CLAIM in payload

    async def test_register_mint(self, make_app, db):
        app = make_app({"KLANGKD_TEST_MODE": "1"})
        headers, jwk, _ = _mint_headers()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            resp = await c.post(
                "/api/v1/auth/register",
                json={"email": "new@example.com", "password": "testpass"},
                headers=headers,
            )
        assert resp.status_code == 200, resp.text
        payload = app.state.auth.decode_token(resp.json()["access_token"])
        assert payload["cnf"]["jkt"] == dpop_mod.jwk_thumbprint(jwk)
        assert auth_mod.BIND_DEADLINE_CLAIM in payload


class TestBornBoundKillsBindRace:
    """The #3230 review's blocking finding: a page script reading the
    fresh JWT could previously bind it to its OWN key (first-bind-wins)
    and keep a deadline-immune session. With born-bound mints the token
    is never bindable — 409, exactly like any already-bound token."""

    async def test_attacker_cannot_bind_a_born_bound_token(self, client, user):
        """The attacker holds the token but not the SPA's key: every
        bind attempt fails the proof gate (401) before the already-bound
        409 — the token is unusable off-page."""
        headers, _, _ = _mint_headers()
        token = await _login(client, headers)
        _, attacker_jwk = make_binding_key()
        resp = await client.post(
            "/api/v1/auth/bind",
            json={"jwk": attacker_jwk},
            headers=_bearer(token),
        )
        assert resp.status_code == 401
        assert "DPoP" in resp.json()["detail"]

    async def test_born_bound_session_works_with_proofs(self, client, user):
        """The legitimate holder keeps working: proofs from the minted
        key pass forever — the deadline never applies to a bound token."""
        headers, jwk, private = _mint_headers()
        token = await _login(client, headers)
        proof = make_dpop_proof(
            private,
            jwk,
            method="GET",
            uri="http://test/api/v1/auth/me",
            token=token,
        )
        resp = await client.get(
            "/api/v1/auth/me", headers={**_bearer(token), "DPoP": proof}
        )
        assert resp.status_code == 200


class TestDeadlineEnforcementHTTP:
    """Unbound web-minted tokens past the deadline are refused (the
    backstop for the flows that can still mint unbound)."""

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
    """The WebSocket gate refuses past-deadline tokens with 4001, and a
    socket opened inside the grace window is armed to close at the
    deadline — not at the token's natural expiry (#3230 review F2)."""

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

    async def test_ws_expiry_arms_at_deadline_when_sooner(self, user, db):
        a = self._ws_app_state().state.auth
        wbd = time.time() + 60
        token = a.create_token(user["id"], user["email"], web_deadline=wbd)
        assert a.ws_expiry(a.decode_token(token)) == wbd

    async def test_ws_expiry_is_exp_when_bound(self, user, db):
        """A bound token's socket closes at exp only — the deadline
        never applies to it."""
        a = self._ws_app_state().state.auth
        token = a.create_token(
            user["id"],
            user["email"],
            jkt="jkt-x",
            web_deadline=time.time() - 10,
        )
        exp = a.decode_token(token)["exp"]
        assert a.ws_expiry(a.decode_token(token)) == exp

    async def test_ws_expiry_is_exp_when_unmarked(self, user, db):
        a = self._ws_app_state().state.auth
        token = a.create_token(user["id"], user["email"])
        exp = a.decode_token(token)["exp"]
        assert a.ws_expiry(a.decode_token(token)) == exp


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
        headers, jwk, private = _mint_headers()
        token = await _login(client, headers)
        wbd = app.state.auth.decode_token(token)[auth_mod.BIND_DEADLINE_CLAIM]
        proof = make_dpop_proof(
            private,
            jwk,
            method="POST",
            uri="http://test/api/v1/auth/refresh",
            token=token,
        )
        resp = await client.post(
            "/api/v1/auth/refresh",
            headers={**_bearer(token), "DPoP": proof},
        )
        assert resp.status_code == 200, resp.text
        payload = app.state.auth.decode_token(resp.json()["access_token"])
        assert payload[auth_mod.BIND_DEADLINE_CLAIM] == wbd
        # The replacement keeps the binding too.
        assert payload["cnf"]["jkt"] == dpop_mod.jwk_thumbprint(jwk)


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


class TestOidcMint:
    """The OIDC callback cannot carry request headers (top-level GET),
    so the binding key rides the login URL into the state cookie and
    the callback mint is born bound. Without it (key-less build, or a
    script stripping the param before navigation) the web flow mints
    unbound-with-deadline — the backstop."""

    @staticmethod
    def _provider():
        from klangk.oidc import OIDCProvider

        return OIDCProvider(
            id="test",
            display_name="Test",
            issuer="https://idp.example.com",
            client_id="klangk",
            client_secret="s",
        )

    async def _callback_app(self, app, monkeypatch, cookie_extra):
        provider = self._provider()
        monkeypatch.setattr(app.state.oidc, "get_provider", lambda _: provider)
        monkeypatch.setattr(
            app.state.oidc,
            "exchange_code",
            AsyncMock(return_value={"id_token": "fake", "access_token": "at"}),
        )
        monkeypatch.setattr(
            app.state.oidc,
            "validate_id_token",
            AsyncMock(
                return_value={
                    "sub": "oidc-sub-1",
                    "email": "oidc@example.com",
                    "email_verified": True,
                }
            ),
        )
        cookie = {
            "state": "test-state",
            "verifier": "test-verifier",
            "cli_redirect": None,
        }
        cookie.update(cookie_extra)
        return json.dumps(cookie)

    async def _redeem(self, client, app, monkeypatch, cookie_extra):
        """Run a callback; return the minted session token (redeemed
        from the one-time code the redirect carries)."""
        cookie = await self._callback_app(app, monkeypatch, cookie_extra)
        client.cookies.set("oidc_test", cookie)
        resp = await client.get(
            "/api/v1/auth/oidc/test/callback",
            params={"code": "auth-code", "state": "test-state"},
            follow_redirects=False,
        )
        assert resp.status_code == 302, resp.text
        location = resp.headers["location"]
        code = location.split("code=")[1].split("&")[0]
        exchanged = await client.post(
            "/api/v1/auth/oidc/exchange", json={"code": code}
        )
        assert exchanged.status_code == 200, exchanged.text
        return exchanged.json()["access_token"]

    async def test_web_flow_with_binding_param_born_bound(
        self, client, app, monkeypatch, user, db
    ):
        _, jwk = make_binding_key()
        token = await self._redeem(
            client, app, monkeypatch, {"binding_jwk": _b64url(json.dumps(jwk))}
        )
        payload = app.state.auth.decode_token(token)
        assert payload["cnf"]["jkt"] == dpop_mod.jwk_thumbprint(jwk)
        assert auth_mod.BIND_DEADLINE_CLAIM in payload

    async def test_web_flow_without_binding_param_refused(
        self, client, app, monkeypatch, user, db
    ):
        """A web flow whose navigation lost its binding key is refused
        at the callback — an attacker stripping the param gets a
        failed login, not an unbound token with a bind-first window
        (#3230 round-2 review F2)."""
        with pytest.raises(Exception) as exc_info:
            await self._redeem(client, app, monkeypatch, {})
        assert "400" in str(exc_info.value) or (
            getattr(exc_info.value, "response", None) is not None
            and exc_info.value.response.status_code == 400
        )

    async def test_web_flow_explicit_none_mints_unmarked(
        self, client, app, monkeypatch, user, db
    ):
        """The explicit cannot-bind marker: a key-less web build's
        OIDC session is unmarked — it keeps the pre-#3230 behavior
        instead of a deadline it can never satisfy (#3230 round-2 F4)."""
        token = await self._redeem(
            client, app, monkeypatch, {"binding_jwk": "none"}
        )
        payload = app.state.auth.decode_token(token)
        assert "cnf" not in payload
        assert auth_mod.BIND_DEADLINE_CLAIM not in payload

    async def test_cli_flow_mints_plain_token(
        self, client, app, monkeypatch, user, db
    ):
        token = await self._redeem(
            client,
            app,
            monkeypatch,
            {"cli_redirect": "http://localhost:12345/done"},
        )
        payload = app.state.auth.decode_token(token)
        assert "cnf" not in payload
        assert auth_mod.BIND_DEADLINE_CLAIM not in payload

    async def test_login_rejects_garbage_binding_param(
        self, client, app, monkeypatch, user, db
    ):
        monkeypatch.setattr(
            app.state.oidc, "oidc_login_allowed", lambda *a: True
        )
        monkeypatch.setattr(
            app.state.oidc,
            "get_provider",
            lambda _: self._provider(),
        )
        monkeypatch.setattr(
            app.state.oidc,
            "build_auth_url",
            AsyncMock(return_value="https://idp.example.com/auth"),
        )
        resp = await client.get(
            "/api/v1/auth/oidc/test/login",
            params={"binding_jwk": "!!!not-base64!!!"},
        )
        assert resp.status_code == 400

    async def test_login_stores_binding_param_in_state_cookie(
        self, client, app, monkeypatch, user, db
    ):
        from http.cookies import SimpleCookie

        monkeypatch.setattr(
            app.state.oidc, "oidc_login_allowed", lambda *a: True
        )
        monkeypatch.setattr(
            app.state.oidc,
            "get_provider",
            lambda _: self._provider(),
        )
        monkeypatch.setattr(
            app.state.oidc,
            "build_auth_url",
            AsyncMock(return_value="https://idp.example.com/auth"),
        )
        _, jwk = make_binding_key()
        encoded = _b64url(json.dumps(jwk))
        resp = await client.get(
            "/api/v1/auth/oidc/test/login",
            params={"binding_jwk": encoded},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        sc = SimpleCookie()
        sc.load(resp.headers["set-cookie"])
        data = json.loads(sc["oidc_test"].value)
        assert data["binding_jwk"] == encoded

    async def test_change_expired_password_mint(self, make_app, db):
        """The one SPA-reachable route the round-1 table missed — pins
        its web_mint_binding threading (#3230 round-2 F6)."""
        import datetime as dt

        app = make_app({"KLANGKD_PASSWORD_MAX_AGE_DAYS": "1"})
        created = await app.state.model.users.create_user(
            "expired@example.com",
            auth_mod.hash_password("OldPassword1"),
            verified=True,
        )
        old = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
        ).isoformat()
        async with app.state.db.transaction() as tx:
            await tx.execute(
                "UPDATE users SET password_set_at = ? WHERE id = ?",
                (old, created["id"]),
            )
        headers, jwk, _ = _mint_headers()
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            resp = await c.post(
                "/api/v1/auth/change-expired-password",
                json={
                    "identifier": "expired@example.com",
                    "current_password": "OldPassword1",
                    "new_password": "NewPassword123!",
                },
                headers=headers,
            )
        assert resp.status_code == 200, resp.text
        payload = app.state.auth.decode_token(resp.json()["access_token"])
        assert payload["cnf"]["jkt"] == dpop_mod.jwk_thumbprint(jwk)
        assert auth_mod.BIND_DEADLINE_CLAIM in payload


class TestSabotageScenario:
    """The #3230 attack end to end against the backstop path: an
    OIDC-minted web session (binding key stripped from the navigation)
    that never binds — the session still dies at the deadline across
    every surface."""

    async def test_never_bound_session_dies_at_deadline(
        self, client, user, monkeypatch
    ):
        token = _standalone_auth().create_token(
            user["id"],
            user["email"],
            web_deadline=time.time() + 3600,
        )
        # Works while inside the grace window (bind sabotaged, unused).
        resp = await client.get("/api/v1/auth/me", headers=_bearer(token))
        assert resp.status_code == 200
        wbd = _standalone_auth().decode_token(token)[
            auth_mod.BIND_DEADLINE_CLAIM
        ]
        # Time passes the baked-in deadline.
        monkeypatch.setattr(auth_mod.time, "time", lambda: wbd + 1)
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
    """Branch coverage for the boolean gate and the WS expiry."""

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

    def test_ws_expiry_deadline_after_exp_uses_exp(self):
        """A deadline past the token's own expiry never extends it."""
        a = _standalone_auth()
        payload = {"exp": 1000.0, "wbd": 2000.0}
        assert a.ws_expiry(payload) == 1000.0

    def test_ws_expiry_missing_exp(self):
        a = _standalone_auth()
        assert a.ws_expiry({"wbd": 123.0}) is None
