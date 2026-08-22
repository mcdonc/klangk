"""Memory-pressure eviction E2E tests (#2526, #2627).

Exercises the real loop against a real klangkd + podman. Pressure is
forced purely via settings — ``KLANGKD_MEMORY_EVICTION_THRESHOLD_PERCENT``
high enough (99.5%) that no real host can report that much available
memory — so the eviction episode deterministically opens and every
assertion (WS event, status flip, restart-on-reconnect) runs against
the production code path.

Own module-scoped server (mandatory: with eviction armed this
aggressively, every idle workspace on the server is a target).

Requires: podman available, klangk image built.

Run with: devenv shell -- test-backend-e2e -k memory_eviction
"""

import asyncio
import json

import httpx
import pytest

from _e2e_server import start_server, stop_server, ws_connect as _ws_dial

# Threshold/recovery pair that no real host can satisfy: MemAvailable
# never reaches 99.5% of MemTotal on a booted machine (kernel slab,
# page tables, wired pages — this very host idles at ~77%), and the
# cgroup dimension (when limited) is tighter still. min() of the two
# fractions is thus always below the threshold → permanently
# "pressured" the moment the loop starts.
PRESSURE_THRESHOLD = "99.5"
PRESSURE_RECOVERY = "100"

_ws_counter = 0


@pytest.fixture(scope="module")
def server():
    """klangkd with eviction armed to fire at the first poll.

    sustain=1 × poll=1s (the floor) → the first eviction lands ~2s
    after the loop starts. Idle timeout is long so the idle monitor
    never competes with the evictor for victims.
    """
    server = start_server(
        KLANGKD_JWT_SECRET="mem-evict-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="3600",
        LOGFIRE_TOKEN="",
        # --- eviction armed ---
        KLANGKD_MEMORY_EVICTION_ENABLED="true",
        KLANGKD_MEMORY_EVICTION_THRESHOLD_PERCENT=PRESSURE_THRESHOLD,
        KLANGKD_MEMORY_EVICTION_RECOVERY_PERCENT=PRESSURE_RECOVERY,
        KLANGKD_MEMORY_EVICTION_SUSTAIN_POLLS="1",
        KLANGKD_MEMORY_EVICTION_POLL_INTERVAL="1",
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
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {
        "token": token,
        "headers": {"Authorization": f"Bearer {token}"},
    }


def create_workspace(server, auth, prefix):
    """Create a unique workspace and start it via a short WS connect.

    A container must exist for the evictor to have a victim; connecting
    creates one. The socket is then closed, leaving the workspace
    running with no subscribers — eviction-eligible.
    """
    global _ws_counter  # noqa: PLW0603
    _ws_counter += 1
    client = server["client"]
    resp = client.post(
        "/api/v1/workspaces",
        headers=auth["headers"],
        json={"name": f"{prefix}-{_ws_counter}"},
        timeout=10,
    )
    assert resp.status_code == 200
    workspace_id = resp.json()["id"]
    return workspace_id


async def start_container_via_ws(server, auth, workspace_id):
    """Connect + disconnect: leaves a running, subscriber-less workspace."""
    ws = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
    try:
        await ws.send(
            json.dumps(
                {
                    "cmd": "workspace_connect",
                    "workspaceId": workspace_id,
                }
            )
        )
        # Drain until the container is genuinely up (the fanout helper's
        # pattern — container_status broadcasts may interleave).
        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline:
            try:
                data = json.loads(await asyncio.wait_for(ws.recv(), 5))
            except asyncio.TimeoutError:
                continue
            if data.get("type") == "container_ready":
                break
        else:
            raise AssertionError("container_ready not received within 60s")
    finally:
        await ws.close()


async def recv_until(ws, predicate, timeout=30):
    """Receive until predicate matches; returns all messages seen."""
    deadline = asyncio.get_event_loop().time() + timeout
    messages = []
    while asyncio.get_event_loop().time() < deadline:
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=1)
            data = json.loads(msg)
            messages.append(data)
            if predicate(data):
                return data
        except asyncio.TimeoutError:
            continue
    return None


def is_running(status_resp) -> bool:
    body = status_resp.json()
    return bool(body.get("running"))


class TestMemoryPressureEviction:
    async def test_idle_workspace_evicted_with_event_and_restarts(
        self, server, auth
    ):
        """The full production path: pressured host → graceful eviction.

        A subscriber-less running workspace is stopped by the evictor; a
        connected client of *another* workspace receives the
        ``workspace_evicted`` broadcast; the victim's status shows
        stopped; and a later reconnect restarts the container (state
        preserved — idle-stop semantics).
        """
        # The bystander: connected to its own workspace (never evicted —
        # it has a live subscriber) and receiving the eviction fan-out.
        bystander_id = create_workspace(server, auth, "evict-bystander")
        await start_container_via_ws(server, auth, bystander_id)
        bystander_ws = await _ws_dial(
            server, f"/ws?token={auth['token']}", max_size=2**20
        )
        try:
            await bystander_ws.send(
                json.dumps(
                    {
                        "cmd": "workspace_connect",
                        "workspaceId": bystander_id,
                    }
                )
            )
            # Drain past the bystander's own container_ready.
            await recv_until(
                bystander_ws,
                lambda m: m.get("type") == "container_ready",
                timeout=60,
            )

            # The victim: running, then left with no subscribers.
            victim_id = create_workspace(server, auth, "evict-victim")
            await start_container_via_ws(server, auth, victim_id)

            # Sustain=1 × poll=1s: the eviction lands within a few
            # polls. The bystander must see the distinct event.
            evicted = await recv_until(
                bystander_ws,
                lambda m: (
                    m.get("type") == "workspace_evicted"
                    and m.get("workspace_id") == victim_id
                ),
                timeout=60,
            )
            assert evicted is not None, (
                "workspace_evicted event not received within 60s of "
                "arming pressure"
            )
            assert "memory" in (evicted.get("reason") or "").lower()

            # Status flips to stopped (graceful removal, not a crash).
            deadline = asyncio.get_event_loop().time() + 30
            while asyncio.get_event_loop().time() < deadline:
                resp = server["client"].get(
                    f"/api/v1/workspaces/{victim_id}/status",
                    headers=auth["headers"],
                    timeout=10,
                )
                assert resp.status_code == 200
                if not is_running(resp):
                    break
                await asyncio.sleep(1)
            else:
                raise AssertionError(
                    "victim still reports running 30s after the "
                    "workspace_evicted event"
                )

            # The bystander workspace was never touched: it has a live
            # subscriber, and the evictor must honor that.
            resp = server["client"].get(
                f"/api/v1/workspaces/{bystander_id}/status",
                headers=auth["headers"],
                timeout=10,
            )
            assert resp.status_code == 200
            assert is_running(resp), (
                "workspace with a live subscriber was evicted"
            )

            # Reconnect restarts the container (idle-stop semantics:
            # state preserved, next connect brings it back). The socket
            # stays OPEN for the status check: this server's evictor is
            # permanently armed (poll=1s, sustain=1), so the moment the
            # reconnect socket closes the restarted container is
            # subscriber-less again and legitimately re-evicted — a
            # close-then-poll here races the evictor (CI flake). A live
            # subscriber is eviction-protected (the bystander's own
            # guarantee), so assert with it connected, then close.
            victim_ws = await _ws_dial(
                server, f"/ws?token={auth['token']}", max_size=2**20
            )
            try:
                await victim_ws.send(
                    json.dumps(
                        {
                            "cmd": "workspace_connect",
                            "workspaceId": victim_id,
                        }
                    )
                )
                deadline = asyncio.get_event_loop().time() + 60
                ready = False
                while asyncio.get_event_loop().time() < deadline:
                    try:
                        data = json.loads(
                            await asyncio.wait_for(victim_ws.recv(), 5)
                        )
                    except asyncio.TimeoutError:
                        continue
                    if data.get("type") == "container_ready":
                        ready = True
                        break
                assert ready, "container_ready not received on reconnect"
                resp = server["client"].get(
                    f"/api/v1/workspaces/{victim_id}/status",
                    headers=auth["headers"],
                    timeout=10,
                )
                assert resp.status_code == 200
                assert is_running(resp), "reconnect did not restart container"
            finally:
                await victim_ws.close()
        finally:
            await bystander_ws.close()
            for ws_id in (bystander_id, victim_id):
                try:
                    server["client"].delete(
                        f"/api/v1/workspaces/{ws_id}",
                        headers=auth["headers"],
                        timeout=60,
                    )
                except httpx.ReadTimeout:
                    pass

    async def test_evicted_container_is_gone_from_podman(self, server, auth):
        """The eviction removes the container for real (podman-visible)."""
        import subprocess

        victim_id = create_workspace(server, auth, "evict-podman")
        await start_container_via_ws(server, auth, victim_id)

        # Wait for the eviction to land (poll the status endpoint).
        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline:
            resp = server["client"].get(
                f"/api/v1/workspaces/{victim_id}/status",
                headers=auth["headers"],
                timeout=10,
            )
            if resp.status_code == 200 and not is_running(resp):
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError("workspace was not evicted within 60s")

        # Give the removal a moment to finish, then verify podman has no
        # container carrying this workspace's label.
        await asyncio.sleep(3)
        result = subprocess.run(
            ["podman", "ps", "-a", "--filter", "label=klangk.workspace"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert victim_id not in result.stdout, (
            "evicted workspace still has a podman container"
        )
        try:
            server["client"].delete(
                f"/api/v1/workspaces/{victim_id}",
                headers=auth["headers"],
                timeout=60,
            )
        except httpx.ReadTimeout:
            pass
