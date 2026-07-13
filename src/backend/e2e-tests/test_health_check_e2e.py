"""End-to-end tests for workspace health-check failure surfacing (#1088).

A failing health check used to discard its stdout/stderr, leaving an
"unhealthy" workspace a black box.  These tests start a real server
with a short poll interval, configure a workspace with a check that
writes a distinctive marker to stderr and exits non-zero, and assert
that the marker surfaces both via the status API and the live
``service_health`` WebSocket event.

Requires: podman available, klangk image built.

Run with: devenv shell -- test-backend-e2e test_health_check_e2e.py
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import httpx
import pytest
import websockets

from klangk_backend.model import free_port
from _e2e_env import clean_env


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server with a fast health-check poll interval."""
    data_dir = tempfile.mkdtemp(prefix="klangk-health-e2e-")
    state_dir = tempfile.mkdtemp(prefix="klangk-health-e2e-state-")
    port = str(free_port())

    env = clean_env(
        KLANGK_PORT=port,
        KLANGK_DATA_DIR=data_dir,
        KLANGK_STATE_DIR=state_dir,
        KLANGK_JWT_SECRET="health-e2e-secret",
        KLANGK_PREVENT_INSECURE_JWT_SECRET="",
        KLANGK_DEFAULT_USER="test@example.com",
        KLANGK_DEFAULT_PASSWORD="testpass",
        KLANGK_TEST_MODE="1",
        KLANGK_IDLE_TIMEOUT_SECONDS="300",
        KLANGK_PORT_RANGE_START=str(free_port()),
        KLANGK_HEALTH_CHECK_INTERVAL="2",
        LOGFIRE_TOKEN="",
        KLANGK_LLM_BASE_URL="",
        KLANGK_LLM_API_KEY="",
        KLANGK_LLM_MODEL="",
    )
    proc = subprocess.Popen(
        [
            "python3",
            os.path.join(os.path.dirname(__file__), "runtestserver.py"),
            "--host",
            "0.0.0.0",
            "--port",
            port,
        ],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://localhost:{port}"
    for _ in range(60):
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        proc.kill()
        stdout = proc.stdout.read().decode() if proc.stdout else ""
        raise RuntimeError(f"Server failed to start:\n{stdout}")

    yield {
        "url": base_url,
        "port": port,
        "data_dir": data_dir,
        "proc": proc,
    }

    try:
        proc.kill()
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    if proc.stdout:
        server_log = proc.stdout.read().decode("utf-8", errors="replace")
        if server_log.strip():
            sys.stderr.write(
                f"\n=== Health e2e server log ===\n{server_log}\n===\n"
            )
    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            "label=klangk.instance=health-e2e",
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
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def auth(server):
    """Login as the default user and return token + headers."""
    url = server["url"]
    resp = httpx.post(
        f"{url}/api/v1/auth/login",
        json={"email": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return {"token": token, "headers": headers}


_ws_counter = 0


def create_workspace(server, auth, *, health_check):
    """Create a workspace with a health check, return (id, cleanup_fn)."""
    global _ws_counter  # noqa: PLW0603
    _ws_counter += 1
    name = f"health-{_ws_counter}"
    url = server["url"]
    resp = httpx.post(
        f"{url}/api/v1/workspaces",
        headers=auth["headers"],
        json={"name": name, "health_check": health_check},
        timeout=10,
    )
    assert resp.status_code == 200
    workspace_id = resp.json()["id"]

    def cleanup():
        try:
            httpx.delete(
                f"{url}/api/v1/workspaces/{workspace_id}",
                headers=auth["headers"],
                timeout=30,
            )
        except httpx.ReadTimeout:
            pass  # container cleanup may take a while

    return workspace_id, cleanup


async def ws_connect(server, auth, workspace_id):
    """Open a WS, connect to the workspace, wait for container_ready.

    Keeping the socket open holds the container alive (idle timeout) so
    the health monitor has a running container to poll.  Returns
    ``(ws, received, reader_task)`` where *received* is a live list that
    a background reader appends **every** message to from the moment of
    connect -- so the one-shot ``service_health`` transition event
    (which can fire while we're still draining ``container_ready``) is
    never lost.
    """
    ws_url = server["url"].replace("http://", "ws://")
    ws = await websockets.connect(
        f"{ws_url}/ws?token={auth['token']}", max_size=2**20
    )
    await ws.send(
        json.dumps({"cmd": "workspace_connect", "workspaceId": workspace_id})
    )
    received: list[dict] = []

    async def _reader():
        try:
            async for raw in ws:
                try:
                    received.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
        except websockets.ConnectionClosed:
            pass

    reader_task = asyncio.create_task(_reader())
    loop = asyncio.get_event_loop()
    deadline = loop.time() + 60
    while loop.time() < deadline:
        if any(m.get("type") == "container_ready" for m in received):
            break
        await asyncio.sleep(0.2)
    else:
        reader_task.cancel()
        await ws.close()
        raise AssertionError("container_ready not received within 60s")
    await ws.send(json.dumps({"cmd": "ui_ready"}))
    return ws, received, reader_task


async def wait_for_received(received, predicate, timeout=45):
    """Poll the shared *received* buffer until predicate matches."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for msg in received:
            if predicate(msg):
                return True
        await asyncio.sleep(0.5)
    return False


def _wait_for_status(server, auth, workspace_id, predicate, timeout=45):
    """Poll the status endpoint until predicate(state) is true."""
    url = server["url"]
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        resp = httpx.get(
            f"{url}/api/v1/workspaces/{workspace_id}/status",
            headers=auth["headers"],
            timeout=10,
        )
        if resp.status_code == 200:
            last = resp.json()
            if predicate(last):
                return last
        time.sleep(1)
    raise AssertionError(
        f"status never satisfied predicate within {timeout}s; last={last!r}"
    )


class TestHealthCheckFailureSurfacing:
    @pytest.mark.asyncio
    async def test_status_api_surfaces_failure_reason(self, server, auth):
        """A failing check's stderr shows up in the status API (#1088)."""
        marker = f"klangk-health-failure-{uuid.uuid4().hex[:8]}"
        # Writes a distinctive line to stderr, then exits non-zero.
        health_check = f"echo {marker} 1>&2; exit 3"
        workspace_id, cleanup = create_workspace(
            server, auth, health_check=health_check
        )
        try:
            ws, _received, reader_task = await ws_connect(
                server, auth, workspace_id
            )
            try:
                state = _wait_for_status(
                    server,
                    auth,
                    workspace_id,
                    lambda s: (
                        s.get("health") == "unhealthy"
                        and marker in (s.get("health_message") or "")
                    ),
                )
            finally:
                reader_task.cancel()
                await ws.close()
        finally:
            cleanup()

        assert state["health"] == "unhealthy"
        # The exit code is reported alongside the captured output.
        assert "exited 3" in state["health_message"]
        assert marker in state["health_message"]

    @pytest.mark.asyncio
    async def test_service_health_event_carries_reason(self, server, auth):
        """The live service_health WS event carries the failure reason."""
        marker = f"klangk-health-failure-{uuid.uuid4().hex[:8]}"
        health_check = f"echo {marker} 1>&2; exit 7"
        workspace_id, cleanup = create_workspace(
            server, auth, health_check=health_check
        )
        try:
            ws, received, reader_task = await ws_connect(
                server, auth, workspace_id
            )
            try:

                def is_unhealthy(msg):
                    return (
                        msg.get("type") == "service_health"
                        and msg.get("workspace_id") == workspace_id
                        and msg.get("healthy") is False
                    )

                found = await wait_for_received(
                    received, is_unhealthy, timeout=45
                )
            finally:
                reader_task.cancel()
                await ws.close()
        finally:
            cleanup()

        assert found, (
            f"no unhealthy service_health event received; "
            f"saw {len(received)} messages: "
            f"{[m.get('type') for m in received]}"
        )
        events = [m for m in received if is_unhealthy(m)]
        # The reason rides along on the broadcast (#1088).
        assert marker in (events[0].get("health_message") or "")
        assert "exited 7" in events[0]["health_message"]
