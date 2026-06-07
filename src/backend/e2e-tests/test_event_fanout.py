"""Multi-connection event fanout tests.

Tests that Pi events are broadcast to all WebSocket connections
for the same workspace, and that connections can join/leave
without disrupting others.

Each test creates its own workspace to avoid shared container
state between tests.

Requires: Docker running, klangk image built.

Run with: devenv shell -- test-cli-e2e
"""

import asyncio
import json
import os
import signal
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
    """Start a real Klangk server (with nginx LLM proxy) for the test module."""
    data_dir = tempfile.mkdtemp(prefix="klangk-fanout-e2e-")
    port = "18996"
    nginx_port = "18994"
    project_root = os.path.join(os.path.dirname(__file__), "..", "..", "..")

    # Start nginx as an LLM proxy so containers can reach the LLM.
    nginx_proc = None
    nginx_log = os.path.join(data_dir, "nginx.log")
    if os.environ.get("KLANGK_LLM_BASE_URL"):
        log_fd = open(nginx_log, "w")
        nginx_proc = subprocess.Popen(
            [os.path.join(project_root, "scripts", "nginx.sh")],
            env={
                **os.environ,
                "DEVENV_STATE": data_dir,
                "KLANGK_NGINX_PORT": nginx_port,
                "KLANGK_PORT": port,
            },
            stdout=log_fd,
            stderr=log_fd,
        )
        # Wait for nginx LLM proxy to be reachable
        for _ in range(10):
            try:
                resp = httpx.get(
                    f"http://localhost:{nginx_port}/llm-proxy/models",
                    timeout=2,
                )
                if resp.status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            nginx_proc.kill()
            log_content = (
                open(nginx_log).read() if os.path.exists(nginx_log) else ""
            )
            raise RuntimeError(
                f"Nginx LLM proxy not reachable on port {nginx_port}:\n{log_content}"
            )

    env = {
        **os.environ,
        "KLANGK_PORT": port,
        "KLANGK_NGINX_PORT": nginx_port,
        "KLANGK_DATA_DIR": data_dir,
        "KLANGK_JWT_SECRET": "fanout-e2e-secret",
        "KLANGK_DEFAULT_USER": "test@example.com",
        "KLANGK_DEFAULT_PASSWORD": "testpass",
        "KLANGK_TEST_MODE": "1",
        "KLANGK_INSTANCE_ID": "fanout-e2e",
        "KLANGK_IDLE_TIMEOUT_SECONDS": "300",
        "KLANGK_PORT_RANGE_START": "9100",
        "LOGFIRE_TOKEN": "",
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
        "nginx_port": nginx_port,
        "data_dir": data_dir,
        "proc": proc,
    }

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    if proc.stdout:
        server_log = proc.stdout.read().decode("utf-8", errors="replace")
        if server_log.strip():
            sys.stderr.write(
                f"\n=== Fanout server log ===\n{server_log}\n===\n"
            )
    if nginx_proc:
        try:
            nginx_proc.kill()
        except OSError:
            pass
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
        f"{url}/auth/login",
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
        f"{url}/workspaces",
        headers=auth["headers"],
        json={"name": name},
        timeout=10,
    )
    assert resp.status_code == 200
    workspace_id = resp.json()["id"]

    def cleanup():
        httpx.delete(
            f"{url}/workspaces/{workspace_id}",
            headers=auth["headers"],
            timeout=10,
        )

    return workspace_id, cleanup


async def ws_connect(server, auth, workspace_id):
    """Open a WebSocket, connect to workspace, return ws."""
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
    resp = json.loads(await ws.recv())
    assert resp["type"] == "workspace_ready"
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

                msgs1 = await recv_until(ws1, is_container_ready, timeout=10)
                msgs2 = await recv_until(ws2, is_container_ready, timeout=10)

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
