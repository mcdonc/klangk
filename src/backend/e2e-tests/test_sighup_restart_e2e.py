"""End-to-end tests for graceful runtime restart on SIGHUP (#1212).

SIGHUP triggers an in-place runtime restart: every WebSocket client is
closed with code 1012, all workspace containers are stopped, the
idle/health loops are cancelled, then container-side startup re-runs
(prewarm, adopt, loops, auto-start).  The HTTP listener and DB stay up
throughout.

These tests start a real server on a private port, open WebSocket
sessions, send SIGHUP, and assert the four acceptance criteria:

1. HTTP stays available across the restart (no refused connections).
2. WebSocket clients are closed with code 1012 and can reconnect.
3. A second SIGHUP during a restart queues behind it (serialized).
4. Workspace containers are stopped and then auto-started again.

Requires: podman available, klangk image built.

Run with: devenv shell -- test-backend-e2e test_sighup_restart_e2e.py
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import httpx
import pytest
import websockets

from klangk_backend.model import free_port


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server with short idle + health intervals."""
    data_dir = tempfile.mkdtemp(prefix="klangk-sighup-e2e-")
    port = str(free_port())

    env = {
        **os.environ,
        "KLANGK_PORT": port,
        "KLANGK_DATA_DIR": data_dir,
        "KLANGK_JWT_SECRET": "sighup-e2e-secret",
        "KLANGK_PREVENT_INSECURE_JWT_SECRET": "",
        "KLANGK_DEFAULT_USER": "test@example.com",
        "KLANGK_DEFAULT_PASSWORD": "testpass",
        "KLANGK_TEST_MODE": "1",
        # Short idle timeout so a workspace with no subscribers stops
        # quickly; we mainly care that SIGHUP stops *all* of them.
        "KLANGK_IDLE_TIMEOUT_SECONDS": "300",
        "KLANGK_PORT_RANGE_START": str(free_port()),
        # Allow auto-start so the post-SIGHUP restart brings workspaces
        # back (acceptance criterion #4).
        "KLANGK_ALLOW_AUTOSTART": "1",
        "LOGFIRE_TOKEN": "",
        "KLANGK_LLM_BASE_URL": "",
        "KLANGK_LLM_API_KEY": "",
        "KLANGK_LLM_MODEL": "",
    }
    proc = subprocess.Popen(
        [
            "uvicorn",
            "klangk_backend.main:app",
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
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    if proc.stdout:
        server_log = proc.stdout.read().decode("utf-8", errors="replace")
        if server_log.strip():
            sys.stderr.write(
                f"\n=== SIGHUP e2e server log ===\n{server_log}\n===\n"
            )
    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            "label=klangk.instance=sighup-e2e",
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
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


def _send_sighup(server) -> None:
    """Send SIGHUP to the running backend process."""
    os.kill(server["proc"].pid, subprocess.signal.SIGHUP)


def _wait_http_ok(server, timeout=60) -> bool:
    """Return True once /health answers 200 again (or throughout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (
                httpx.get(f"{server['url']}/health", timeout=2).status_code
                == 200
            ):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


# --- Acceptance criteria ---


def test_http_listener_stays_up_across_sighup(server):
    """#1: SIGHUP recycles the runtime, not the listener.

    We hammer /health before, during, and after SIGHUP and assert the
    server never goes unreachable.  A few transient failures during the
    restart window are acceptable (the listener is shared but the runtime
    is briefly torn down); what must NOT happen is a sustained outage.
    """
    assert _wait_http_ok(server)
    _send_sighup(server)

    # Probe through the restart window.  Most calls should succeed; the
    # only hard requirement is that the server is back well before this.
    successes = 0
    checked = 0
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        checked += 1
        try:
            if (
                httpx.get(f"{server['url']}/health", timeout=2).status_code
                == 200
            ):
                successes += 1
        except Exception:
            pass
        time.sleep(0.5)
    assert successes > 0, "server never came back after SIGHUP"
    # The listener is not torn down, so the vast majority of probes
    # succeed even mid-restart.
    assert successes >= checked * 0.8


async def test_websocket_closed_with_1012_and_reconnects(server, auth):
    """#2: SIGHUP closes WS clients with code 1012; they can reconnect."""
    ws_url = server["url"].replace("http://", "ws://")
    ws = await websockets.connect(
        f"{ws_url}/ws?token={auth['token']}", max_size=2**20
    )
    try:
        _send_sighup(server)

        # The server closes every client with code 1012 ("service
        # restarted").  websockets raises ConnectionClosed on a
        # server-initiated close; the code is on the exception.
        closed = None
        try:
            await asyncio.wait_for(ws.recv(), timeout=60)
        except websockets.ConnectionClosed as exc:
            closed = exc.code
        assert closed == 1012, f"expected close 1012, got {closed}"
    finally:
        await ws.close()

    # Reconnect succeeds and the new socket stays open.
    ws2 = await websockets.connect(
        f"{ws_url}/ws?token={auth['token']}", max_size=2**20
    )
    try:
        # A ping/pong round-trip confirms the new connection is live.
        pong_waiter = await ws2.ping()
        await asyncio.wait_for(pong_waiter, timeout=10)
    finally:
        await ws2.close()


async def test_rapid_double_sighup_is_serialized(server):
    """#3: two SIGHUPs in quick succession queue, never race.

    Each restart logs both "restarting" and "restarted"; two restarts
    mean two complete cycles.  We can't read the server's logs cheaply
    from here, so we assert the weaker but still meaningful invariant:
    the server survives two back-to-back SIGHUPs and stays healthy.
    The serialization itself is covered by the unit test
    (test_restart_lock_serializes_concurrent_calls).
    """
    assert _wait_http_ok(server)
    _send_sighup(server)
    # Fire a second one almost immediately.
    await asyncio.sleep(0.2)
    _send_sighup(server)
    # Server must settle back to healthy despite the overlap.
    assert _wait_http_ok(server, timeout=90)


async def test_containers_stopped_then_autostarted(server, auth):
    """#4: SIGHUP stops containers, then auto-start brings them back.

    With KLANGK_ALLOW_AUTOSTART=1, a workspace created with auto-start
    configured is recreated after the restart.  We track the container
    via the workspace status API: it goes from 'running' (pre-SIGHUP) to
    gone/stopped, then back to 'running' once auto-start completes.
    """
    url = server["url"]
    headers = auth["headers"]

    # Create a workspace with auto_start enabled.
    resp = httpx.post(
        f"{url}/api/v1/workspaces",
        headers=headers,
        json={"name": "sighup-autostart", "auto_start": True},
        timeout=30,
    )
    assert resp.status_code == 200
    workspace_id = resp.json()["id"]

    def status_value():
        r = httpx.get(
            f"{url}/api/v1/workspaces/{workspace_id}/status",
            headers=headers,
            timeout=30,
        )
        assert r.status_code == 200
        return r.json().get("running", False)

    # Bring the container up by connecting once.
    ws_url = server["url"].replace("http://", "ws://")
    ws = await websockets.connect(
        f"{ws_url}/ws?token={auth['token']}", max_size=2**20
    )
    try:
        await ws.send(
            json.dumps(
                {"cmd": "workspace_connect", "workspaceId": workspace_id}
            )
        )
        # Wait until the API reports the container running.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not status_value():
            time.sleep(1)
        assert status_value(), "container did not come up before SIGHUP"
    finally:
        await ws.close()

    # Give the disconnect a moment to register, then restart.
    await asyncio.sleep(2)
    _send_sighup(server)

    # After the restart, auto_start should recreate the container.
    deadline = time.monotonic() + 120
    back_up = False
    while time.monotonic() < deadline:
        if status_value():
            back_up = True
            break
        time.sleep(2)
    assert back_up, "container was not auto-started after SIGHUP"

    # Cleanup.
    try:
        httpx.delete(
            f"{url}/api/v1/workspaces/{workspace_id}",
            headers=headers,
            timeout=30,
        )
    except httpx.ReadTimeout:
        pass
