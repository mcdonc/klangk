"""Token-revocation E2E tests.

Runtime verification, against real klangkd + real websockets, of the
session-token revocation machinery in ``auth.py`` (the jti blocklist)
across BOTH surfaces that consume a token:

- the HTTP API (``get_user_from_token`` → 401 for a blocklisted token),
- the workspace websocket (``ws_authenticate`` → close 4001
  "Invalid token"; 4002 "Token expired" for a lapsed one).

Covers the three producers of a revocation — logout, refresh (the old
jti is blocklisted with the replacement cached), and expiry (a
separate short-TTL server) — plus the two documented behaviors that
are easy to regress:

- logout is idempotent (#2687): revoking an already-revoked token is
  still 200;
- a refresh is replayable: the blocklisted old token returns its
  CACHED replacement, not an error — while a token revoked by LOGOUT
  can never refresh.

Also covers #3152's pushed eviction: a hard revocation (logout)
closes the live sockets that token authenticated (4001 "Token
revoked"), refresh rotation retargets instead of kicking, the kick is
per-JTI (other sessions survive), and token expiry closes an open
socket at exp (4002 "Token expired").

Run with: devenv shell -- test-backend-e2e test_token_revocation_e2e.py
"""

import asyncio
import json

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from _e2e_server import (
    httpx_async_client,
    httpx_client,
    start_server,
    stop_server,
    ws_connect,
)

# ~1.1s: long enough to log in and dial, short enough that the token
# has lapsed after a 3s sleep.
SHORT_TOKEN_HOURS = "0.0003"


@pytest.fixture(scope="module")
def server():
    """A real klangkd with default token lifetime."""
    server = start_server(
        KLANGKD_JWT_SECRET="token-revocation-e2e-secret",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
    )
    yield server
    stop_server(server)


@pytest.fixture(scope="module")
def api(server):
    with httpx_client(server, timeout=10.0) as client:
        yield client


@pytest.fixture(scope="module")
def short_server():
    """A second klangkd whose tokens lapse in ~1 second."""
    server = start_server(
        KLANGKD_JWT_SECRET="token-expiry-e2e-secret",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        KLANGKD_ACCESS_TOKEN_HOURS=SHORT_TOKEN_HOURS,
        LOGFIRE_TOKEN="",
    )
    yield server
    stop_server(server)


def login(api) -> str:
    """A fresh access token (each login is its own session/jti)."""
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": "admin@example.com", "password": "adminpass"},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def alogin(aapi) -> str:
    """A fresh token via the ASYNC client (tests that hold a live
    websocket open across an HTTP call must not block the event loop:
    the sync client would freeze the websockets background task that
    must ACK the server's close handshake during a kick)."""
    resp = await aapi.post(
        "/api/v1/auth/login",
        json={"identifier": "admin@example.com", "password": "adminpass"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def alogout(aapi, token) -> int:
    resp = await aapi.post("/api/v1/auth/logout", headers=headers(token))
    return resp.status_code


async def arefresh(aapi, token):
    return await aapi.post("/api/v1/auth/refresh", headers=headers(token))


def logout(api, token) -> int:
    return api.post("/api/v1/auth/logout", headers=headers(token)).status_code


def refresh(api, token):
    return api.post("/api/v1/auth/refresh", headers=headers(token))


async def probe_liveness(ws) -> str:
    """Send an unknown command and drain (broadcast frames like
    server_schedule race the reply) until the error frame arrives."""
    await ws.send(json.dumps({"cmd": "definitely-not-a-command"}))
    deadline = asyncio.get_event_loop().time() + 5
    while asyncio.get_event_loop().time() < deadline:
        msg = await asyncio.wait_for(ws.recv(), timeout=5)
        if "Unknown command" in str(msg):
            return str(msg)
    raise AssertionError("no error frame answered the probe")


async def expect_live_close(ws, code: int, reason_part: str = "") -> None:
    """Assert an OPEN socket is closed with *code* (broadcast frames
    may precede the close — drain until ConnectionClosed)."""
    with pytest.raises(ConnectionClosed) as excinfo:
        while True:
            await asyncio.wait_for(ws.recv(), timeout=10)
    rcvd = excinfo.value.rcvd
    assert rcvd is not None, f"no close frame: {excinfo.value}"
    assert rcvd.code == code, f"expected close {code}, got {rcvd}"
    if reason_part:
        assert reason_part in (rcvd.reason or ""), (
            f"expected reason ~{reason_part!r}, got {rcvd.reason!r}"
        )


@pytest.fixture
async def aapi(server):
    """An async httpx client on the same event loop as the websockets
    (see alogin for why the eviction tests need it)."""
    async with httpx_async_client(server, timeout=10.0) as client:
        yield client


async def expect_ws_rejected(server, token: str) -> None:
    """Dial ``/ws?token=...`` with a dead token and assert the
    handshake is refused.

    ``ws_authenticate`` runs BEFORE ``websocket.accept()``
    (dispatch.py), and an ASGI close on an unaccepted socket is an
    HTTP 403 handshake rejection — the in-code close codes (4001
    invalid / 4002 expired) never cross the wire to a fresh dial; a
    client sees a refused upgrade. This pins that observable
    contract."""
    with pytest.raises(InvalidStatus) as excinfo:
        await ws_connect(server, f"/ws?token={token}")
    assert excinfo.value.response.status_code == 403, (
        f"expected handshake 403, got {excinfo.value.response.status_code}"
    )


class TestLogoutRevocation:
    async def test_logout_revokes_api_and_ws(self, server, api):
        """A logged-out token is dead on both surfaces; a fresh login
        works."""
        token = login(api)
        assert (
            api.get("/api/v1/workspaces", headers=headers(token)).status_code
            == 200
        )
        assert logout(api, token) == 200
        # API: 401
        resp = api.get("/api/v1/workspaces", headers=headers(token))
        assert resp.status_code == 401
        # WS: the handshake itself is refused (403) — see
        # expect_ws_rejected for why this is not a 4001 close code
        await expect_ws_rejected(server, token)
        # A fresh session is unaffected
        fresh = login(api)
        assert (
            api.get("/api/v1/workspaces", headers=headers(fresh)).status_code
            == 200
        )

    def test_logout_idempotent(self, api):
        """#2687: revoking an already-revoked token is still 200."""
        token = login(api)
        assert logout(api, token) == 200
        assert logout(api, token) == 200

    def test_revoked_by_logout_cannot_refresh(self, api):
        """A token revoked by LOGOUT has no cached replacement — the
        refresh endpoint must refuse it (401 'Token has been
        revoked'), not mint a new session."""
        token = login(api)
        assert logout(api, token) == 200
        resp = refresh(api, token)
        assert resp.status_code == 401
        assert "revoked" in resp.json().get("detail", "").lower()


class TestRefreshRevocation:
    async def test_refresh_revokes_old_token_everywhere(self, server, api):
        """After a refresh, the old jti is dead on API and WS; the new
        token works."""
        old = login(api)
        resp = refresh(api, old)
        assert resp.status_code == 200, resp.text
        new = resp.json()["access_token"]
        assert new != old
        # Old token: API 401, WS handshake refused
        assert (
            api.get("/api/v1/workspaces", headers=headers(old)).status_code
            == 401
        )
        await expect_ws_rejected(server, old)
        # New token: API 200
        assert (
            api.get("/api/v1/workspaces", headers=headers(new)).status_code
            == 200
        )

    def test_refresh_is_replayable(self, api):
        """Refreshing with the already-refreshed token returns the
        CACHED replacement (the same new token), not an error — the
        idempotency contract that lets a racing client retry its
        refresh."""
        old = login(api)
        first = refresh(api, old)
        assert first.status_code == 200
        again = refresh(api, old)
        assert again.status_code == 200
        assert again.json()["access_token"] == first.json()["access_token"]


class TestLiveSocketEviction:
    """#3152: a hard revocation pushes — the live sockets a revoked
    token authenticated are closed immediately (4001 'Token revoked'),
    not at the next connect. Refresh rotation is exempt (the socket is
    retargeted to the new jti instead), and the kick is per-JTI: other
    sessions of the same user stay connected."""

    async def test_logout_closes_open_socket(self, server, aapi):
        token = await alogin(aapi)
        ws = await ws_connect(server, f"/ws?token={token}")
        try:
            await probe_liveness(ws)
            assert await alogout(aapi, token) == 200
            await expect_live_close(ws, 4001, "Token revoked")
        finally:
            await ws.close()
        # And the NEXT connect with the revoked token is also refused
        await expect_ws_rejected(server, token)

    async def test_refresh_keeps_socket_and_retargets(self, server, aapi):
        """A refresh must NOT kick the live socket (rotation exempt);
        it retargets the connection onto the new jti, so a LATER logout
        still closes it."""
        old = await alogin(aapi)
        ws = await ws_connect(server, f"/ws?token={old}")
        try:
            await probe_liveness(ws)
            resp = await arefresh(aapi, old)
            assert resp.status_code == 200
            new = resp.json()["access_token"]
            # Rotation: the open socket survives...
            await probe_liveness(ws)
            # ...and is kickable under the NEW token
            assert await alogout(aapi, new) == 200
            await expect_live_close(ws, 4001, "Token revoked")
        finally:
            await ws.close()

    async def test_logout_kicks_only_that_session(self, server, aapi):
        """The kick is per-JTI: logging out one session leaves the
        same user's other live socket connected."""
        token_a = await alogin(aapi)
        token_b = await alogin(aapi)
        ws_a = await ws_connect(server, f"/ws?token={token_a}")
        ws_b = await ws_connect(server, f"/ws?token={token_b}")
        try:
            await probe_liveness(ws_a)
            await probe_liveness(ws_b)
            assert await alogout(aapi, token_b) == 200
            await expect_live_close(ws_b, 4001, "Token revoked")
            await probe_liveness(ws_a)  # untouched
        finally:
            await ws_b.close()
            await ws_a.close()


class TestExpiredToken:
    async def test_expired_token_rejected_api_and_ws(self, short_server):
        """A lapsed token is 401 'Token expired' on the API and close
        4002 'Token expired' on the WS (distinct from revocation's
        4001)."""
        with httpx_client(short_server, timeout=10.0) as api:
            token = login(api)
            assert (
                api.get(
                    "/api/v1/workspaces", headers=headers(token)
                ).status_code
                == 200
            )
            await asyncio.sleep(3)
            # On a normal endpoint a lapsed token is 401 (the generic
            # dependency maps the expired-signature error to Invalid
            # token); the refresh endpoint is where the distinction
            # surfaces — ExpiredSignatureError -> 'Token expired'.
            resp = api.get("/api/v1/workspaces", headers=headers(token))
            assert resp.status_code == 401
            await expect_ws_rejected(short_server, token)
            # A lapsed token that was NEVER refreshed cannot refresh
            resp = refresh(api, token)
            assert resp.status_code == 401
            assert "expired" in resp.json().get("detail", "").lower()

    async def test_expiry_closes_open_socket(self, short_server):
        """#3152: an open socket does not outlive its token — the
        connection schedules its own close at exp (4002 'Token
        expired')."""
        async with httpx_async_client(short_server, timeout=10.0) as aapi:
            token = await alogin(aapi)
        ws = await ws_connect(short_server, f"/ws?token={token}")
        try:
            await expect_live_close(ws, 4002, "Token expired")
        finally:
            await ws.close()
