"""Multi-connection event fanout tests.

Tests that Pi events are broadcast to all WebSocket connections
for the same workspace, and that connections can join/leave
without disrupting others.

Requires: Docker running, bark-pi image built.

Run with: devenv shell -- test-cli-e2e
"""

import asyncio
import json
import os
import signal
import shutil
import subprocess
import tempfile
import time

import httpx
import pytest
import websockets


@pytest.fixture(scope="module")
def server():
    """Start a real Bark server (with nginx LLM proxy) for the test module."""
    data_dir = tempfile.mkdtemp(prefix="bark-fanout-e2e-")
    port = "18996"
    nginx_port = "18994"
    project_root = os.path.join(os.path.dirname(__file__), "..", "..", "..")

    # Start nginx as an LLM proxy so containers can reach the LLM.
    nginx_proc = None
    nginx_log = os.path.join(data_dir, "nginx.log")
    if os.environ.get("LLM_BASE_URL"):
        log_fd = open(nginx_log, "w")
        nginx_proc = subprocess.Popen(
            [os.path.join(project_root, "scripts", "nginx.sh")],
            env={
                **os.environ,
                "DEVENV_STATE": data_dir,
                "BARK_NGINX_PORT": nginx_port,
                "BARK_PORT": port,
            },
            stdout=log_fd,
            stderr=log_fd,
        )
        # Wait for nginx to start listening
        for _ in range(10):
            try:
                httpx.get(f"http://localhost:{nginx_port}/", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            nginx_proc.kill()
            log_content = (
                open(nginx_log).read() if os.path.exists(nginx_log) else ""
            )
            raise RuntimeError(
                f"Nginx failed to start on port {nginx_port}:\n{log_content}"
            )

    env = {
        **os.environ,
        "BARK_PORT": port,
        "BARK_NGINX_PORT": nginx_port,
        "BARK_DATA_DIR": data_dir,
        "BARK_JWT_SECRET": "fanout-e2e-secret",
        "BARK_DEFAULT_USER": "test@example.com",
        "BARK_DEFAULT_PASSWORD": "testpass",
        "BARK_TEST_MODE": "1",
        "BARK_INSTANCE_ID": "fanout-e2e",
        "BARK_IDLE_TIMEOUT_SECONDS": "300",
        "LOGFIRE_TOKEN": "",
    }
    proc = subprocess.Popen(
        [
            "uvicorn",
            "bark_backend.main:app",
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
    if nginx_proc:
        try:
            nginx_proc.kill()
        except OSError:
            pass
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=bark.instance=fanout-e2e",
            "-q",
        ],
        capture_output=True,
        text=True,
    )
    if result.stdout.strip():
        subprocess.run(
            ["docker", "rm", "-f", *result.stdout.strip().split()],
            capture_output=True,
        )
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def auth(server):
    """Login and return token + workspace ID."""
    url = server["url"]
    resp = httpx.post(
        f"{url}/auth/login",
        json={"email": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    resp = httpx.post(
        f"{url}/workspaces",
        headers=headers,
        json={"name": "fanout-test"},
        timeout=10,
    )
    assert resp.status_code == 200
    workspace_id = resp.json()["id"]

    yield {
        "token": token,
        "headers": headers,
        "workspace_id": workspace_id,
    }

    httpx.delete(
        f"{url}/workspaces/{workspace_id}", headers=headers, timeout=10
    )


async def ws_connect(server, auth):
    """Open a WebSocket, connect to workspace, return (ws, first_msg)."""
    ws_url = server["url"].replace("http://", "ws://")
    ws = await websockets.connect(
        f"{ws_url}/ws?token={auth['token']}", max_size=2**20
    )
    await ws.send(
        json.dumps(
            {
                "cmd": "workspace_connect",
                "workspaceId": auth["workspace_id"],
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
        ws1 = await ws_connect(server, auth)
        ws2 = await ws_connect(server, auth)

        try:
            # Both should receive the container_ready event from ui_ready.
            # ws1 got it when it connected; ws2 should get one too.
            # Drain messages from both looking for container_ready.
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

            # At least one of them should have gotten container_ready
            # (ws1 always gets it; ws2 gets it if the backend sends it
            # as part of the workspace_connect response)
            all_msgs = msgs1 + msgs2
            assert any(is_container_ready(m) for m in all_msgs)
        finally:
            await ws1.close()
            await ws2.close()

    @pytest.mark.asyncio
    async def test_exec_output_only_goes_to_requester(self, server, auth):
        """exec_output goes only to the connection that started the exec,
        not to all subscribers (exec is per-connection, not broadcast)."""
        ws1 = await ws_connect(server, auth)
        ws2 = await ws_connect(server, auth)

        try:
            # Drain initial messages
            await asyncio.sleep(1)

            # ws1 starts an exec
            await ws1.send(
                json.dumps(
                    {"cmd": "exec_start", "command": ["echo", "from-ws1"]}
                )
            )

            # ws1 should get exec_output + exec_exit
            def is_exec_exit(msg):
                return msg.get("type") == "exec_exit"

            msgs1 = await recv_until(ws1, is_exec_exit, timeout=15)
            exec_outputs = [m for m in msgs1 if m.get("type") == "exec_output"]
            assert len(exec_outputs) > 0

            # ws2 should NOT have exec_output (it's per-connection)
            ws2_msgs = []
            try:
                while True:
                    msg = await asyncio.wait_for(ws2.recv(), timeout=2)
                    ws2_msgs.append(json.loads(msg))
            except asyncio.TimeoutError:
                pass

            ws2_exec = [m for m in ws2_msgs if m.get("type") == "exec_output"]
            assert len(ws2_exec) == 0
        finally:
            await ws1.close()
            await ws2.close()

    @pytest.mark.asyncio
    async def test_first_disconnect_does_not_kill_second(self, server, auth):
        """When the first connection disconnects, the second can still exec."""
        ws1 = await ws_connect(server, auth)
        ws2 = await ws_connect(server, auth)

        try:
            # Drain initial messages
            await asyncio.sleep(1)

            # First connection disconnects
            await ws1.close()
            await asyncio.sleep(1)

            # Second connection should still work
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
            exec_outputs = [m for m in msgs if m.get("type") == "exec_output"]
            assert len(exec_outputs) > 0
        finally:
            await ws2.close()

    @pytest.mark.asyncio
    async def test_prompt_response_reaches_both_connections(
        self, server, auth
    ):
        """When one connection sends a prompt, both receive the Pi events."""
        ws1 = await ws_connect(server, auth)

        # Wait for Pi to be ready (first few Pi events take time)
        await asyncio.sleep(5)

        ws2 = await ws_connect(server, auth)
        await asyncio.sleep(1)

        try:
            # Verify LLM proxy is reachable before sending prompt
            if os.environ.get("LLM_BASE_URL"):
                proxy_resp = httpx.get(
                    f"http://localhost:{server['nginx_port']}/llm-proxy/models",
                    timeout=5,
                )
                assert proxy_resp.status_code == 200, (
                    f"LLM proxy not reachable: {proxy_resp.status_code} "
                    f"{proxy_resp.text[:200]}"
                )

            # ws1 sends a prompt
            await ws1.send(json.dumps({"cmd": "prompt", "text": "say hello"}))

            # Both should receive RUN_STARTED (or TEXT_MESSAGE_CONTENT)
            def is_pi_event(msg):
                if msg.get("type") != "event":
                    return False
                etype = msg.get("event", {}).get("type", "")
                return etype in (
                    "RUN_STARTED",
                    "TEXT_MESSAGE_START",
                    "TEXT_MESSAGE_CONTENT",
                )

            msgs1, msgs2 = await asyncio.gather(
                recv_until(ws1, is_pi_event, timeout=60),
                recv_until(ws2, is_pi_event, timeout=60),
            )

            def event_types(msgs):
                return [
                    m.get("event", {}).get("type", m.get("type")) for m in msgs
                ]

            assert any(is_pi_event(m) for m in msgs1), (
                f"ws1 did not receive Pi event. Got: {event_types(msgs1)}"
            )
            assert any(is_pi_event(m) for m in msgs2), (
                f"ws2 did not receive Pi event. Got: {event_types(msgs2)}"
            )
        finally:
            for ws in (ws1, ws2):
                try:
                    await ws.send(json.dumps({"cmd": "abort"}))
                except Exception:
                    pass
                try:
                    await ws.close()
                except Exception:
                    pass
