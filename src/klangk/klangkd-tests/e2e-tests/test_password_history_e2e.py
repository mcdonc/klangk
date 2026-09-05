"""Password-history reuse gate, end to end against a real server (#2582).

Starts klangkd with ``KLANGKD_PASSWORD_HISTORY_COUNT=3`` and drives the
change-password endpoint through the full lifecycle: reuse of the
current password rejected, reuse of a retired password rejected, a hash
pruned out of the 3-deep window becoming reusable again, and
``/api/v1/config`` advertising the count.

No containers are involved (password changes never touch podman), so the
server comes up fast; the headless UDS mode is enough.

Run with: devenv shell -- python -m pytest src/klangk/klangkd-tests/e2e-tests/test_password_history_e2e.py -v
"""

import pytest

from _e2e_server import httpx_client, start_server, stop_server


@pytest.fixture(scope="module")
def server():
    server = start_server(
        KLANGKD_JWT_SECRET="pw-history-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_PASSWORD_HISTORY_COUNT="3",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
    )
    yield server
    stop_server(server)


@pytest.fixture(scope="module")
def api(server):
    with httpx_client(server, timeout=30.0) as client:
        yield client


def _login(api, password):
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": "admin@example.com", "password": password},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _change(api, headers, current, new):
    """POST a password change; returns the response object."""
    return api.post(
        "/api/v1/auth/change-password",
        json={"current_password": current, "new_password": new},
        headers=headers,
    )


class TestPasswordHistoryE2E:
    def test_config_advertises_history_count(self, api):
        resp = api.get("/api/v1/config")
        assert resp.status_code == 200, resp.text
        assert resp.json()["password_history_count"] == 3

    def test_reuse_gate_lifecycle(self, api):
        # All passwords must clear the default 8-char minimum policy.
        # Each successful change revokes all sessions (#3152), so we
        # must re-login after every 200.
        seed = "adminpass"
        p1, p2, p3, p4 = (
            "e2e-pass-one",
            "e2e-pass-two",
            "e2e-pass-three",
            "e2e-pass-four",
        )
        headers = _login(api, seed)

        # Reusing the current password is always rejected.
        resp = _change(api, headers, current=seed, new=seed)
        assert resp.status_code == 400, resp.text
        assert "current" in resp.json()["detail"]

        # Change away: retires the seed hash into history.
        assert _change(api, headers, seed, p1).status_code == 200
        headers = _login(api, p1)

        # Changing back to the just-retired seed is rejected.
        resp = _change(api, headers, current=p1, new=seed)
        assert resp.status_code == 400, resp.text
        assert "recently" in resp.json()["detail"]

        # Fill the window: history (newest first) becomes
        # [p2, p1, seed] after p3, then [p3, p2, p1] after p4 —
        # the seed falls out of the 3-deep window.
        assert _change(api, headers, p1, p2).status_code == 200
        headers = _login(api, p2)
        assert _change(api, headers, p2, p3).status_code == 200
        headers = _login(api, p3)
        assert _change(api, headers, p3, p4).status_code == 200
        headers = _login(api, p4)

        # Still inside the window: p1 is rejected.
        resp = _change(api, headers, current=p4, new=p1)
        assert resp.status_code == 400, resp.text
        assert "recently" in resp.json()["detail"]

        # Pruned out of the window: the seed is reusable again. This
        # retires p4, pruning p1 — history is now [p2, p3, seed].
        assert _change(api, headers, p4, seed).status_code == 200
        headers = _login(api, seed)

        # p1 just fell out of the window -> reusable. That change
        # retires the seed, so history is now [p3, p4, seed]: p3 is
        # still in the window — the gate stays live for the next cycle.
        assert _change(api, headers, seed, p1).status_code == 200
        headers = _login(api, p1)
        resp = _change(api, headers, current=p1, new=p3)
        assert resp.status_code == 400, resp.text
        assert "recently" in resp.json()["detail"]
