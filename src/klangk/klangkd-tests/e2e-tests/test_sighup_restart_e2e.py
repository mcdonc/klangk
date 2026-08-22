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
    """#2: SIGHUP closes WS clients with code 1012; they can reconnect.

    The graceful restart first sends ``host_restart`` events with a
    ``phase`` field ("draining" at refuse-starts, "restarting" just
    before the recycle); those arrive as ordinary frames before the
    1012 close, so receive until the close and assert both (#2527)."""
    ws = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
    try:
        _send_sighup(server)

        # The server broadcasts the restart phases, then closes every
        # client with code 1012 ("service restarted").  websockets
        # raises ConnectionClosed on a server-initiated close; the code
        # is on the received close frame.  ``ConnectionClosed.code`` is
        # deprecated in websockets >=13.1 (``rcvd`` is the received
        # Close frame; None only if the peer hung up without a close
        # frame, which can't carry 1012).
        closed = None
        phases = []
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if msg.get("type") == "host_restart":
                    phases.append(msg.get("phase"))
        except websockets.ConnectionClosed as exc:
            closed = exc.rcvd.code if exc.rcvd is not None else None
        assert closed == 1012, f"expected close 1012, got {closed}"
        assert "draining" in phases, f"missing draining phase, got {phases}"
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
    # 120s, not the #2619-era 30s: this create synchronously awaits the
    # eager container start (the #2616 class — #2619 raised it to 120s in
    # test_api_e2e.py's autostart class but missed this file; the same
    # ReadTimeout resurfaced here under triple-e2e-suite load, #2633 CI).
    resp = client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "sighup-autostart", "auto_start": True},
        timeout=120,
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


async def _connect_until_ready(server, auth, workspace_id, timeout=90):
    """workspace_connect until ``container_ready``; returns the open socket.

    Retries through the graceful-restart window: while the node refuses
    starts the connect gets an error frame (the socket stays open), so a
    single attempt is not conclusive — keep dialing until the container
    genuinely comes up or the deadline passes (#2527 restart-race e2e).
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        ws = await _ws_dial(
            server, f"/ws?token={auth['token']}", max_size=2**20
        )
        ready = False
        try:
            await ws.send(
                json.dumps(
                    {
                        "cmd": "workspace_connect",
                        "workspaceId": workspace_id,
                    }
                )
            )
            inner = time.monotonic() + 15
            while time.monotonic() < inner:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break  # nothing more coming on this attempt
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                last = msg
                if msg.get("type") == "container_ready":
                    ready = True
                    break
        except websockets.ConnectionClosed:
            pass  # dropped mid-attempt (e.g. residual 1012); retry
        finally:
            if not ready:
                await ws.close()
        if ready:
            return ws
        await asyncio.sleep(1)
    raise AssertionError(
        f"container_ready not received within {timeout}s (last={last!r})"
    )


async def test_sighup_during_workspace_restart(server, auth):
    """A settings-page restart racing SIGHUP lands safely in every
    interleaving (#2527):

    - HUP before the restart's up-front refusal check → 503, workspace
      untouched (then drained by the HUP itself).
    - HUP mid-restart → the old container is already stopped and the
      fresh start is refused at the choke point → 503, workspace left
      stopped — never half-restarted.
    - Restart completing during the HUP → its own POST is an in-flight
      request the quiesce phase waits for; the fresh container is then
      stopped by the drain (tracked, or via the racing-start sweep).

    In every case: the restart endpoint answers 200 or 503 (anything
    else is a bug), the HUP completes with the server healthy, the
    status endpoint stays coherent, and a fresh start works afterwards.
    """
    client = server["client"]
    headers = auth["headers"]

    resp = client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "sighup-restart-race"},
        timeout=120,
    )
    assert resp.status_code == 200
    workspace_id = resp.json()["id"]

    # A running container first — a restart needs something to restart.
    ws = await _connect_until_ready(server, auth, workspace_id)
    await ws.close()
    await asyncio.sleep(1)  # let the disconnect register

    # Fire the restart, then the HUP a beat later: the restart is in
    # its stop/create phase when the drain window opens under it.
    restart_task = asyncio.ensure_future(
        asyncio.to_thread(
            client.post,
            f"/api/v1/workspaces/{workspace_id}/restart",
            headers=headers,
            timeout=120,
        )
    )
    await asyncio.sleep(0.2)
    _send_sighup(server)
    restart_resp = await restart_task

    assert restart_resp.status_code in (200, 503), restart_resp.text
    assert _wait_http_ok(server, timeout=90)

    # Status endpoint is coherent after the dust settles.
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        r = client.get(
            f"/api/v1/workspaces/{workspace_id}/status",
            headers=headers,
            timeout=10,
        )
        assert r.status_code == 200
        # Drained (the common outcome) → stop polling. Still running is
        # also legitimate (the restart won the post-HUP race); just
        # verify it keeps reporting 200 and move on.
        if not r.json().get("running", False):
            break
        await asyncio.sleep(1)

    # A fresh start works once the restart cycle has finished.
    ws2 = await _connect_until_ready(server, auth, workspace_id, timeout=120)
    await ws2.close()

    try:
        client.delete(
            f"/api/v1/workspaces/{workspace_id}",
            headers=headers,
            timeout=60,
        )
    except httpx.ReadTimeout:
        pass


async def test_sigterm_graceful_shutdown(server, auth):
    """TERM/INT shutdown is graceful (#2527): the client receives a
    ``host_shutdown`` event and a terminal stop for its workspace before
    the process exits — not a bare socket drop.

    Owns its server (it terminates it); the module-scoped `server`
    fixture is untouched.
    """
    own = start_server(
        KLANGKD_JWT_SECRET="term-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
    )
    try:
        client = own["client"]
        resp = client.post(
            "/api/v1/auth/login",
            json={"identifier": "test@example.com", "password": "testpass"},
            timeout=10,
        )
        assert resp.status_code == 200
        token = resp.json()["access_token"]

        resp = client.post(
            "/api/v1/workspaces",
            headers={"Authorization": f"Bearer {token}"},
            json={"name": "term-shutdown"},
            timeout=120,
        )
        assert resp.status_code == 200
        ws_id = resp.json()["id"]

        ws = await _ws_dial(own, f"/ws?token={token}", max_size=2**20)
        try:
            await ws.send(
                json.dumps({"cmd": "workspace_connect", "workspaceId": ws_id})
            )
            # Wait for the container to be genuinely up.
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                msg = json.loads(raw)
                if msg.get("type") == "container_ready":
                    break

            # SIGTERM — the graceful shutdown hook must fire before the
            # process exits: host_shutdown broadcast first.
            os.kill(own["proc"].pid, subprocess.signal.SIGTERM)

            saw_host_shutdown = False
            stopped_or_closed = False
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    msg = json.loads(raw)
                    if msg.get("type") == "host_shutdown":
                        saw_host_shutdown = True
                    ev = msg.get("event") or {}
                    if ev.get(
                        "name"
                    ) == "container_stopped" and "shutdown" in (
                        ev.get("value") or {}
                    ).get("reason", ""):
                        stopped_or_closed = True
            except websockets.ConnectionClosed:
                stopped_or_closed = True  # the process exited
            assert saw_host_shutdown, "host_shutdown event not received"
            assert stopped_or_closed
        finally:
            await ws.close()
        # The process exits on its own after the drain.
        own["proc"].wait(timeout=60)
    finally:
        stop_server(own)


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
