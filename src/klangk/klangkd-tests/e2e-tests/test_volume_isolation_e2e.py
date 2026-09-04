"""Workspace-owned named-volume E2E tests (#3153).

Runtime verification, against a real klangkd + real podman containers,
that one workspace cannot mount another workspace's named volumes:

1. Foreign-volume attack — a volume owned by ANOTHER WORKSPACE
   (auto-created by that workspace's start) cannot be started-mounted:
   the create passes (shape-only validation), the start fails 400
   "belongs to another workspace".
2. Any-starter positive — a volume created by a member's cold connect
   is owned by the WORKSPACE (stamped with its id, no user stamp —
   asserted via podman inspect), so the owner can stop and restart the
   workspace afterwards and still see the member's data in the same
   volume.

Requires: podman available, klangk image built.

Run with: devenv shell -- test-backend-e2e test_volume_isolation_e2e.py
"""

import asyncio
import base64
import json
import subprocess
import time
import uuid

import httpx
import pytest

from _e2e_server import start_server, stop_server, ws_connect as _ws_dial


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server for the test module."""
    server = start_server(
        KLANGKD_JWT_SECRET="voliso-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="3600",
        LOGFIRE_TOKEN="",
    )
    yield server
    stop_server(server)


def register(api, email, password="testpass"):
    """Register a user (test mode); return (headers, user_id)."""
    resp = api.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password},
        timeout=30,
    )
    assert resp.status_code == 200, f"Register failed: {resp.text}"
    data = resp.json()
    token = data.get("access_token")
    assert token, f"No token in register response: {resp.text}"
    return {"Authorization": f"Bearer {token}"}, data.get("user_id")


def login(api, email, password):
    """Login and return auth headers."""
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": password},
        timeout=30,
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def group_id_by_name(api, headers, name):
    """Find a workspace-role group's id by exact name."""
    resp = api.get(
        "/api/v1/groups",
        headers=headers,
        params={"source": "workspace-role", "page_size": 200},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    for group in resp.json().get("groups", []):
        if group.get("name") == name:
            return group["id"]
    raise AssertionError(f"group {name!r} not found")


def add_group_member(api, headers, group_name, user_id):
    """Add a user to a group by group name."""
    group_id = group_id_by_name(api, headers, group_name)
    resp = api.post(
        f"/api/v1/groups/{group_id}/members",
        headers=headers,
        json={"user_id": user_id},
        timeout=10,
    )
    assert resp.status_code in (200, 201), resp.text


@pytest.fixture(scope="module")
def users(server):
    """Two plain (non-admin) member users (alice, bob) + admin headers."""
    api = server["client"]
    alice_headers, alice_id = register(api, "alice@example.com")
    bob_headers, bob_id = register(api, "bob@example.com")
    admin = login(api, "admin@example.com", "adminpass")
    return {
        "alice": alice_headers,
        "bob": bob_headers,
        "alice_id": alice_id,
        "bob_id": bob_id,
        "admin": admin,
    }


def create_workspace(api, headers, name, mounts=None):
    """Create a workspace row (no container) and return its id."""
    body = {"name": name}
    if mounts is not None:
        body["mounts"] = mounts
    resp = api.post(
        "/api/v1/workspaces", headers=headers, json=body, timeout=10
    )
    assert resp.status_code == 200, f"Create {name!r} failed: {resp.text}"
    return resp.json()["id"]


def delete_workspace(api, headers, workspace_id):
    """Best-effort workspace delete (stops + removes the container)."""
    try:
        api.delete(
            f"/api/v1/workspaces/{workspace_id}", headers=headers, timeout=60
        )
    except httpx.HTTPError:
        pass


# --- WS exec helpers (same pattern as test_per_user_home.py) ---


def is_container_ready(msg):
    if msg.get("type") != "event":
        return False
    event = msg.get("event", {})
    return (
        event.get("type") == "CUSTOM"
        and event.get("name") == "container_ready"
    )


def is_exec_exit(msg):
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


async def wait_for_message(ws, predicate, timeout=60, what="message"):
    """Drain until ``predicate`` matches; raise on timeout (a silent
    fall-through would surface later as a confusing exec failure
    instead of "never became ready")."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for {what}")
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
        if predicate(msg):
            return


async def ws_connect(server, headers, workspace_id):
    """Open a WebSocket, connect to the workspace, wait until ready."""
    token = headers["Authorization"].removeprefix("Bearer ")
    ws = await _ws_dial(server, f"/ws?token={token}", max_size=2**20)
    await ws.send(
        json.dumps({"cmd": "workspace_connect", "workspaceId": workspace_id})
    )
    await wait_for_message(
        ws,
        lambda m: m.get("type") == "container_ready" or is_container_ready(m),
        what="initial container_ready",
    )
    await ws.send(json.dumps({"cmd": "ui_ready"}))
    await wait_for_message(
        ws, is_container_ready, what="container_ready after ui_ready"
    )
    return ws


async def exec_command(ws, command):
    """Run a command via exec and return combined output."""
    await ws.send(json.dumps({"cmd": "exec_start", "command": command}))
    msgs = await recv_until(ws, is_exec_exit, timeout=15)
    outputs = [m for m in msgs if m.get("type") == "exec_output"]
    return b"".join(base64.b64decode(m["data"]) for m in outputs).decode()


class TestVolumeOwnership:
    # Server startup (readiness can take minutes under podman
    # contention) is billed to this first module test.
    @pytest.mark.timeout(600)
    def test_foreign_workspace_volume_rejected_at_start(self, server, users):
        """A volume auto-created by bob's workspace cannot be
        started-mounted by alice's workspace — create passes (shape-only
        validation), start fails 400 'belongs to another workspace'."""
        api = server["client"]
        volume = f"klangke2e{uuid.uuid4().hex[:10]}"

        # bob's workspace start auto-creates the volume, stamped with
        # bob's workspace id
        bob_ws = create_workspace(
            api,
            users["bob"],
            f"voliso-bvol-{uuid.uuid4().hex[:6]}",
            mounts=[f"{volume}:/mnt/vol"],
        )
        try:
            resp = api.post(
                f"/api/v1/workspaces/{bob_ws}/start",
                headers=users["bob"],
                timeout=60,
            )
            assert resp.status_code == 200, resp.text

            # alice's create passes (shape-only); the START is where
            # workspace ownership is enforced
            alice_ws = create_workspace(
                api,
                users["alice"],
                f"voliso-foreign-{uuid.uuid4().hex[:6]}",
                mounts=[f"{volume}:/mnt/vol"],
            )
            try:
                resp = api.post(
                    f"/api/v1/workspaces/{alice_ws}/start",
                    headers=users["alice"],
                    timeout=60,
                )
                assert resp.status_code == 400, resp.text
                assert "belongs to another workspace" in resp.json().get(
                    "detail", ""
                )
            finally:
                delete_workspace(api, users["alice"], alice_ws)
        finally:
            delete_workspace(api, users["bob"], bob_ws)
            subprocess.run(
                ["podman", "volume", "rm", volume], capture_output=True
            )

    # One member cold connect + one owner restart: two bringups.
    @pytest.mark.timeout(600)
    @pytest.mark.asyncio
    async def test_member_created_volume_restartable_by_owner(
        self, server, users
    ):
        """A volume created by a member's cold connect is owned by the
        WORKSPACE (stamped with its id, no user stamp), so the owner
        can stop and restart it afterwards — and see the member's
        data in the same volume."""
        api = server["client"]
        volume = f"klangke2ews{uuid.uuid4().hex[:10]}"
        ws = create_workspace(
            api,
            users["alice"],
            f"voliso-ws-{uuid.uuid4().hex[:6]}",
            mounts=[f"{volume}:/mnt/vol"],
        )
        try:
            # bob joins the workspace as a collaborator, then his cold
            # connect auto-creates the volume — owned by the workspace
            add_group_member(
                api, users["admin"], f"collaborators-{ws}", users["bob_id"]
            )
            ws_bob = await ws_connect(server, users["bob"], ws)
            try:
                out = (
                    await exec_command(
                        ws_bob,
                        [
                            "bash",
                            "-c",
                            "echo bob-data > /mnt/vol/marker "
                            "&& cat /mnt/vol/marker",
                        ],
                    )
                ).strip()
                assert "bob-data" in out
            finally:
                await ws_bob.close()

            # the podman volume is stamped with the workspace, and
            # carries NO user stamp (#3153: workspace ownership)
            inspected = subprocess.run(
                ["podman", "volume", "inspect", volume],
                capture_output=True,
                text=True,
                check=True,
            )
            labels = json.loads(inspected.stdout)[0].get("Labels") or {}
            assert labels.get("klangk.workspace-id") == ws
            assert "klangk.user-id" not in labels

            # owner stop + restart: any legitimate starter may use the
            # workspace's volume
            resp = api.post(
                f"/api/v1/workspaces/{ws}/stop",
                headers=users["alice"],
                timeout=60,
            )
            assert resp.status_code == 200, resp.text
            resp = api.post(
                f"/api/v1/workspaces/{ws}/start",
                headers=users["alice"],
                timeout=60,
            )
            assert resp.status_code == 200, resp.text

            # the owner's container sees bob's marker — same volume,
            # not recreated
            ws_alice = await ws_connect(server, users["alice"], ws)
            try:
                out = (
                    await exec_command(
                        ws_alice, ["bash", "-c", "cat /mnt/vol/marker"]
                    )
                ).strip()
                assert "bob-data" in out
            finally:
                await ws_alice.close()
        finally:
            delete_workspace(api, users["alice"], ws)
            subprocess.run(
                ["podman", "volume", "rm", volume], capture_output=True
            )
