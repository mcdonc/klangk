"""Health-check surfacing + idle timeout against the appliance (#2561).

The appliance boots with ``KLANGKD_IDLE_TIMEOUT_SECONDS=20`` and a 2s
health poll (see ``_appliance.Appliance._env``) so both behaviors
assert in bounded time:

* a failing per-workspace health check surfaces its stderr + exit code
  through the status API (#1088), and
* a workspace whose last client disconnects is stopped by the idle
  sweep after the timeout.
"""

import asyncio
import time
import uuid


from _ws import connect_workspace


def _create(api, headers, **fields):
    resp = api.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": f"super-hi-{uuid.uuid4().hex[:8]}", **fields},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _status(api, headers, ws_id):
    resp = api.get(f"/api/v1/workspaces/{ws_id}/status", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_unhealthy_check_surfaces_reason(appliance, api, auth):
    headers = auth["headers"]
    marker = f"super-health-{uuid.uuid4().hex[:8]}"
    ws_id = _create(api, headers, health_check=f"echo {marker} 1>&2; exit 3")
    conn = await connect_workspace(appliance, auth["token"], ws_id)
    try:
        deadline = time.monotonic() + 90
        state = None
        while time.monotonic() < deadline:
            state = _status(api, headers, ws_id)
            if state.get("health") == "unhealthy" and marker in (
                state.get("health_message") or ""
            ):
                break
            await asyncio.sleep(1)
        assert state is not None
        assert state["health"] == "unhealthy", state
        assert marker in (state["health_message"] or ""), state
        assert "exited 3" in (state["health_message"] or ""), state
    finally:
        await conn.close()
        api.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)


async def test_idle_timeout_stops_workspace(appliance, api, auth):
    """After the last WS client leaves, the idle sweep stops the container.

    Uses a per-workspace ``idle_timeout`` settings override (12s) — the
    deploy-wide default stays at 300s so no other test's containers are
    reaped mid-flight.
    """
    headers = auth["headers"]
    ws_id = _create(api, headers, settings={"idle_timeout": 12})
    conn = await connect_workspace(appliance, auth["token"], ws_id)
    await conn.close()

    # Override 12s (check interval scales to 6s): stopped well within 90s.
    deadline = time.monotonic() + 90
    running = True
    while time.monotonic() < deadline:
        running = _status(api, headers, ws_id)["running"]
        if not running:
            break
        await asyncio.sleep(2)
    assert running is False, (
        "workspace still running 90s after the last client left "
        "(per-workspace idle_timeout 12s)"
    )
    api.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
