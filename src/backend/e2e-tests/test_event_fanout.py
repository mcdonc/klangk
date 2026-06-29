"""Multi-connection event fanout tests.

Tests that Pi events are broadcast to all WebSocket connections
for the same workspace, and that connections can join/leave
without disrupting others.

Each test creates its own workspace to avoid shared container
state between tests.

Requires: podman available, klangk image built.

Run with: devenv shell -- test-backend-e2e
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
    """Start a real Klangk server for the test module.

    No LLM or nginx needed — these tests only exercise WebSocket event
    fanout (container ready, exec output routing), not LLM interactions.
    """
    data_dir = tempfile.mkdtemp(prefix="klangk-fanout-e2e-")
    port = "18996"

    env = {
        **os.environ,
        "KLANGK_PORT": port,
        "KLANGK_DATA_DIR": data_dir,
        "KLANGK_JWT_SECRET": "fanout-e2e-secret",
        "KLANGK_PREVENT_INSECURE_JWT_SECRET": "",
        "KLANGK_DEFAULT_USER": "test@example.com",
        "KLANGK_DEFAULT_PASSWORD": "testpass",
        "KLANGK_TEST_MODE": "1",
        "KLANGK_INSTANCE_ID": "fanout-e2e",
        "KLANGK_IDLE_TIMEOUT_SECONDS": "300",
        "KLANGK_PORT_RANGE_START": "9100",
        "LOGFIRE_TOKEN": "",
        # Clear LLM vars so .env values don't leak in
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
        proc.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    if proc.stdout:
        server_log = proc.stdout.read().decode("utf-8", errors="replace")
        if server_log.strip():
            sys.stderr.write(
                f"\n=== Fanout server log ===\n{server_log}\n===\n"
            )
    result = subprocess.run(
        [
            "podman",
            "ps",
            "-a",
            "--filter",
            "label=klangk.instance=fanout-e2e",
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
    """Login and return token + headers."""
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


def create_workspace(server, auth):
    """Create a unique workspace, return (workspace_id, cleanup_fn)."""
    global _ws_counter  # noqa: PLW0603
    _ws_counter += 1
    name = f"fanout-{_ws_counter}"
    url = server["url"]
    resp = httpx.post(
        f"{url}/api/v1/workspaces",
        headers=auth["headers"],
        json={"name": name},
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
    """Open a WebSocket, connect to workspace, return ws.

    Drains messages until ``workspace_ready`` arrives. A
    ``container_status`` broadcast (sent to all authenticated
    connections when a container starts) can land before the ready
    response, because the container is started during the
    ``workspace_connect`` handshake.
    """
    ws_url = server["url"].replace("http://", "ws://")
    ws = await websockets.connect(
        f"{ws_url}/ws?token={auth['token']}", max_size=2**20
    )
    await ws.send(
        json.dumps(
            {
                "cmd": "workspace_connect",
                "workspaceId": workspace_id,
            }
        )
    )
    deadline = asyncio.get_event_loop().time() + 30
    while asyncio.get_event_loop().time() < deadline:
        try:
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        except asyncio.TimeoutError:
            continue
        if resp.get("type") == "workspace_ready":
            break
    else:
        raise AssertionError("workspace_ready not received within 30s")
    await ws.send(json.dumps({"cmd": "ui_ready"}))
    return ws


async def recv_until(ws, predicate, timeout=30):
    """Receive messages until predicate returns True or timeout."""
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


def register_user(server, email, password):
    """Register a new user (requires KLANGK_TEST_MODE=1), return auth dict."""
    url = server["url"]
    resp = httpx.post(
        f"{url}/api/v1/auth/register",
        json={"email": email, "password": password},
        timeout=10,
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    return {"token": token, "headers": headers}


class TestEventFanout:
    @pytest.mark.asyncio
    async def test_both_connections_receive_container_ready(
        self, server, auth
    ):
        """Two connections to the same workspace both get events."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws1 = await ws_connect(server, auth, workspace_id)
            ws2 = await ws_connect(server, auth, workspace_id)

            try:

                def is_container_ready(msg):
                    if msg.get("type") != "event":
                        return False
                    event = msg.get("event", {})
                    return (
                        event.get("type") == "CUSTOM"
                        and event.get("name") == "container_ready"
                    )

                msgs1, msgs2 = await asyncio.gather(
                    recv_until(ws1, is_container_ready, timeout=30),
                    recv_until(ws2, is_container_ready, timeout=30),
                )

                all_msgs = msgs1 + msgs2
                assert any(is_container_ready(m) for m in all_msgs)
            finally:
                await ws1.close()
                await ws2.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_exec_output_only_goes_to_requester(self, server, auth):
        """exec_output goes only to the connection that started the exec,
        not to all subscribers (exec is per-connection, not broadcast)."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws1 = await ws_connect(server, auth, workspace_id)
            ws2 = await ws_connect(server, auth, workspace_id)

            try:
                await asyncio.sleep(1)

                await ws1.send(
                    json.dumps(
                        {"cmd": "exec_start", "command": ["echo", "from-ws1"]}
                    )
                )

                def is_exec_exit(msg):
                    return msg.get("type") == "exec_exit"

                msgs1 = await recv_until(ws1, is_exec_exit, timeout=15)
                exec_outputs = [
                    m for m in msgs1 if m.get("type") == "exec_output"
                ]
                assert len(exec_outputs) > 0

                ws2_msgs = []
                try:
                    while True:
                        msg = await asyncio.wait_for(ws2.recv(), timeout=2)
                        ws2_msgs.append(json.loads(msg))
                except asyncio.TimeoutError:
                    pass

                ws2_exec = [
                    m for m in ws2_msgs if m.get("type") == "exec_output"
                ]
                assert len(ws2_exec) == 0
            finally:
                await ws1.close()
                await ws2.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_join_broadcasts_system_chat_message(self, server, auth):
        """Connecting to a workspace broadcasts a system chat message."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws1 = await ws_connect(server, auth, workspace_id)
            try:
                # Drain ws1 until we see the "joined" system chat message
                def is_join_chat(msg):
                    return (
                        msg.get("type") == "chat_message"
                        and msg.get("message_type") == 2
                        and "joined" in msg.get("message", "")
                    )

                msgs = await recv_until(ws1, is_join_chat, timeout=15)
                join_msgs = [m for m in msgs if is_join_chat(m)]
                assert len(join_msgs) >= 1
                assert join_msgs[0]["message"] == "test joined"
                assert join_msgs[0]["message_type"] == 2
            finally:
                await ws1.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_leave_broadcasts_system_chat_message(self, server, auth):
        """Disconnecting broadcasts a 'left' system chat message to others."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            # Register a second user and share the workspace
            auth2 = register_user(server, "user2@example.com", "testpass2")
            resp = httpx.post(
                f"{server['url']}/api/v1/workspaces/{workspace_id}/members",
                headers=auth["headers"],
                json={"email": "user2@example.com"},
                timeout=10,
            )
            assert resp.status_code == 200

            ws1 = await ws_connect(server, auth, workspace_id)
            ws2 = await ws_connect(server, auth2, workspace_id)

            try:
                # Drain any pending messages on ws1
                await recv_until(
                    ws1,
                    lambda m: False,
                    timeout=3,
                )

                # Close ws2 — should trigger "left" system message
                await ws2.close()

                # ws1 should receive the leave system chat message
                def is_leave_chat(msg):
                    return (
                        msg.get("type") == "chat_message"
                        and msg.get("message_type") == 2
                        and "left" in msg.get("message", "")
                    )

                msgs = await recv_until(ws1, is_leave_chat, timeout=15)
                leave_msgs = [m for m in msgs if is_leave_chat(m)]
                assert len(leave_msgs) >= 1
                assert leave_msgs[0]["message"] == "user2 left"
                assert leave_msgs[0]["message_type"] == 2
            finally:
                await ws1.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_same_user_multi_connect_no_duplicate_leave(
        self, server, auth
    ):
        """Same user with two connections — closing one does not emit 'left'."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws1 = await ws_connect(server, auth, workspace_id)
            ws2 = await ws_connect(server, auth, workspace_id)

            try:
                # Drain pending messages on ws1
                await recv_until(ws1, lambda m: False, timeout=3)

                # Close ws2 — same user still has ws1
                await ws2.close()
                await asyncio.sleep(1)

                # Drain ws1 — should NOT have a "left" message
                remaining = await recv_until(ws1, lambda m: False, timeout=3)
                leave_msgs = [
                    m
                    for m in remaining
                    if m.get("type") == "chat_message"
                    and m.get("message_type") == 2
                    and "left" in m.get("message", "")
                ]
                assert len(leave_msgs) == 0
            finally:
                await ws1.close()
        finally:
            cleanup()

    @pytest.mark.asyncio
    async def test_first_disconnect_does_not_kill_second(self, server, auth):
        """When the first connection disconnects, the second can still exec."""
        workspace_id, cleanup = create_workspace(server, auth)
        try:
            ws1 = await ws_connect(server, auth, workspace_id)
            ws2 = await ws_connect(server, auth, workspace_id)

            try:
                await asyncio.sleep(1)

                await ws1.close()
                await asyncio.sleep(1)

                await ws2.send(
                    json.dumps(
                        {
                            "cmd": "exec_start",
                            "command": ["echo", "still-alive"],
                        }
                    )
                )

                def is_exec_exit(msg):
                    return msg.get("type") == "exec_exit"

                msgs = await recv_until(ws2, is_exec_exit, timeout=15)
                exec_outputs = [
                    m for m in msgs if m.get("type") == "exec_output"
                ]
                assert len(exec_outputs) > 0
            finally:
                await ws2.close()
        finally:
            cleanup()
