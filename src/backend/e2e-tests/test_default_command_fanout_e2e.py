"""End-to-end test for default-command tab fan-out (#1114).

A visitor who opened an auto-started workspace *while setup was still
running* used to miss the ``default-cmd`` tab forever: their snapshot was
taken before the window existed, and its later creation was announced
only to the owning connection.  This test reproduces that race against a
real server + container and asserts the visitor (under a *different*
account) receives the tab once the default command fires.
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
    data_dir = tempfile.mkdtemp(prefix="klangk-dcmd-fanout-e2e-")
    port = "18997"

    env = {
        **os.environ,
        "KLANGK_PORT": port,
        "KLANGK_DATA_DIR": data_dir,
        "KLANGK_JWT_SECRET": "dcmd-fanout-e2e-secret",
        "KLANGK_PREVENT_INSECURE_JWT_SECRET": "",
        "KLANGK_DEFAULT_USER": "test@example.com",
        "KLANGK_DEFAULT_PASSWORD": "testpass",
        "KLANGK_TEST_MODE": "1",
        "KLANGK_INSTANCE_ID": "dcmd-fanout-e2e",
        "KLANGK_IDLE_TIMEOUT_SECONDS": "0",  # don't idle out mid-test
        "KLANGK_PORT_RANGE_START": "9400",
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
                f"\n=== dcmd-fanout server log ===\n{server_log}\n===\n"
            )
    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            "label=klangk.instance=dcmd-fanout-e2e",
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
    """Open a WS, connect to the workspace, wait for container_ready.

    Returns ``(ws, received, reader_task)`` where *received* is a live
    list a background reader appends every message to — the one-shot
    ``terminal_windows`` fan-out can land at any moment and must not be
    missed.
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


def _has_default_cmd(msg):
    return msg.get("type") == "terminal_windows" and any(
        w.get("name") == "default-cmd" for w in msg.get("windows", [])
    )


class TestDefaultCommandFanout:
    @pytest.mark.asyncio
    async def test_visitor_gets_default_cmd_tab_after_creation(
        self, server, owner
    ):
        """A different-account visitor sees the tab once it is created."""
        url = server["url"]
        # Register a collaborator (the visitor) under a different account.
        visitor = _register(server, "visitor@example.com", "visitorpass")

        # Create a workspace with a default command, held in 'pending'
        # setup so the default-cmd window is NOT created yet.
        resp = httpx.post(
            f"{url}/api/v1/workspaces",
            headers=owner["headers"],
            json={
                "name": "dcmd-fanout",
                "default_command": "sleep 600",
                "setup_state": "pending",
            },
            timeout=10,
        )
        assert resp.status_code == 200, resp.text
        workspace_id = resp.json()["id"]

        # Grant the visitor the 'coders' role (has code-in-isolation).
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
            # Visitor connects and starts a terminal while setup is still
            # pending: their snapshot must NOT contain default-cmd yet.
            vis_ws, vis_rx, vis_reader = await _connect(
                server, visitor, workspace_id
            )
            try:
                await _terminal_start(vis_ws)
                # Wait for the visitor's first terminal_windows (no tab yet).
                loop = asyncio.get_event_loop()
                deadline = loop.time() + 30
                while loop.time() < deadline:
                    if any(
                        m.get("type") == "terminal_windows" for m in vis_rx
                    ):
                        break
                    await asyncio.sleep(0.3)
                else:
                    raise AssertionError(
                        "visitor never received initial terminal_windows"
                    )
                # Sanity: no default-cmd tab while setup is pending.
                assert not any(_has_default_cmd(m) for m in vis_rx), vis_rx

                # Setup completes; the owner fires terminal_start, which
                # creates the default-cmd window and fans it out.
                httpx.put(
                    f"{url}/api/v1/workspaces/{workspace_id}",
                    headers=owner["headers"],
                    json={"setup_state": "complete"},
                    timeout=10,
                )
                await _terminal_start(owner_ws)

                # The visitor should now receive a terminal_windows that
                # includes the default-cmd tab -- without a manual refresh.
                deadline = loop.time() + 45
                while loop.time() < deadline:
                    if any(_has_default_cmd(m) for m in vis_rx):
                        break
                    await asyncio.sleep(0.3)
                else:
                    raise AssertionError(
                        "visitor never received the default-cmd tab; "
                        f"saw {len(vis_rx)} messages: "
                        f"{[m.get('type') for m in vis_rx]}"
                    )
                assert any(_has_default_cmd(m) for m in vis_rx)
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
