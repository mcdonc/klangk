"""Session-workstation-binding E2E against a real klangkd (#3194).

Boots one server with ``KLANGKD_SESSION_WORKSTATION_BINDING=ip`` and
simulates two workstations by sending different ``X-Real-IP`` headers —
over the UDS the None peer is the trusted same-uid proxy hop, so the
backend honors the forwarded header as the effective client IP (the
same trick as the #2586 concurrent-logon audit E2E). Verifies the full
replay-protection contract on every consuming surface:

- HTTP: a request from a different workstation is 401 AND revokes the
  session (the token dies for its legitimate owner too — a replayed
  token must not survive first use from elsewhere);
- refresh: a rotation attempt from a different workstation is refused
  and revoked, including the idempotent cached-replacement arm (the
  already-refreshed old token must never disclose the live successor
  to a wrong-workstation caller);
- WebSocket: the handshake from a different workstation is refused;
- roaming within one IPv6 /64 and unresolvable clients stay allowed;
- a second, unarmed server is the control: with binding off, the same
  token answers from any workstation.

Run with: devenv shell -- test-backend-e2e -k session_workstation_binding
"""

import uuid

import pytest
from websockets.exceptions import InvalidStatus

from _e2e_server import httpx_client, start_server, stop_server, ws_connect

WS_A = "203.0.113.7"
WS_B = "198.51.100.9"
# Two hosts inside one 2001:db8:1:2::/64 — the roaming case binding
# must tolerate (privacy-extension address rotation).
IPV6_A = "2001:db8:1:2::10"
IPV6_B = "2001:db8:1:2::20"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A real klangkd with binding armed (logging to a file — the
    violation audit record lands there)."""
    log_path = tmp_path_factory.mktemp("binding") / "klangkd.log"
    server = start_server(
        KLANGKD_JWT_SECRET="session-binding-e2e-secret",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        KLANGKD_SESSION_WORKSTATION_BINDING="ip",
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


@pytest.fixture(scope="module")
def open_server():
    """The control: binding off (the default)."""
    server = start_server(
        KLANGKD_JWT_SECRET="session-binding-off-e2e-secret",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
    )
    yield server
    stop_server(server)


@pytest.fixture(scope="module")
def open_api(open_server):
    with httpx_client(open_server, timeout=30.0) as client:
        yield client


def _workstation(ip: str | None) -> dict:
    """Headers presenting workstation *ip* (no header → unresolvable)."""
    return {"X-Real-IP": ip} if ip is not None else {}


def _bearer(token: str, ip: str | None = None) -> dict:
    return {"Authorization": f"Bearer {token}", **_workstation(ip)}


def _register(api, source_ip: str) -> tuple[str, str]:
    """Register a unique auto-verified user from *source_ip*; return
    (email, token)."""
    email = f"bind-{uuid.uuid4().hex[:8]}@example.com"
    resp = api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "testpass"},
        headers=_workstation(source_ip),
    )
    assert resp.status_code == 200, resp.text
    return email, resp.json()["access_token"]


def _login(api, email: str, source_ip: str, password: str = "testpass") -> str:
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": password},
        headers=_workstation(source_ip),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _refresh(api, token: str, ip: str):
    return api.post("/api/v1/auth/refresh", headers=_bearer(token, ip))


def _violation_records(server) -> list[str]:
    with open(server["audit_log"]) as fh:
        return [
            line
            for line in fh.read().splitlines()
            if "session binding violation" in line
        ]


class TestHttpRequestSurface:
    def test_replayed_request_rejected_and_revoked(self, api, server):
        """The security property (#3194): a token captured on workstation
        A, replayed from B, is refused — and the session is revoked, so
        the token is dead for everyone (the owner re-authenticates)."""
        email, token = _register(api, WS_A)
        alive = api.get("/api/v1/auth/me", headers=_bearer(token, WS_A))
        assert alive.status_code == 200
        user_id = alive.json()["id"]

        replay = api.get("/api/v1/auth/me", headers=_bearer(token, WS_B))
        assert replay.status_code == 401
        assert replay.json()["detail"] == (
            "Session bound to a different workstation"
        )

        # The violation is audited with both workstations.
        records = _violation_records(server)
        assert records, "no session binding violation audit record"
        assert WS_A in records[-1] and WS_B in records[-1]

        # ...in the structured audit stream too (#3205): a
        # session.revoke row whose detail names the bound workstation
        # and whose source_ip is the presenting one. The default admin
        # holds manage-events.
        admin = _login(api, "admin@example.com", "127.0.0.1", "adminpass")
        resp = api.get(
            "/api/v1/events/audit",
            params={"event": "session.revoke", "limit": 200},
            headers=_bearer(admin),
        )
        assert resp.status_code == 200, resp.text
        rows = [
            item
            for item in resp.json()["items"]
            if item["detail"].get("reason") == "workstation-binding"
        ]
        assert rows, "no workstation-binding session.revoke audit row"
        assert rows[0]["detail"]["bound_ip"] == WS_A
        assert rows[0]["source_ip"] == WS_B
        assert rows[0]["target_id"] == user_id

        # Revoked: the legitimate owner's next request reads as revoked.
        owner = api.get("/api/v1/auth/me", headers=_bearer(token, WS_A))
        assert owner.status_code == 401
        assert owner.json()["detail"] == "Token has been revoked"

    def test_ipv6_roaming_within_prefix_allowed(self, api, server):
        """Address rotation inside one /64 is the same workstation —
        the session survives (the roaming-tolerant judgement)."""
        _, token = _register(api, IPV6_A)
        before = len(_violation_records(server))
        assert (
            api.get(
                "/api/v1/auth/me", headers=_bearer(token, IPV6_B)
            ).status_code
            == 200
        )
        assert len(_violation_records(server)) == before

    def test_unresolvable_client_fails_open(self, api, server):
        """A request with no resolvable client IP (no forwarded header,
        UDS peer None) is never judged — the documented fail-open."""
        _, token = _register(api, WS_A)
        before = len(_violation_records(server))
        assert (
            api.get("/api/v1/auth/me", headers=_bearer(token)).status_code
            == 200
        )
        assert len(_violation_records(server)) == before


class TestRefreshSurface:
    def test_refresh_from_foreign_workstation_refused(self, api):
        """A rotation attempt from B with an A-issued token is 401 and
        revokes the session (the refresh seam is where a headless
        stolen-token client must eventually surface)."""
        email, token = _register(api, WS_A)
        replay = _refresh(api, token, WS_B)
        assert replay.status_code == 401
        assert replay.json()["detail"] == (
            "Session bound to a different workstation"
        )
        owner = api.get("/api/v1/auth/me", headers=_bearer(token, WS_A))
        assert owner.status_code == 401
        assert owner.json()["detail"] == "Token has been revoked"

    def test_cached_refresh_replacement_not_disclosed(self, api):
        """The idempotent arm: after a legitimate refresh, replaying the
        OLD token from B must not hand over the cached live replacement
        — it is refused and the replacement revoked (#3194 review)."""
        email, old = _register(api, WS_A)
        rotated = _refresh(api, old, WS_A)
        assert rotated.status_code == 200
        new = rotated.json()["access_token"]
        assert new != old

        replay = _refresh(api, old, WS_B)
        assert replay.status_code == 401

        # The successor died with the violation; the owner re-logs in.
        owner = api.get("/api/v1/auth/me", headers=_bearer(new, WS_A))
        assert owner.status_code == 401
        assert owner.json()["detail"] == "Token has been revoked"
        assert _login(api, email, WS_A)


class TestWebSocketSurface:
    async def test_ws_dial_from_foreign_workstation_refused(self, server, api):
        """The /ws handshake presents the token from B — refused before
        accept (HTTP 403 handshake rejection, same observable contract
        as any dead token; see test_token_revocation_e2e). The refused
        dial also revokes the session, so the owner is out too."""
        _, token = _register(api, WS_A)
        # Control first: the same token from its own workstation
        # connects (the refusal below is about the workstation, not the
        # token) — the foreign dial would revoke it and close this
        # window.
        ws = await ws_connect(
            server,
            f"/ws?token={token}",
            additional_headers={"X-Real-IP": WS_A},
        )
        await ws.close()

        with pytest.raises(InvalidStatus) as excinfo:
            await ws_connect(
                server,
                f"/ws?token={token}",
                additional_headers={"X-Real-IP": WS_B},
            )
        assert excinfo.value.response.status_code == 403

        # The refused dial revoked the session: the owner is out too.
        owner = api.get("/api/v1/auth/me", headers=_bearer(token, WS_A))
        assert owner.status_code == 401
        assert owner.json()["detail"] == "Token has been revoked"


class TestBindingOffControl:
    def test_binding_off_allows_any_workstation(self, open_api):
        """The control: with binding off (the default), the very same
        request pattern the armed server refuses sails through — the
        401s above come from the setting, not the test setup."""
        _, token = _register(open_api, WS_A)
        assert (
            open_api.get(
                "/api/v1/auth/me", headers=_bearer(token, WS_B)
            ).status_code
            == 200
        )
