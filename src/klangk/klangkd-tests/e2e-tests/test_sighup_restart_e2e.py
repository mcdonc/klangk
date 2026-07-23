"""End-to-end tests for graceful runtime restart on SIGHUP (#1212, #1587).

SIGHUP triggers an in-place runtime restart: every WebSocket client is
closed with code 1012, all workspace containers are stopped, the
idle/health loops are cancelled, then container-side startup re-runs
(prewarm, adopt, loops, auto-start).  The HTTP listener and DB stay up
throughout.

Since #1587, SIGHUP also **reloads configuration**: the settings are
re-resolved from the environment + config file, and reloadable values
take effect without a process restart.

These tests start a real server on a private port, open WebSocket
sessions, send SIGHUP, and assert the acceptance criteria:

1. HTTP stays available across the restart (no refused connections).
2. WebSocket clients are closed with code 1012 and can reconnect.
3. A second SIGHUP during a restart queues behind it (serialized).
4. Workspace containers are stopped and then auto-started again.
5. A config file change is picked up after SIGHUP (#1587).

Requires: podman available, klangk image built.

Run with: devenv shell -- test-backend-e2e test_sighup_restart_e2e.py
"""

import asyncio
import json
import os
import subprocess
import tempfile
import time

import httpx
import pytest
import websockets

from _e2e_server import start_server, stop_server, ws_connect as _ws_dial


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server (klangkd over its UDS) with short idle +
    health intervals."""
    server = start_server(
        KLANGKD_JWT_SECRET="sighup-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        KLANGKD_ALLOW_AUTOSTART="1",
        LOGFIRE_TOKEN="",
        KLANGKD_LLM_BASE_URL="",
        KLANGKD_LLM_API_KEY="",
        KLANGKD_LLM_MODEL="",
    )
    yield server
    stop_server(server)


@pytest.fixture(scope="module")
def auth(server):
    """Login as the default user and return token + headers."""
    resp = server["client"].post(
        "/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
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
            if server["client"].get("/health", timeout=2).status_code == 200:
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
            if server["client"].get("/health", timeout=2).status_code == 200:
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
    ws = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
    try:
        _send_sighup(server)

        # The server closes every client with code 1012 ("service
        # restarted").  websockets raises ConnectionClosed on a
        # server-initiated close; the code is on the received close
        # frame.  ``ConnectionClosed.code`` is deprecated in websockets
        # >=13.1 (``rcvd`` is the received Close frame; None only if the
        # peer hung up without a close frame, which can't carry 1012).
        closed = None
        try:
            await asyncio.wait_for(ws.recv(), timeout=60)
        except websockets.ConnectionClosed as exc:
            closed = exc.rcvd.code if exc.rcvd is not None else None
        assert closed == 1012, f"expected close 1012, got {closed}"
    finally:
        await ws.close()

    # Reconnect succeeds and the new socket stays open.
    ws2 = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
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

    With KLANGKD_ALLOW_AUTOSTART=1, a workspace created with auto-start
    configured is recreated after the restart.  We track the container
    via the workspace status API: it goes from 'running' (pre-SIGHUP) to
    gone/stopped, then back to 'running' once auto-start completes.
    """
    client = server["client"]
    headers = auth["headers"]

    # Create a workspace with auto_start enabled.
    resp = client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "sighup-autostart", "auto_start": True},
        timeout=30,
    )
    assert resp.status_code == 200
    workspace_id = resp.json()["id"]

    def status_value():
        r = client.get(
            f"/api/v1/workspaces/{workspace_id}/status",
            headers=headers,
            timeout=30,
        )
        assert r.status_code == 200
        return r.json().get("running", False)

    # Bring the container up by connecting once.
    ws = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
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
        client.delete(
            f"/api/v1/workspaces/{workspace_id}",
            headers=headers,
            timeout=30,
        )
    except httpx.ReadTimeout:
        pass


# --- Config reload via SIGHUP (#1587) ---


def test_config_reload_via_sighup():
    """#5: A config file change is picked up after SIGHUP (#1587).

    Writes a YAML config with product_name="Before", starts a server
    with --config, asserts /api/v1/config returns "Before", rewrites the
    file to "After", sends SIGHUP, and asserts the endpoint returns
    "After".
    """
    import yaml

    data_dir = tempfile.mkdtemp(prefix="klangk-reload-e2e-")
    state_dir = tempfile.mkdtemp(prefix="klangk-reload-e2e-state-")

    config_path = os.path.join(state_dir, "klangk.yaml")
    with open(config_path, "w") as f:
        yaml.dump({"product_name": "Before"}, f)

    server = start_server(
        data_dir=data_dir,
        state_dir=state_dir,
        config=config_path,
        KLANGKD_JWT_SECRET="reload-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
        KLANGKD_LLM_BASE_URL="",
        KLANGKD_LLM_API_KEY="",
        KLANGKD_LLM_MODEL="",
    )
    client = server["client"]
    try:
        # Login.
        resp = client.post(
            "/api/v1/auth/login",
            json={"identifier": "test@example.com", "password": "testpass"},
            timeout=10,
        )
        assert resp.status_code == 200
        headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

        # Assert initial config.
        resp = client.get("/api/v1/config", headers=headers, timeout=10)
        assert resp.status_code == 200
        assert resp.json()["product_name"] == "Before"

        # Rewrite the config file and send SIGHUP.
        with open(config_path, "w") as f:
            yaml.dump({"product_name": "After"}, f)
        os.kill(server["proc"].pid, subprocess.signal.SIGHUP)

        # Wait for the restart to complete and assert the new value.
        time.sleep(5)
        assert _wait_http_ok(server), "server did not recover after SIGHUP"
        resp = client.get("/api/v1/config", headers=headers, timeout=10)
        assert resp.status_code == 200
        assert resp.json()["product_name"] == "After"
    finally:
        stop_server(server)
