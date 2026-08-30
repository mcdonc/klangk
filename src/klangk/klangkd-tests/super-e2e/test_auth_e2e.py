"""Auth tests against the appliance (#2561): the password flow end users hit.

The appliance boots in password mode with a seeded default user (the
supported configuration for the published image —
docs/deployment/docker.md).
"""

import uuid


def test_login_success(api):
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": "admin@example.com", "password": "adminpass"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_login_wrong_password(api):
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": "admin@example.com", "password": "wrong"},
    )
    assert resp.status_code == 401


def test_unknown_user_rejected(api):
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": "nobody@example.com", "password": "x"},
    )
    assert resp.status_code == 401


def test_unauthenticated_api_denied(api):
    assert api.get("/api/v1/workspaces").status_code == 401


def test_register_and_login(api):
    """Test mode auto-verifies registrations (KLANGKD_TEST_MODE=1)."""
    email = f"super-{uuid.uuid4().hex[:8]}@example.com"
    resp = api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "registerpass"},
    )
    assert resp.status_code == 200, resp.text
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": "registerpass"},
    )
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_config_instance_id(api, auth):
    resp = api.get("/api/v1/config", headers=auth["headers"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["instance_id"]
    # The appliance ships FQDN egress filtering armed (netfilter_enabled
    # + the embedded sidecar image) — the deployed default (#2255).
    assert data.get("netfilter_enabled") is True, data
