"""Boot-shape tests: the supervisord-managed stack as actually shipped (#2561).

These prove the appliance's process tree, the caddy → UDS → uvicorn path,
and the frontend bundle served through the shipped reverse proxy — the
deployment surfaces none of the dev-shell e2e suites exercise.
"""

import asyncio
import time

import httpx

from _ws import dial


def test_pid1_is_supervisord(appliance):
    comm = appliance.exec_out("cat", "/proc/1/comm")
    assert comm == "supervisord", f"PID 1 is {comm!r}, not supervisord"


def test_klangkd_managed_by_supervisord(appliance):
    pids = appliance.service_pids("klangk.main")
    assert pids, "no klangkd (python3 -m klangk.main) process running"


def test_caddy_child_of_klangkd(appliance):
    # klangkd forks caddy as its own child (the caddy engine, #1559);
    # supervisord does not manage it directly.
    pids = appliance.service_pids("caddy")
    assert pids, "no caddy process running under klangkd"


def test_health_endpoint(appliance):
    with httpx.Client(base_url=appliance.url, timeout=10) as client:
        resp = client.get("/health")
    assert resp.status_code == 200


def test_frontend_served_through_proxy(appliance):
    """The Flutter web bundle is served by caddy from the installed wheel."""
    with httpx.Client(base_url=appliance.url, timeout=30) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Flutter 3.24+ loads main.dart.js via flutter_bootstrap.js; older
    # builds reference it directly. Either proves the wheel's bundled
    # frontend is what caddy serves.
    assert "flutter_bootstrap.js" in body or "main.dart.js" in body, (
        f"frontend bundle not referenced; body head: {body[:400]!r}"
    )


def test_version_endpoint(appliance, api, auth):
    resp = api.get("/api/v1/version", headers=auth["headers"])
    assert resp.status_code == 200
    data = resp.json()
    # The build-host-image flow bakes a real version (never "dev").
    assert data.get("version") not in (None, "", "dev"), data


async def test_ws_connects_through_proxy(appliance, auth):
    """The public WS endpoint answers through caddy (ping/pong round-trip)."""
    ws = await dial(appliance, auth["token"])
    try:
        pong = await ws.ping()
        await asyncio.wait_for(pong, timeout=10)
    finally:
        await ws.close()


def test_supervisord_restarts_klangkd(appliance, api, auth):
    """A crashed klangkd is restarted by supervisord (autorestart=true)."""
    pids = appliance.service_pids("klangk.main")
    assert pids, "no klangkd process to kill"
    appliance.exec("kill", "-9", pids[0])
    # klangkd comes back with a fresh PID and serves /health again. The
    # health wait (not just the pid) is the gate: the module's later tests
    # and the rest of the suite need a settled backend, and uvicorn only
    # re-binds its listeners once the restart completes.
    deadline = time.monotonic() + 180
    new_pids: list[str] = []
    while time.monotonic() < deadline:
        new_pids = appliance.service_pids("klangk.main")
        if new_pids and pids[0] not in new_pids:
            break
        time.sleep(2)
    assert new_pids, "klangkd did not come back after kill -9"
    assert pids[0] not in new_pids, "stale klangkd pid still present"
    healthy = False
    while time.monotonic() < deadline:
        try:
            if api.get("/health", timeout=5).status_code == 200:
                healthy = True
                break
        except Exception:
            pass
        time.sleep(2)
    assert healthy, "klangkd did not serve /health after supervisord restart"
