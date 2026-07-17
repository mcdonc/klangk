"""End-to-end tests for the agent-home behaviors added in #1157.

#1157 shipped two container-bringup behaviors that the unit suite can
only *mock*:

1. ``KLANGK_AGENT_HOME=/home/<agent_handle>`` is baked into the
   container env at creation (``_build_env``), so **every** exec
   process inside the container inherits it.  The unit test only proves
   ``_build_env`` emits the string; here we prove podman actually
   inherits it across a real ``exec``.

2. The agent home (``/home/<agent_handle>`` + a populated
   ``~/.pi/agent/``) is provisioned **eagerly at bring-up** via
   ``ensure_agent_home`` -- not lazily at the first chat mention.

   Crucially, eager provisioning lives only in ``start_workspace``
   (triggered by ``auto_start=True`` on workspace creation), *not* the
   normal WS connect path.  So the eager test creates a workspace with
   ``auto_start=True`` and inspects the filesystem via ``podman exec``
   **without ever connecting or chatting** -- the behavioral lock that
   the home exists at start, before any mention.

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

from _e2e_server import start_server, stop_server, ws_connect as _ws_dial

# Default agent handle (see model/users.py: _DEFAULT_AGENT_HANDLE).  Not
# imported from the backend to keep the e2e test decoupled from the
# server's internals -- the test asserts against observable container
# state, not the Python API.
AGENT_HANDLE = "clanker"
AGENT_HOME = f"/home/{AGENT_HANDLE}"


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server for the test module.

    KLANGK_ALLOW_AUTOSTART=1 is required so the eager-provisioning test
    can create a workspace with auto_start=True (which routes through
    start_workspace -> ensure_agent_home).
    """
    server = start_server(
        KLANGK_JWT_SECRET="agent-home-e2e-secret",
        KLANGK_PREVENT_INSECURE_JWT_SECRET="",
        KLANGK_DEFAULT_USER="test@example.com",
        KLANGK_DEFAULT_PASSWORD="testpass",
        KLANGK_TEST_MODE="1",
        KLANGK_IDLE_TIMEOUT_SECONDS="300",
        KLANGK_ALLOW_AUTOSTART="1",
        LOGFIRE_TOKEN="",
        KLANGK_LLM_BASE_URL="",
        KLANGK_LLM_API_KEY="",
        KLANGK_LLM_MODEL="",
    )

    # Fetch auto-generated instance ID from the running server (used by
    # _container_id_for_workspace for deterministic container-name lookups).
    config_resp = server["client"].get("/api/v1/config", timeout=10)
    server["instance_id"] = (
        config_resp.json().get("instance_id", "")
        if config_resp.status_code == 200
        else ""
    )

    yield server

    stop_server(server)


def _rm_containers(instance_id):
    """Remove any containers labeled with our instance id."""
    if not instance_id:
        return
    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            f"label=klangk.instance={instance_id}",
            "-q",
        ],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        subprocess.run(
            ["podman", "rm", "-f", *result.stdout.strip().split()],
            capture_output=True,
        )


def _container_id_for_workspace(workspace_id, instance_id):
    """Return the running container id for a specific workspace.

    The container name is deterministic (``klangk-{instance_id}-{ws[:12]}``),
    so filtering by name targets the exact workspace -- never a stale
    container left over from another test/run under the same instance.
    """
    name = f"klangk-{instance_id}-{workspace_id[:12]}"
    result = subprocess.run(
        [
            "podman",
            "ps",
            "--filter",
            f"name=^{name}$",
            "-q",
        ],
        capture_output=True,
        text=True,
    )
    return [c for c in result.stdout.strip().split() if c]


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
        """KLANGK_AGENT_HOME is baked at container start and inherited by
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
                    ["bash", "-c", 'echo "$KLANGK_AGENT_HOME"'],
                )
                # Exact value: the default agent handle's home.
                assert output.strip() == AGENT_HOME, (
                    f"expected KLANGK_AGENT_HOME={AGENT_HOME!r}, "
                    f"got {output!r}"
                )
            finally:
                await ws.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_agent_home_provisioned_eagerly(self, server, auth):
        """The agent home exists immediately after a container is brought
        up via start_workspace -- with NO chat mention and NO WS
        connection preceding the check (#1157).

        auto_start=True routes creation through start_workspace,
        which calls ensure_agent_home on a freshly-created container
        (status == "created").  We then inspect the filesystem directly
        via podman exec, as root, so the check is independent of any
        user's read permissions.
        """
        # auto_start triggers start_workspace. The service command fires
        # at the create choke point (bringup), but only once setup_state
        # is complete; agent-home provisioning always runs at create.
        workspace_id, cleanup = create_workspace(
            server, auth, auto_start=True, setup_state="complete"
        )
        try:
            # Wait for the eagerly-started container to be running.
            # create_workspace awaits start_workspace, so the
            # container is up by the time the POST returned; poll as a
            # belt-and-suspenders against scheduling latency.  Filter by
            # the deterministic container name so we target THIS
            # workspace's container, never a stale one.
            cids = []
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                cids = _container_id_for_workspace(
                    workspace_id, server["instance_id"]
                )
                if cids:
                    break
                time.sleep(0.5)
            assert cids, (
                "eagerly-started container never appeared in podman ps"
            )
            cid = cids[0]

            # Verify the agent home dir + populated ~/.pi/agent/ exist.
            # Run as root so the existence check is independent of
            # per-user read permissions on the agent's home.
            check = (
                "test -d {home}"
                " && test -f {home}/.pi/agent/settings.json"
                " && test -f {home}/.pi/agent/models.json"
                " && echo ALL_PRESENT"
            ).format(home=AGENT_HOME)
            result = subprocess.run(
                ["podman", "exec", "-u", "root", cid, "bash", "-c", check],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert "ALL_PRESENT" in result.stdout, (
                f"agent home not fully provisioned at {AGENT_HOME}"
                f" (rc={result.returncode}):\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}"
            )
        finally:
            cleanup()
