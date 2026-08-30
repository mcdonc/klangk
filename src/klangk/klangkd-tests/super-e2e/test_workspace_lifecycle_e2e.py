"""Workspace lifecycle against the appliance (#2561).

create → connect (container bringup inside the appliance's nested
rootless podman) → exec → interactive terminal → stop → start → delete,
all through the public API/WS surfaces.
"""

import asyncio
import json
import uuid


from _ws import connect_workspace, exec_command, recv_until


def _create(api, headers, **fields):
    name = fields.pop("name", f"super-lifecycle-{uuid.uuid4().hex[:8]}")
    resp = api.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": name, **fields},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _status(api, headers, ws_id):
    resp = api.get(f"/api/v1/workspaces/{ws_id}/status", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_create_exec_stop_start_delete(appliance, api, auth):
    token = auth["token"]
    headers = auth["headers"]
    ws = _create(api, headers)
    ws_id = ws["id"]
    ws_conn = await connect_workspace(appliance, token, ws_id)
    try:
        # exec: run a real process inside the nested container.
        marker = f"super-{uuid.uuid4().hex[:8]}"
        output, code = await exec_command(
            ws_conn, ["bash", "-c", f"echo {marker}; pwd"]
        )
        assert code == 0, output
        assert marker in output
        assert "/home/klangk" in output

        # interactive terminal in the same container
        await ws_conn.send(
            json.dumps({"cmd": "terminal_start", "cols": 80, "rows": 24})
        )
        msgs = await recv_until(
            ws_conn, lambda m: m.get("type") == "terminal_started"
        )
        assert any(m.get("type") == "terminal_started" for m in msgs), msgs
    finally:
        await ws_conn.close()

    # stop via the API: the container is stopped + removed.
    resp = api.post(f"/api/v1/workspaces/{ws_id}/stop", headers=headers)
    assert resp.status_code == 200
    assert _status(api, headers, ws_id)["running"] is False

    # start via the API: a fresh container comes up inside the appliance.
    resp = api.post(f"/api/v1/workspaces/{ws_id}/start", headers=headers)
    assert resp.status_code == 200, resp.text
    deadline_secs = 60
    running = False
    for _ in range(deadline_secs):
        running = _status(api, headers, ws_id)["running"]
        if running:
            break
        await asyncio.sleep(1)
    # Single observation: the idle sweep could stop the container between
    # two checks, so assert on the value that ended the poll.
    assert running is True, "container did not come back after /start"

    # delete: gone for good.
    resp = api.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
    assert resp.status_code == 200
    resp = api.get(f"/api/v1/workspaces/{ws_id}/status", headers=headers)
    # The workspace row and its ACEs are cascade-deleted together, so the
    # ACL gate refuses the monitor permission (403) before the router can
    # answer its own 404 — either proves the workspace is unreachable.
    assert resp.status_code in (403, 404)
