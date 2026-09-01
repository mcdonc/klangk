"""Concurrent-logon audit E2E against a real klangkd (#2586).

Boots one server (UDS-direct) and simulates two workstations by sending
different ``X-Real-IP`` headers on the auth endpoints — over the UDS the
None peer is the trusted same-uid proxy hop, so the backend honors the
forwarded header as the effective client IP. Verifies the logon-time
audit record in the server log and the admin query endpoint.

Run with: devenv shell -- test-backend-e2e test_concurrent_logon_audit_e2e.py
"""

import uuid

import pytest

from _e2e_server import httpx_client, start_server, stop_server

WS_A = "203.0.113.7"
WS_B = "198.51.100.9"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A real klangkd logging to a file (the audit record lands there)."""
    log_path = tmp_path_factory.mktemp("audit") / "klangkd.log"
    server = start_server(
        KLANGKD_JWT_SECRET="logon-audit-e2e-secret",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
        log_path=str(log_path),
    )
    server["audit_log"] = str(log_path)
    yield server
    stop_server(server)


@pytest.fixture(scope="module")
def api(server):
    # 30s budget: login is PBKDF2 CPU work that stretches past 10s on a
    # CI runner starved by concurrent xdist workers (#2740).
    with httpx_client(server, timeout=30.0) as client:
        yield client


def _log(server) -> str:
    with open(server["audit_log"]) as fh:
        return fh.read()


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _audit_records(server) -> list[str]:
    return [
        line
        for line in _log(server).splitlines()
        if "concurrent logon from different workstations" in line
    ]


def _register(api, source_ip: str) -> tuple[str, str]:
    """Register a unique auto-verified user from *source_ip*; return
    (email, session-1 token)."""
    email = f"cla-{uuid.uuid4().hex[:8]}@example.com"
    resp = api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "testpass"},
        headers={"X-Real-IP": source_ip},
    )
    assert resp.status_code == 200, resp.text
    return email, resp.json()["access_token"]


def _login(api, email: str, source_ip: str, password: str = "testpass") -> str:
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": password},
        headers={"X-Real-IP": source_ip},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


class TestConcurrentLogonAudit:
    def test_different_workstation_audited(self, api, server):
        """A login concurrent with an active session from another
        workstation writes an audit record naming both."""
        email, first = _register(api, WS_A)
        assert _audit_records(server) == []

        second = _login(api, email, WS_B)
        records = _audit_records(server)
        assert len(records) == 1
        assert WS_A in records[0] and WS_B in records[0]
        assert email in records[0]

        # Both sessions are live (no limit configured — audit only).
        assert (
            api.get("/api/v1/auth/me", headers=_bearer(first)).status_code
            == 200
        )
        assert (
            api.get("/api/v1/auth/me", headers=_bearer(second)).status_code
            == 200
        )

    def test_same_workstation_not_audited(self, api, server):
        """Logins from one workstation (two browsers on one machine) are
        concurrent but not from different workstations: no record."""
        email, _ = _register(api, WS_A)
        before = len(_audit_records(server))
        _login(api, email, WS_A)
        _login(api, email, WS_A)
        assert len(_audit_records(server)) == before

    def test_admin_can_query_session_workstations(self, api, server):
        """The admin sessions endpoint lists the user's active sessions
        with their workstation identity — the queryable audit trail."""
        email, _ = _register(api, WS_A)
        token = _login(api, email, WS_B)
        user_id = api.get("/api/v1/auth/me", headers=_bearer(token)).json()[
            "id"
        ]

        admin = _login(api, "admin@example.com", "127.0.0.1", "adminpass")
        resp = api.get(
            f"/api/v1/users/{user_id}/sessions",
            headers=_bearer(admin),
        )
        assert resp.status_code == 200, resp.text
        ips = {item["source_ip"] for item in resp.json()["items"]}
        assert ips == {WS_A, WS_B}
        # Every row carries the full workstation identity shape.
        for item in resp.json()["items"]:
            assert set(item) == {
                "created_at",
                "expires_at",
                "source_ip",
                "user_agent",
            }

    def test_sessions_endpoint_requires_admin(self, api):
        """A non-admin user cannot query another user's sessions."""
        email, _ = _register(api, WS_A)
        token = _login(api, email, WS_B)
        user_id = api.get("/api/v1/auth/me", headers=_bearer(token)).json()[
            "id"
        ]
        resp = api.get(
            f"/api/v1/users/{user_id}/sessions",
            headers=_bearer(token),
        )
        assert resp.status_code == 403
