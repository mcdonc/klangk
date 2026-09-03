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
import subprocess
import time

import httpx
import pytest

from _e2e_env import ci_budget
from _e2e_server import start_server, stop_server, ws_connect as _ws_dial

# Fixed agent-identity home (#2718): the agent user *is* the ``klangk``
# user, so its home is the constant ``/home/klangk`` under both layouts.
# Not imported from the backend to keep the e2e test decoupled from the
# server's internals -- the test asserts against observable container
# state, not the Python API.
AGENT_HOME = "/home/klangk"


def _container_id_for_workspace(workspace_id):
    """Return the running workspace container id(s) for a workspace.

    Looked up by the ``klangk.workspace`` correlation label + ``role=workspace``
    (#2286) so we target the exact workspace, never a stale container from
    another test/run (and never the network sidecar, which shares the label).
    """
    result = subprocess.run(
        [
            "podman",
            "ps",
            "--filter",
            f"label=klangk.workspace={workspace_id}",
            "--filter",
            "label=klangk.role=workspace",
            "-q",
        ],
        capture_output=True,
        text=True,
    )
    return [c for c in result.stdout.strip().split() if c]


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
        KLANGKD_ALLOW_AUTOSTART="1",
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
    # #3064: the wait spans the server's whole bring-up chain, whose
    # budgets widen on CI — widen with them.
    budget = ci_budget(60, 240)
    ws = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
    await ws.send(
        json.dumps({"cmd": "workspace_connect", "workspaceId": workspace_id})
    )
    deadline = asyncio.get_event_loop().time() + budget
    while asyncio.get_event_loop().time() < deadline:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=budget))
        if _is_container_ready(msg):
            break
    else:
        await ws.close()
        raise AssertionError(f"container_ready not received within {budget}s")
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

    @pytest.mark.asyncio
    async def test_agent_home_materialized_eagerly(self, server, auth):
        """The shared home exists immediately after a container is brought
        up via start_workspace — with NO WS connection preceding the check.

        Nothing but the create choke point (``bringup`` →
        ``ensure_shared_home``) materializes ``/home/klangk`` — the home
        volume mounts at ``/home`` and shadows the image's own content —
        so a populated ``/home/klangk`` (with ``.profile``) must be there
        before the ``service`` session's first login shell, including on
        the boot path where no user ever connects (#2717).
        auto_start=True routes creation through start_workspace, so the
        container is up by the time the POST returned; we inspect the
        filesystem directly via podman exec as root, independent of any
        user's read permissions.
        """
        workspace_id, cleanup = create_workspace(
            server, auth, auto_start=True, setup_state="complete"
        )
        try:
            cids = []
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                cids = _container_id_for_workspace(workspace_id)
                if cids:
                    break
                time.sleep(0.5)
            assert cids, (
                "eagerly-started container never appeared in podman ps"
            )
            cid = cids[0]

            check = (
                "test -d /home/klangk"
                " && test -f /home/klangk/.profile"
                " && echo ALL_PRESENT"
            )
            result = subprocess.run(
                ["podman", "exec", "-u", "root", cid, "bash", "-c", check],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert "ALL_PRESENT" in result.stdout, (
                f"agent home not materialized at /home/klangk"
                f" (rc={result.returncode}):\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}"
            )
        finally:
            cleanup()
