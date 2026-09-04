"""Workspace volume-isolation E2E tests (#3153).

Runtime verification that one workspace container cannot reach another
workspace's data through mounts — the end-to-end counterpart of the
unit-tested validation layer (``test_container.py``'s mount validators).

Covers the attack surfaces plus a runtime audit of the real
containers' mount tables:

1. Home isolation — a marker written in workspace A's home is invisible
   from another workspace of the same user AND from another user's
   workspace (per-handle homes, ``KLANGKD_PER_HANDLE_HOME=true``).
2. Protected-source attack — a bind mount pointing at another
   workspace's home host path is rejected (400, protected host path).
3. Traversal attack — a spec whose source normalizes (``..``) into
   another workspace's home is rejected the same way.
4. Foreign-volume attack — a named volume owned by another user
   (auto-created via their workspace start) cannot be mounted: the
   start fails 400 ("belongs to another user or workspace") unless
   the volume's workspace or creator matches this start (#3153
   follow-up: volumes are workspace-scoped — see
   TestWorkspaceScopedVolumes for the positive side).
5. Runtime audit — ``podman inspect`` on every workspace container:
   each mount source under the workspaces root belongs to that
   workspace's own subtree, and the sets are pairwise disjoint.

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

# Containers must outlive the module's individual tests (the runtime
# audit needs them running), so the idle timeout is stretched far past
# the suite default.
IDLE_TIMEOUT = "3600"


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server for the test module."""
    server = start_server(
        KLANGKD_JWT_SECRET="voliso-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="admin@example.com",
        KLANGKD_DEFAULT_PASSWORD="adminpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS=IDLE_TIMEOUT,
        KLANGKD_PER_HANDLE_HOME="true",
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
    """Two plain (non-admin) member users: alice and bob."""
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


def sweep_instance_volumes(server):
    """Best-effort removal of this instance's named podman volumes.

    Volumes live in podman storage, outside the data dir that
    ``stop_server`` rmtree's — without this sweep a hard-killed worker
    leaves ``klangke2e*`` volumes behind on the runner (matching the
    container hygiene ``stop_server`` already applies).
    """
    try:
        with open(f"{server['data_dir']}/instance-id") as fh:
            instance = fh.read().strip()
        listed = subprocess.run(
            [
                "podman",
                "volume",
                "ls",
                "-q",
                "--filter",
                f"label=klangk.instance={instance}",
            ],
            capture_output=True,
            text=True,
        )
        if listed.stdout.split():
            subprocess.run(
                ["podman", "volume", "rm", *listed.stdout.split()],
                capture_output=True,
            )
    except OSError:
        pass


def delete_workspace(api, headers, workspace_id):
    """Best-effort workspace delete (stops + removes the container)."""
    try:
        api.delete(
            f"/api/v1/workspaces/{workspace_id}", headers=headers, timeout=60
        )
    except httpx.HTTPError:
        pass


@pytest.fixture(scope="module")
def workspaces(server, users):
    """Three workspace rows: two owned by alice, one by bob.

    Created un-started — the tests that need containers connect (the
    connect path starts them); the mount-attack tests only need the
    rows to exist so their home paths are attack targets.
    """
    api = server["client"]
    suffix = uuid.uuid4().hex[:8]
    made = {
        "alice1": create_workspace(api, users["alice"], f"voliso-a1-{suffix}"),
        "alice2": create_workspace(api, users["alice"], f"voliso-a2-{suffix}"),
        "bob": create_workspace(api, users["bob"], f"voliso-b-{suffix}"),
    }
    yield made
    delete_workspace(api, users["alice"], made["alice1"])
    delete_workspace(api, users["alice"], made["alice2"])
    delete_workspace(api, users["bob"], made["bob"])
    sweep_instance_volumes(server)


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


# --- podman helpers (runtime audit) ---


def podman_mounts_for(workspace_id):
    """The container's mount list from ``podman inspect`` (by label)."""
    listed = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "-q",
            "--filter",
            f"label=klangk.workspace={workspace_id}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    ids = listed.stdout.split()
    assert ids, f"No container found for workspace {workspace_id}"
    inspected = subprocess.run(
        ["podman", "inspect", *ids],
        capture_output=True,
        text=True,
        check=True,
    )
    mounts = []
    for container in json.loads(inspected.stdout):
        mounts += container.get("Mounts") or []
    return mounts


def workspace_root_sources(server, workspace_id):
    """Mount sources under the workspaces root, resolved on the host."""
    root = f"{server['data_dir']}/workspaces"
    sources = set()
    for mount in podman_mounts_for(workspace_id):
        source = mount.get("Source", "")
        if source.startswith(root):
            sources.add(source)
    return sources


class TestHomeIsolation:
    # Three sequential container bringups (plus the module server's
    # startup, billed to this first test) can outrun the conftest's
    # 300s default on a contended runner — validated under 600s.
    @pytest.mark.timeout(600)
    @pytest.mark.asyncio
    async def test_home_marker_not_shared_across_workspaces(
        self, server, users, workspaces
    ):
        """A home-dir marker is invisible from every other workspace —
        same user's other workspace and another user's workspace."""
        marker = f".e2e-voliso-{uuid.uuid4().hex[:8]}"

        ws_a1 = await ws_connect(server, users["alice"], workspaces["alice1"])
        try:
            home = (
                await exec_command(ws_a1, ["bash", "-c", "echo $HOME"])
            ).strip()
            assert home == "/home/alice", f"alice's HOME: {home!r}"
            # Write AND read back: if the write silently failed, the
            # ABSENT checks below would pass vacuously — a false green
            # on exactly the invariant this test exists for.
            await exec_command(
                ws_a1, ["bash", "-c", f"echo x > $HOME/{marker}"]
            )
            readback = (
                await exec_command(
                    ws_a1, ["bash", "-c", f"cat $HOME/{marker}"]
                )
            ).strip()
            assert readback == "x", (
                f"marker write in alice1 failed (readback {readback!r})"
            )
        finally:
            await ws_a1.close()

        ws_a2 = await ws_connect(server, users["alice"], workspaces["alice2"])
        try:
            out = (
                await exec_command(
                    ws_a2,
                    [
                        "bash",
                        "-c",
                        f"test -e $HOME/{marker} "
                        "&& echo PRESENT || echo ABSENT",
                    ],
                )
            ).strip()
            assert "ABSENT" in out and "PRESENT" not in out
        finally:
            await ws_a2.close()

        ws_b = await ws_connect(server, users["bob"], workspaces["bob"])
        try:
            home = (
                await exec_command(ws_b, ["bash", "-c", "echo $HOME"])
            ).strip()
            assert home == "/home/bob", f"bob's HOME: {home!r}"
            out = (
                await exec_command(
                    ws_b,
                    [
                        "bash",
                        "-c",
                        f"test -e /home/alice/{marker} "
                        "&& echo PRESENT || echo ABSENT",
                    ],
                )
            ).strip()
            assert "ABSENT" in out and "PRESENT" not in out
        finally:
            await ws_b.close()


class TestMountAttacks:
    def test_bind_other_workspace_home_rejected(
        self, server, users, workspaces
    ):
        """Bind-mounting another workspace's home host path → 400."""
        api = server["client"]
        victim = f"{server['data_dir']}/workspaces/{workspaces['bob']}/home"
        resp = api.post(
            "/api/v1/workspaces",
            headers=users["alice"],
            json={
                "name": f"voliso-attack-{uuid.uuid4().hex[:6]}",
                "mounts": [f"{victim}:/mnt/pwn"],
            },
            timeout=10,
        )
        assert resp.status_code == 400, resp.text
        assert "protected" in resp.json().get("detail", "")

    def test_put_other_workspace_home_rejected(
        self, server, users, workspaces
    ):
        """The PUT (update) path validates mounts through the same
        gate as create — a protected source cannot be smuggled in by
        editing an existing workspace."""
        api = server["client"]
        victim = f"{server['data_dir']}/workspaces/{workspaces['bob']}/home"
        resp = api.put(
            f"/api/v1/workspaces/{workspaces['alice2']}",
            headers=users["alice"],
            json={"mounts": [f"{victim}:/mnt/pwn"]},
            timeout=10,
        )
        assert resp.status_code == 400, resp.text
        assert "protected" in resp.json().get("detail", "")

    def test_traversal_spec_rejected(self, server, users, workspaces):
        """A ``..``-laden source normalizing into another workspace's
        home is rejected (realpath-resolved protection)."""
        api = server["client"]
        ws_b = workspaces["bob"]
        victim = f"{server['data_dir']}/workspaces/{ws_b}/../{ws_b}/home"
        resp = api.post(
            "/api/v1/workspaces",
            headers=users["alice"],
            json={
                "name": f"voliso-trav-{uuid.uuid4().hex[:6]}",
                "mounts": [f"{victim}:/mnt/pwn"],
            },
            timeout=10,
        )
        assert resp.status_code == 400, resp.text
        assert "protected" in resp.json().get("detail", "")

    def test_foreign_named_volume_rejected_at_start(self, server, users):
        """A named volume owned by bob cannot be started-mounted by
        alice — the start fails 400 'belongs to another user'."""
        api = server["client"]
        volume = f"klangke2e{uuid.uuid4().hex[:10]}"

        # bob's workspace start auto-creates the volume, labeled to bob
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

            # alice's create passes (shape-only validation); the START
            # is where ownership is enforced
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
                assert "belongs to another user or workspace" in (
                    resp.json().get("detail", "")
                )
            finally:
                delete_workspace(api, users["alice"], alice_ws)
        finally:
            delete_workspace(api, users["bob"], bob_ws)
            subprocess.run(
                ["podman", "volume", "rm", volume],
                capture_output=True,
            )


class TestWorkspaceScopedVolumes:
    # #3153: volumes created at workspace start are tagged with the
    # workspace, so ANY legitimate starter of that workspace can use
    # them. Two bringups (member cold connect, owner restart + connect).
    @pytest.mark.timeout(600)
    @pytest.mark.asyncio
    async def test_member_created_volume_restartable_by_owner(
        self, server, users
    ):
        """A volume created by a member's cold connect (stamped with
        HIS user id) is still startable by the OWNER afterwards — the
        workspace label matches where the creating user does not."""
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
            # connect auto-creates the volume under HIS user id
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

            # the podman volume is stamped with the workspace AND bob
            inspected = subprocess.run(
                ["podman", "volume", "inspect", volume],
                capture_output=True,
                text=True,
                check=True,
            )
            labels = json.loads(inspected.stdout)[0].get("Labels") or {}
            assert labels.get("klangk.workspace-id") == ws
            assert labels.get("klangk.user-id") == users["bob_id"]

            # owner stop + restart: allowed only via the workspace match
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


class TestRuntimeMountAudit:
    # Self-sufficient: starts (or adopts) each container via the API
    # before inspecting. Under `-n 2 --dist=loadscope` the module's
    # classes can split across xdist workers (the class is the
    # distribution unit), each with its own module fixtures — so this
    # test must not rely on TestHomeIsolation having run in the same
    # worker (CI run 33886832062: "No container found").
    @pytest.mark.timeout(600)
    def test_no_cross_workspace_mount_sources(self, server, users, workspaces):
        """podman inspect: every workspaces-root mount source belongs to
        its own workspace's subtree, and the sets are disjoint."""
        api = server["client"]
        for key, owner in (
            ("alice1", "alice"),
            ("alice2", "alice"),
            ("bob", "bob"),
        ):
            resp = api.post(
                f"/api/v1/workspaces/{workspaces[key]}/start",
                headers=users[owner],
                timeout=120,
            )
            assert resp.status_code == 200, f"{key} start: {resp.text}"
        root = f"{server['data_dir']}/workspaces"
        for key, owner in (
            ("alice1", "alice"),
            ("alice2", "alice"),
            ("bob", "bob"),
        ):
            ws_id = workspaces[key]
            home_mounts = [
                m
                for m in podman_mounts_for(ws_id)
                if m.get("Destination") == "/home"
            ]
            assert home_mounts, f"{key}: no /home mount in container"
            assert home_mounts[0]["Source"] == f"{root}/{ws_id}/home", (
                f"{key}: /home mounted from {home_mounts[0]['Source']}"
            )

        sources = {
            key: workspace_root_sources(server, ws_id)
            for key, ws_id in workspaces.items()
        }
        for key, ws_id in workspaces.items():
            own = f"{root}/{ws_id}"
            for source in sources[key]:
                assert source.startswith(own + "/") or source == own, (
                    f"{key} mounts foreign source {source}"
                )
        pairs = [(a, b) for a in sources for b in sources if a < b]
        for a, b in pairs:
            overlap = sources[a] & sources[b]
            assert not overlap, f"{a} and {b} share mount sources {overlap}"
