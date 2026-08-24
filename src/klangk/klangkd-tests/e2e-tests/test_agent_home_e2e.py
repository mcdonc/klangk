"""E2E lock for the ``KLANGKWS_AGENT_HOME`` container env var (#1157).

The chat feature (and with it the agent-home provisioning) was removed,
but the constant survives: ``KLANGKWS_AGENT_HOME=/home/klangk`` is baked
into every container's env under both home layouts, and the sandbox
``setup.sh`` contract reads it.  The unit suite only proves ``build_env``
emits the string; here we prove podman actually inherits it across a
real ``exec``.

Requires: podman available, klangk image built.

Run with: devenv shell -- test-backend-e2e
"""

import asyncio
import base64
import json

import httpx
import pytest

from _e2e_server import start_server, stop_server, ws_connect as _ws_dial

# Fixed agent-identity home (#2718): the agent user *is* the ``klangk``
# user, so its home is the constant ``/home/klangk`` under both layouts.
# Not imported from the backend to keep the e2e test decoupled from the
# server's internals -- the test asserts against observable container
# state, not the Python API.
AGENT_HOME = "/home/klangk"


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server for the test module."""
    server = start_server(
        KLANGKD_JWT_SECRET="agent-home-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
    )

    yield server

    stop_server(server)


@pytest.fixture(scope="module")
def auth(server):
    resp = server["client"].post(
        "/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


_ws_counter = 0


def create_workspace(server, auth, **fields):
    """Create a workspace; return (id, cleanup). Extra fields go in the body."""
    global _ws_counter  # noqa: PLW0603
    _ws_counter += 1
    name = fields.pop("name", f"agent-home-e2e-{_ws_counter}")
    client = server["client"]
    resp = client.post(
        "/api/v1/workspaces",
        headers=auth["headers"],
        json={"name": name, **fields},
        timeout=30,
    )
    assert resp.status_code == 200, resp.text
    workspace_id = resp.json()["id"]

    def cleanup():
        try:
            client.delete(
                f"/api/v1/workspaces/{workspace_id}",
                headers=auth["headers"],
                timeout=30,
            )
        except httpx.ReadTimeout:
            pass

    return workspace_id, cleanup


# --- WS / exec helpers (modeled on test_per_user_home.py) ---


def _is_container_ready(msg):
    if msg.get("type") == "container_ready":
        return True
    if msg.get("type") == "event":
        event = msg.get("event", {})
        return (
            event.get("type") == "CUSTOM"
            and event.get("name") == "container_ready"
        )
    return False


def _is_exec_exit(msg):
    return msg.get("type") == "exec_exit"


async def recv_until(ws, predicate, timeout=30):
    deadline = asyncio.get_event_loop().time() + timeout
    messages = []
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=1)
            data = json.loads(msg)
            messages.append(data)
            if predicate(data):
                return messages
        except asyncio.TimeoutError:
            continue
    return messages


async def ws_connect(server, auth, workspace_id):
    """Open a WS, connect, wait for container_ready."""
    ws = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
    await ws.send(
        json.dumps({"cmd": "workspace_connect", "workspaceId": workspace_id})
    )
    deadline = asyncio.get_event_loop().time() + 60
    while asyncio.get_event_loop().time() < deadline:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
        if _is_container_ready(msg):
            break
    else:
        await ws.close()
        raise AssertionError("container_ready not received within 60s")
    return ws


async def exec_command(ws, command):
    """Run a command via the WS exec path; return decoded stdout+stderr."""
    await ws.send(json.dumps({"cmd": "exec_start", "command": command}))
    msgs = await recv_until(ws, _is_exec_exit, timeout=15)
    outputs = [m for m in msgs if m.get("type") == "exec_output"]
    return b"".join(base64.b64decode(m["data"]) for m in outputs).decode()


class TestAgentHomeE2E:
    @pytest.mark.asyncio
    async def test_agent_home_env_present_in_exec(self, server, auth):
        """KLANGKWS_AGENT_HOME is baked at container start and inherited by
        every exec process (#1157).  The WS exec path spawns a process
        inside the container via the server's exec machinery (the same
        path terminals use) -- it does *not* pass the var per-call, so
        observing it here proves podman inherited it from the container
        env, not that _build_env merely emitted a string.
        """
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                output = await exec_command(
                    ws,
                    ["bash", "-c", 'echo "$KLANGKWS_AGENT_HOME"'],
                )
                # Exact value: the fixed agent-identity home.
                assert output.strip() == AGENT_HOME, (
                    f"expected KLANGKWS_AGENT_HOME={AGENT_HOME!r}, "
                    f"got {output!r}"
                )
            finally:
                await ws.close()
        finally:
            cleanup()
