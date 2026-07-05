"""End-to-end test for the service-command service session (#1133).

The service command is a per-workspace singleton that runs exactly once,
in a standalone ``service`` tmux session owned by the AGENT identity
(``service:service-cmd``) -- decoupled from both the owner's interactive
session and the ``pi --mode rpc`` subprocess. A visitor under a different
account must NOT spawn their own copy; instead they see the
``service-cmd`` window as a joinable shared terminal attributed to the
agent.

This reproduces the whole model against a real server + container:

* the owner fires ``terminal_start`` and gets their own interactive
  shell (the ``service-cmd`` window is NOT one of their own tabs -- it
  lives in the ``service`` session);
* the owner nonetheless sees ``service-cmd`` in the shared-terminals
  list (attributed to the agent), so the service is visible;
* a visitor (granted the ``coders`` role) fires ``terminal_start`` and
  gets their own interactive shell -- with NO ``service-cmd`` tab of
  their own (exactly-once: the command is not re-run for them);
* the visitor sees the ``service-cmd`` window in the shared-terminals
  list too.
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


@pytest.fixture(scope="module")
def server():
    """Start a real Klangk server for the test module."""
    data_dir = tempfile.mkdtemp(prefix="klangk-dcmd-shared-e2e-")
    port = "18999"

    env = {
        **os.environ,
        "KLANGK_PORT": port,
        "KLANGK_DATA_DIR": data_dir,
        "KLANGK_JWT_SECRET": "dcmd-shared-e2e-secret",
        "KLANGK_PREVENT_INSECURE_JWT_SECRET": "",
        "KLANGK_DEFAULT_USER": "test@example.com",
        "KLANGK_DEFAULT_PASSWORD": "testpass",
        "KLANGK_TEST_MODE": "1",
        "KLANGK_INSTANCE_ID": "dcmd-shared-e2e",
        "KLANGK_IDLE_TIMEOUT_SECONDS": "0",
        "KLANGK_PORT_RANGE_START": "9500",
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
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        proc.kill()
        stdout = proc.stdout.read().decode() if proc.stdout else ""
        raise RuntimeError(f"Server failed to start:\n{stdout}")

    yield {"url": base_url, "port": port, "data_dir": data_dir, "proc": proc}

    try:
        proc.kill()
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    if proc.stdout:
        server_log = proc.stdout.read().decode("utf-8", errors="replace")
        if server_log.strip():
            sys.stderr.write(
                f"\n=== dcmd-shared server log ===\n{server_log}\n===\n"
            )
    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            "label=klangk.instance=dcmd-shared-e2e",
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


def _login(server, email, password):
    url = server["url"]
    resp = httpx.post(
        f"{url}/api/v1/auth/login",
        json={"email": email, "password": password},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


def _register(server, email, password):
    url = server["url"]
    resp = httpx.post(
        f"{url}/api/v1/auth/register",
        json={"email": email, "password": password},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    return _login(server, email, password)


@pytest.fixture(scope="module")
def owner(server):
    return _login(server, "test@example.com", "testpass")


async def _connect(server, auth, workspace_id):
    """Open a WS, connect, wait for container_ready.

    Returns ``(ws, received, reader_task)`` where *received* is a live
    list a background reader appends every message to -- the async
    ``shared_terminals`` broadcast can land at any moment.
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
    return ws, received, reader_task


async def _terminal_start(ws):
    await ws.send(
        json.dumps({"cmd": "terminal_start", "cols": 80, "rows": 24})
    )


def _own_windows(received):
    """The most recent terminal_windows payload seen."""
    for m in reversed(received):
        if m.get("type") == "terminal_windows":
            return m.get("windows", [])
    return None


def _shared(received):
    """The most recent shared_terminals payload seen."""
    for m in reversed(received):
        if m.get("type") == "shared_terminals":
            return m.get("terminals", [])
    return None


def _has_service_cmd(windows):
    return any(w.get("name") == "service-cmd" for w in (windows or []))


def _shared_has_service_cmd(shared):
    """True if the shared_terminals payload includes service-cmd."""
    return any(t.get("window_name") == "service-cmd" for t in (shared or []))


class TestServiceCommandSharedSingleton:
    @pytest.mark.asyncio
    async def test_service_window_shared_not_own(self, server, owner):
        """The service command runs in the agent's ``service`` session
        (#1133): neither the owner nor a visitor has ``service-cmd`` as
        their own tab, but both see it as a shared (agent-attributed)
        terminal, and it is a singleton (not re-run per user)."""
        url = server["url"]
        visitor = _register(server, "visitor@example.com", "visitorpass")

        resp = httpx.post(
            f"{url}/api/v1/workspaces",
            headers=owner["headers"],
            json={
                "name": "dcmd-shared",
                "service_command": "sleep 600",
                "setup_state": "complete",
            },
            timeout=10,
        )
        assert resp.status_code == 200, resp.text
        workspace_id = resp.json()["id"]

        resp = httpx.post(
            f"{url}/api/v1/workspaces/{workspace_id}/roles/coders",
            headers=owner["headers"],
            json={"email": "visitor@example.com"},
            timeout=10,
        )
        assert resp.status_code == 200, resp.text

        owner_ws, owner_rx, owner_reader = await _connect(
            server, owner, workspace_id
        )
        try:
            # Owner starts the terminal -> service command fires in the
            # ``service`` session. The owner gets their own shell; the
            # ``service-cmd`` window is NOT one of their own tabs.
            await _terminal_start(owner_ws)
            loop = asyncio.get_event_loop()
            # Wait for the owner's own windows, then confirm the service
            # window surfaces as shared (nudge list_shared_terminals).
            deadline = loop.time() + 45
            while loop.time() < deadline:
                if _own_windows(owner_rx) is not None:
                    break
                await asyncio.sleep(0.3)
            await asyncio.sleep(1)  # let the service command fire + settle
            if not _shared_has_service_cmd(_shared(owner_rx)):
                await owner_ws.send(
                    json.dumps({"cmd": "list_shared_terminals"})
                )
                deadline = loop.time() + 20
                while loop.time() < deadline:
                    if _shared_has_service_cmd(_shared(owner_rx)):
                        break
                    await asyncio.sleep(0.3)
            # The service-cmd window is the agent's, not the owner's own.
            assert not _has_service_cmd(_own_windows(owner_rx)), (
                "owner has service-cmd as an OWN tab; it should live in the "
                f"service session. own windows: {_own_windows(owner_rx)}"
            )
            assert _shared_has_service_cmd(_shared(owner_rx)), (
                "owner never saw the service service-cmd as shared; msgs: "
                f"{[m.get('type') for m in owner_rx]}"
            )

            # Visitor connects and starts their own terminal.
            vis_ws, vis_rx, vis_reader = await _connect(
                server, visitor, workspace_id
            )
            try:
                await _terminal_start(vis_ws)
                # Wait for the visitor's terminal_started + windows.
                deadline = loop.time() + 45
                while loop.time() < deadline:
                    if _own_windows(vis_rx) is not None:
                        break
                    await asyncio.sleep(0.3)
                # Give the shared broadcast a moment to land, then
                # nudge an explicit list if needed.
                await asyncio.sleep(1)
                if not _shared_has_service_cmd(_shared(vis_rx)):
                    await vis_ws.send(
                        json.dumps({"cmd": "list_shared_terminals"})
                    )
                    deadline = loop.time() + 20
                    while loop.time() < deadline:
                        if _shared_has_service_cmd(_shared(vis_rx)):
                            break
                        await asyncio.sleep(0.3)

                own = _own_windows(vis_rx)
                # Singleton: the visitor's OWN session has no service-cmd
                # window -- the command was not re-run for them.
                assert own is not None, "visitor never received windows"
                assert not _has_service_cmd(own), (
                    f"visitor spawned their own service-cmd (not a "
                    f"singleton); own windows: {own}"
                )

                shared = _shared(vis_rx)
                # Shared visibility: the service service-cmd is joinable.
                assert shared, (
                    "visitor never received shared_terminals; msgs: "
                    f"{[m.get('type') for m in vis_rx]}"
                )
                assert _shared_has_service_cmd(shared), (
                    f"service service-cmd not in shared list: {shared}"
                )
            finally:
                vis_reader.cancel()
                await vis_ws.close()
        finally:
            owner_reader.cancel()
            await owner_ws.close()
            httpx.delete(
                f"{url}/api/v1/workspaces/{workspace_id}",
                headers=owner["headers"],
                timeout=30,
            )
