"""End-to-end test for ``klangk monitor`` health-event detection (#1174).

The monitor command (added in #1015) previously had only stub-based unit
tests (``src/klangk/klangkc-tests/tests/test_cli.py`` exercises ``monitor_connection`` /
``monitor_run`` against a fake WebSocket).  This module launches the real
``klangk monitor`` as a subprocess against a live server driving real
health-check transitions, and asserts it receives both the unhealthy and
healthy ``service_health`` events as a workspace's check flips down and back
up.

Design note — why a file-flip instead of container stop/start:  when a
container dies the server emits ``container_status{running:false}`` and then
*silence* (never ``service_health{healthy:false}``), because the health loop
only polls ``registry.states`` and a dead container's state is removed.  So
container death cannot produce the unhealthy->healthy ``service_health`` pair
this test exists to verify.  Instead the container is kept alive (a holder
WebSocket keeps it from idling out) and a sentinel *file* inside it drives
the check result: ``test -f /tmp/klangk-unhealthy && exit 1 || exit 0``.
Creating the file flips the check unhealthy; removing it flips it healthy.
See #1175 (item #2) for the stream-contract gap that forces this shape.

Requires: podman available, klangk image built.

Run with: devenv shell -- test-cli-e2e test_monitor_e2e.py
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

import sys

sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__), "..", "..", "klangkd-tests", "e2e-tests"
    ),
)
from _e2e_env import clean_env
from _e2e_server import start_server, stop_server


# --- server / auth / cli-config fixtures (self-contained, per repo convention) ---


def _start_server(data_dir, health_interval="2"):
    """Start a real klangkd (the proxy on a TCP port) with a fast health-check
    poll interval. Returns (server_handle, base_url).

    The CLI drives the real ``klangk`` binary against ``base_url``; the proxy
    proxies to klangkd's UDS (#1525). Server output is streamed to a file
    (PIPE's 64 KB OS buffer can deadlock the event loop on chatty servers —
    #364).
    """
    log_path = os.path.join(data_dir, "server.log")
    server = start_server(
        uds=False,
        data_dir=data_dir,
        KLANGK_JWT_SECRET="monitor-e2e-secret",
        KLANGK_PREVENT_INSECURE_JWT_SECRET="",
        KLANGK_DEFAULT_USER="test@example.com",
        KLANGK_DEFAULT_PASSWORD="testpass",
        KLANGK_TEST_MODE="1",
        KLANGK_IDLE_TIMEOUT_SECONDS="300",
        KLANGK_HEALTH_CHECK_INTERVAL=health_interval,
        LOGFIRE_TOKEN="",
        KLANGK_LLM_BASE_URL="",
        KLANGK_LLM_API_KEY="",
        KLANGK_LLM_MODEL="",
        log_path=log_path,
    )
    return server, server["url"]


def _stop_server(server, data_dir=None):
    """Stop a server started by ``_start_server``."""
    stop_server(server)


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server with a fast health-check interval."""
    data_dir = tempfile.mkdtemp(prefix="klangk-monitor-e2e-")
    server, base_url = _start_server(data_dir)
    yield {"url": base_url, "data_dir": data_dir}
    _stop_server(server)


@pytest.fixture(scope="module")
def auth(server):
    """Login as the default user and return token + headers."""
    url = server["url"]
    resp = httpx.post(
        f"{url}/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture(scope="module")
def cli_env(server, tmp_path_factory):
    """An isolated HOME with ``klangk`` logged into the test server.

    The monitor reads its server URL + token from the CLI config under
    ``~/.config/klangk/klangk.yaml``, so a separate HOME (with a fresh login)
    is all the subprocess needs to connect.
    """
    config_dir = tmp_path_factory.mktemp("klangk-monitor-cli")
    env = clean_env(HOME=str(config_dir))
    result = subprocess.run(
        [
            "klangk",
            "login",
            server["url"],
            "test@example.com",
            "--password-file",
            "-",
        ],
        input="testpass\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"klangk login failed: {result.stdout=}\n{result.stderr=}"
    )
    return env


# --- helpers ---


def _create_workspace(server, auth):
    """Create a workspace with a flippable health check.

    Returns its id.  The check is unhealthy iff a sentinel file exists
    inside the container; absent => healthy (exit 0).
    """
    resp = httpx.post(
        f"{server['url']}/api/v1/workspaces",
        headers=auth["headers"],
        json={"name": "monitor-flip", "health_check": HEALTH_CHECK},
        timeout=10,
    )
    assert resp.status_code == 200
    return resp.json()["id"]


HEALTH_CHECK = "test -f /tmp/klangk-unhealthy && exit 1 || exit 0"


async def _holder_ws(server, auth, workspace_id):
    """Open a WS + workspace_connect to keep the container alive.

    The subscriber holds the container so the health loop has a running
    container to poll.  Returns (ws, reader_task).  Mirrors ``ws_connect``
    in ``test_health_check_e2e.py``: one background reader appends every
    frame to a shared list, so the one-shot ``container_ready`` frame is
    never lost to a race with our polling.
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
            return ws, reader_task
        await asyncio.sleep(0.2)
    reader_task.cancel()
    await ws.close()
    raise AssertionError("container_ready not received within 60s")


def _wait_for_health(server, auth, workspace_id, predicate, timeout=45):
    """Poll the status endpoint until predicate(health_status) is true."""
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
            if predicate(last.get("health")):
                return last
        time.sleep(0.5)
    raise AssertionError(
        f"health never satisfied predicate within {timeout}s; last={last!r}"
    )


def _container_id(server, auth, workspace_id):
    """Fetch the running container's id (for ``podman exec`` flips)."""
    resp = httpx.get(
        f"{server['url']}/api/v1/workspaces/{workspace_id}/status",
        headers=auth["headers"],
        timeout=10,
    )
    assert resp.status_code == 200
    cid = resp.json().get("container_id")
    assert cid, f"no container_id for workspace {workspace_id}"
    return cid


def _exec_flip(container_id, create):
    """Create or remove the sentinel file inside the container.

    Runs as user ``klangk`` (the same user the health check runs as), so
    the check can ``test -f`` the sentinel without permission surprises.
    """
    action = "touch" if create else "rm -f"
    result = subprocess.run(
        [
            "podman",
            "exec",
            "--user",
            "klangk",
            container_id,
            "bash",
            "-c",
            f"{action} /tmp/klangk-unhealthy",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, (
        f"podman exec {action} failed: {result.stdout=}\n{result.stderr=}"
    )


# --- the test ---


class TestMonitorDetectsHealthFlips:
    @pytest.mark.asyncio
    async def test_monitor_sees_unhealthy_then_healthy(
        self, server, auth, cli_env
    ):
        """``klangk monitor`` prints the down->up service_health pair (#1174)."""
        workspace_id = _create_workspace(server, auth)
        try:
            # Hold the container alive so the health loop polls it.
            ws, reader_task = await _holder_ws(server, auth, workspace_id)
            try:
                # Settle to healthy BEFORE launching the monitor, so the
                # monitor connects to a steady-state-healthy container and
                # only sees the transitions we drive.  (service_health fires
                # on transition only — #1173/#1175 item #1 — so a
                # steady-state-healthy workspace emits nothing on connect.)
                _wait_for_health(
                    server, auth, workspace_id, lambda h: h == "healthy"
                )
                cid = _container_id(server, auth, workspace_id)

                # Launch the monitor as a subprocess, streaming only
                # service_health events for this workspace.  stderr is
                # discarded (the reconnect/status banners go there) so its
                # OS buffer can't deadlock the process.
                proc = await asyncio.create_subprocess_exec(
                    "klangk",
                    "monitor",
                    "--type",
                    "service_health",
                    "--workspace",
                    workspace_id,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=cli_env,
                )
                lines: list[str] = []

                async def _drain_stdout():
                    try:
                        while True:
                            raw = await proc.stdout.readline()
                            if not raw:
                                break
                            line = raw.decode(errors="replace").strip()
                            if line:
                                lines.append(line)
                    except Exception:
                        pass

                drain_task = asyncio.create_task(_drain_stdout())
                try:
                    # Drive the check DOWN: create the sentinel file.
                    _exec_flip(cid, create=True)
                    down_idx = await _wait_for_line(
                        lines,
                        lambda m: (
                            m.get("type") == "service_health"
                            and m.get("workspace_id") == workspace_id
                            and m.get("healthy") is False
                        ),
                        timeout=45,
                    )

                    # Drive the check UP: remove the sentinel file.
                    _exec_flip(cid, create=False)
                    await _wait_for_line(
                        lines,
                        lambda m: (
                            m.get("type") == "service_health"
                            and m.get("workspace_id") == workspace_id
                            and m.get("healthy") is True
                        ),
                        after=down_idx,
                        timeout=45,
                    )
                finally:
                    # Tear down the monitor.  It reconnects forever by
                    # default, so it won't exit on its own.
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                    drain_task.cancel()
                    try:
                        await drain_task
                    except (asyncio.CancelledError, Exception):
                        pass
            finally:
                reader_task.cancel()
                try:
                    await reader_task
                except (asyncio.CancelledError, Exception):
                    pass
                try:
                    await ws.close()
                except Exception:
                    pass
        finally:
            try:
                httpx.delete(
                    f"{server['url']}/api/v1/workspaces/{workspace_id}",
                    headers=auth["headers"],
                    timeout=30,
                )
            except httpx.ReadTimeout:
                pass  # container teardown can be slow


async def _wait_for_line(lines, predicate, *, after=-1, timeout=45):
    """Poll *lines* until one after index *after* matches *predicate*.

    Returns the index of the matching line.  Parses each line as JSON for
    the predicate; non-JSON lines are skipped.  The monitor emits
    line-delimited JSON, so one line == one event.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for i in range(after + 1, len(lines)):
            try:
                msg = json.loads(lines[i])
            except json.JSONDecodeError:
                continue
            if predicate(msg):
                return i
        await asyncio.sleep(0.5)
    parsed = []
    for ln in lines:
        try:
            parsed.append(json.loads(ln).get("type"))
        except json.JSONDecodeError:
            parsed.append(ln[:40])
    raise AssertionError(
        f"no matching monitor line within {timeout}s; "
        f"saw {len(lines)} lines: {parsed}"
    )
