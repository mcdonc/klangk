"""Shared-terminal scenario against the appliance (#2561).

The owner shares one of their terminal windows; a second user (granted
the ``coders`` role on the workspace) sees it in the shared list and
joins it — the collaboration surface as deployed.
"""

import asyncio
import json
import uuid


from _ws import connect_workspace, recv_until


def _register(api, email, password):
    resp = api.post(
        "/api/v1/auth/register", json={"email": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    resp = api.post(
        "/api/v1/auth/login", json={"identifier": email, "password": password}
    )
    assert resp.status_code == 200, resp.text
    return {
        "email": email,
        "token": resp.json()["access_token"],
        "headers": {"Authorization": f"Bearer {resp.json()['access_token']}"},
    }


async def _start_terminal(ws):
    await ws.send(
        json.dumps({"cmd": "terminal_start", "cols": 80, "rows": 24})
    )


def _windows(msgs):
    for m in reversed(msgs):
        if m.get("type") == "terminal_windows":
            return m.get("windows", [])
    return None


async def _wait_shared_contains(ws, window_id: str, timeout: float = 30):
    """Nudge list_shared_terminals until the shared list contains the window."""
    deadline = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < deadline:
        await ws.send(json.dumps({"cmd": "list_shared_terminals"}))
        msgs = await recv_until(
            ws, lambda m: m.get("type") == "shared_terminals", timeout=10
        )
        for m in msgs:
            if m.get("type") == "shared_terminals":
                last = m.get("terminals", [])
                if any(t.get("window_id") == window_id for t in last):
                    return last
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"window {window_id} never appeared in shared_terminals; "
        f"last list: {last}"
    )


async def test_share_and_join(appliance, api, auth):
    owner = auth
    visitor = _register(
        api, f"visitor-{uuid.uuid4().hex[:8]}@example.com", "visitorpass"
    )

    resp = api.post(
        "/api/v1/workspaces",
        headers=owner["headers"],
        json={"name": f"super-shared-{uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code == 200, resp.text
    ws_id = resp.json()["id"]

    # Grant the visitor the coders role (view/terminal/files).
    resp = api.post(
        f"/api/v1/workspaces/{ws_id}/roles/coders",
        headers=owner["headers"],
        json={"email": visitor["email"]},
    )
    assert resp.status_code == 200, resp.text

    owner_ws = await connect_workspace(appliance, owner["token"], ws_id)
    vis_ws = None
    try:
        await _start_terminal(owner_ws)
        msgs = await recv_until(
            owner_ws, lambda m: _windows([m]) is not None, timeout=60
        )
        windows = _windows(msgs)
        assert windows, f"owner never got terminal_windows; msgs: {msgs}"
        window = windows[0]

        # Share it, then wait until the shared list carries it.
        await owner_ws.send(
            json.dumps({"cmd": "share_window", "window_id": window["id"]})
        )
        await _wait_shared_contains(owner_ws, window["id"])

        # The visitor connects, sees the shared window, and joins it.
        vis_ws = await connect_workspace(appliance, visitor["token"], ws_id)
        vis_shared = await _wait_shared_contains(vis_ws, window["id"])
        target = next(
            (t for t in vis_shared if t.get("window_id") == window["id"]),
            None,
        )
        assert target, f"visitor cannot see shared window: {vis_shared}"
        await vis_ws.send(
            json.dumps(
                {
                    "cmd": "join_shared_terminal",
                    "user_id": target.get("user_id"),
                    "window_id": window["id"],
                }
            )
        )
        joined = await recv_until(
            vis_ws,
            lambda m: m.get("type") == "terminal_started",
            timeout=60,
        )
        assert any(m.get("type") == "terminal_started" for m in joined), (
            f"visitor join produced no terminal_started; msgs: {joined}"
        )
    finally:
        if vis_ws is not None:
            await vis_ws.close()
        await owner_ws.close()
        api.delete(f"/api/v1/workspaces/{ws_id}", headers=owner["headers"])
