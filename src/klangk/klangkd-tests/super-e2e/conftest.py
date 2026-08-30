"""Shared fixtures for the super-E2E suite (#2561).

One session-scoped **appliance** — the real Docker host image booted with
the shipped nested-podman posture — serves every test. Clients are
black-box only: the public HTTP API and WebSocket over the published
browser port, plus a ``docker exec`` control channel for service-state
assertions (supervisord children, rootless podman inside the appliance,
SIGHUP delivery). No monkeypatching, no in-process app.

Per-test timeout: stamped generously (see ``_SUPER_E2E_TIMEOUT``) — the
first test also carries the session fixture's boot wait, and container
bringup inside nested rootless podman is slower than the unit suites'
60s cap.
"""

from __future__ import annotations

import os

import httpx
import pytest

from _appliance import Appliance

# Boot (first test's setup) + bringup + teardown headroom. The appliance
# fixture has its own internal 600s boot deadline; this must exceed it
# plus the test body itself.
_SUPER_E2E_TIMEOUT = 900

# Failed test node ids — populated by the makereport hook so the session
# teardown can decide whether dumping appliance logs is worth the noise.
_FAILED_TESTS: list[str] = []


def pytest_collection_modifyitems(config, items):
    """Give every super-e2e test a generous per-test timeout."""
    for item in items:
        if item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(_SUPER_E2E_TIMEOUT))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.failed:
        _FAILED_TESTS.append(report.nodeid)


@pytest.fixture(scope="session")
def appliance():
    """Boot the host appliance once per session; tear it down after.

    On failure, the teardown prints the appliance's combined logs — the
    in-container klangkd/caddy/supervisord output is the only evidence of
    most server-side failures, and the container (with its logs) dies at
    ``docker rm``.
    """
    image = os.environ.get("KLANGK_SUPER_E2E_IMAGE", "klangk-host:latest")
    app = Appliance(image=image)
    app.start()
    try:
        yield app
    finally:
        if _FAILED_TESTS:
            logs = app.logs()
            tail = "\n".join(logs.splitlines()[-400:])
            print(
                f"\n=== appliance logs (tail, {_FAILED_TESTS} failed) ===\n"
                f"{tail}\n=== end appliance logs ==="
            )
        app.stop()


@pytest.fixture(scope="session")
def api(appliance):
    """A long-lived httpx client bound to the appliance's published port.

    60s budget: register/login run PBKDF2 inside the appliance on a
    possibly single-CPU CI container, and workspace create awaits eager
    container bringup inside nested podman.
    """
    with httpx.Client(base_url=appliance.url, timeout=60.0) as client:
        yield client


@pytest.fixture(scope="session")
def auth(api):
    """Login as the seeded default (admin) user; token + headers."""
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": "admin@example.com", "password": "adminpass"},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }
