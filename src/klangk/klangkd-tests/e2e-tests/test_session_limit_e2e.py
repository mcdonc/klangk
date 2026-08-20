"""Concurrent-session limiting E2E against a real klangkd (#2585).

Boots one server with ``KLANGKD_MAX_SESSIONS_PER_USER=2`` and drives the
full HTTP lifecycle of the cap: eviction of the oldest session via the
token blocklist, refresh keeping its slot, and logout freeing a slot.

Run with: devenv shell -- test-backend-e2e test_session_limit_e2e.py
"""

import uuid

import pytest

from _e2e_server import httpx_client, start_server, stop_server

CAP = 2


@pytest.fixture(scope="module")
def server():
    """A real klangkd with the per-user concurrent-session cap set to 2."""
    server = start_server(
        KLANGKD_JWT_SECRET="session-limit-e2e-secret",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_MAX_SESSIONS_PER_USER=str(CAP),
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
    )
    yield server
    stop_server(server)


@pytest.fixture(scope="module")
def api(server):
    """httpx client pointing at the test server (over its UDS)."""
    with httpx_client(server, timeout=10.0) as client:
        yield client


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _register_with_email(api) -> tuple[str, str]:
    """Register a unique auto-verified user; return (email, session-1 token).

    The register-issued token is itself session #1 for the fresh user —
    every test starts from it so the session count is deterministic.
    """
    email = f"sl-{uuid.uuid4().hex[:8]}@example.com"
    resp = api.post(
        "/api/v1/auth/register", json={"email": email, "password": "testpass"}
    )
    assert resp.status_code == 200, resp.text
    return email, resp.json()["access_token"]


def _login(api, email: str) -> str:
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": "testpass"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _me(api, token: str):
    """Probe a token: returns the response of GET /auth/me."""
    return api.get("/api/v1/auth/me", headers=_headers(token))


class TestSessionLimit:
    def test_cap_evicts_oldest_session(self, api):
        """A login past the cap revokes the oldest session's token."""
        email, first = _register_with_email(api)
        second = _login(api, email)

        # Both sessions valid at the cap.
        assert _me(api, first).status_code == 200
        assert _me(api, second).status_code == 200

        # Third login exceeds the cap → the oldest (register-issued)
        # session is revoked through the blocklist.
        third = _login(api, email)
        resp = _me(api, first)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Token has been revoked"
        assert _me(api, second).status_code == 200
        assert _me(api, third).status_code == 200

    def test_refresh_keeps_session_slot(self, api):
        """Refreshing a token does not grow the session count: the
        refreshed token inherits the old one's slot, so a user at the cap
        can refresh without evicting their other session."""
        email, first = _register_with_email(api)
        second = _login(api, email)  # at the cap

        resp = api.post("/api/v1/auth/refresh", headers=_headers(second))
        assert resp.status_code == 200, resp.text
        refreshed = resp.json()["access_token"]
        assert refreshed != second

        # The refreshed token works, the old one is retired, and — the
        # point — the FIRST session was not evicted (refresh kept its
        # slot instead of opening a third session).
        assert _me(api, refreshed).status_code == 200
        assert _me(api, second).status_code == 401
        assert _me(api, first).status_code == 200

    def test_refresh_does_not_reset_eviction_order(self, api):
        """Eviction order is login time, not refresh time: refreshing the
        oldest session does not make it the newest, so a later login past
        the cap evicts the refreshed (oldest) session, not the younger
        idle one.
        """
        email, first = _register_with_email(api)
        second = _login(api, email)  # at the cap

        resp = api.post("/api/v1/auth/refresh", headers=_headers(first))
        assert resp.status_code == 200, resp.text
        refreshed = resp.json()["access_token"]
        assert _me(api, refreshed).status_code == 200

        # Third login exceeds the cap → the refreshed token (session #1
        # by login time) is evicted; the younger idle session survives.
        third = _login(api, email)
        resp = _me(api, refreshed)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Token has been revoked"
        assert _me(api, second).status_code == 200
        assert _me(api, third).status_code == 200

    def test_logout_frees_session_slot(self, api):
        """Logging out one session frees its slot: the next login does
        not evict the remaining session."""
        email, first = _register_with_email(api)
        second = _login(api, email)  # at the cap

        resp = api.post("/api/v1/auth/logout", headers=_headers(first))
        assert resp.status_code == 200

        # Slot freed → third login evicts nothing.
        third = _login(api, email)
        assert _me(api, second).status_code == 200
        assert _me(api, third).status_code == 200
