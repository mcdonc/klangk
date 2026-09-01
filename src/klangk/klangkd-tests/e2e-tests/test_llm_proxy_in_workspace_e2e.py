"""In-workspace LLM-proxy E2E tests (#2959).

The deepest integration of the LLM proxy: a real klangkd (TCP mode, so
its own caddy runs the browser + egress listeners) with
``KLANGKD_LLM_MODELS`` pointing at a fake OpenAI-compatible upstream,
a real workspace container, and ``curl`` driven from *inside* it — the
exact production path:

    curl in container
      → egress caddy (container-source ACL + workspace-JWT forward_auth)
        → klangkd backend (workspace-token gate, #2959)
          → litellm router → fake upstream

Also proves the gate's negative from the inside: a tokenless request
from within the container is rejected (401 at forward_auth), and a
chat completion echoes the fake upstream's canned content.

Requires: podman available, klangk image built.

Run with: devenv shell -- test-backend-e2e -k test_llm_proxy_in_workspace
"""

import asyncio
import base64
import json

import pytest

from _e2e_server import start_server, stop_server, ws_connect as _ws_dial
from _fake_llm import start_fake_llm, stop_fake_llm


@pytest.fixture(scope="module")
def fake_llm():
    """Start a fake OpenAI-compatible LLM server on a free port."""
    fake = start_fake_llm()
    yield fake
    stop_fake_llm(fake)


@pytest.fixture(scope="module")
def server(fake_llm):
    """Real klangkd with LLM models pointing at the fake upstream.

    TCP mode (uds=False) so klangkd's own caddy runs — the egress
    listener the container's ``KLANGKWS_LLM_PROXY_URL`` points at only
    exists when the proxy is up. ``KLANGKD_CONTAINER_SUBNETS`` is left
    unset so caddy auto-derives the container sources from the host's
    IPv4s (the production behavior: pasta NAT makes container traffic
    appear as the host's own addresses, never loopback).
    """
    model_entry = f"openai/fake-model:{fake_llm['url']}/v1:dummy-key"
    srv = start_server(
        uds=False,
        KLANGKD_JWT_SECRET="llm-ws-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
        KLANGKD_LLM_MODELS=model_entry,
    )
    yield srv
    stop_server(srv)


@pytest.fixture(scope="module")
def auth(server):
    resp = server["client"].post(
        "/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


async def recv_until(ws, predicate, timeout=30):
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


def is_container_ready(msg):
    if msg.get("type") != "event":
        return False
    event = msg.get("event", {})
    return (
        event.get("type") == "CUSTOM"
        and event.get("name") == "container_ready"
    )


async def ws_connect(server, auth, workspace_id):
    """Open a WebSocket, connect to the workspace, wait for the container.

    Mirrors test_per_user_home.py: the first wait is on the direct
    ``container_ready`` WS message (connect ack); after ``ui_ready`` a
    CUSTOM ``container_ready`` event follows (handle auto-create).
    """
    ws = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
    await ws.send(
        json.dumps({"cmd": "workspace_connect", "workspaceId": workspace_id})
    )
    deadline = asyncio.get_event_loop().time() + 90
    while asyncio.get_event_loop().time() < deadline:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if msg.get("type") == "container_ready":
            break
    await ws.send(json.dumps({"cmd": "ui_ready"}))
    while asyncio.get_event_loop().time() < deadline:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if is_container_ready(msg):
            break
    return ws


async def exec_command(ws, command):
    """Run a command via exec and return combined output (base64-decoded)."""
    await ws.send(json.dumps({"cmd": "exec_start", "command": command}))
    msgs = await recv_until(
        ws, lambda m: m.get("type") == "exec_exit", timeout=60
    )
    outputs = [m for m in msgs if m.get("type") == "exec_output"]
    return b"".join(base64.b64decode(m["data"]) for m in outputs).decode()


class TestLLMProxyFromWorkspace:
    @pytest.mark.asyncio
    async def test_llm_proxy_round_trip_from_inside_workspace(
        self, server, auth
    ):
        """One container, three legs through the production path:

        1. models with the workspace token → the fake model is listed;
        2. chat completion with the token → the canned content comes
           back (echoed model name proves verbatim forwarding);
        3. no token → 401 (egress forward_auth rejects it before the
           backend ever sees it).
        """
        client = server["client"]
        resp = client.post(
            "/api/v1/workspaces",
            headers=auth["headers"],
            json={"name": "llm-proxy-in-ws"},
            timeout=10,
        )
        assert resp.status_code == 200, resp.text
        workspace_id = resp.json()["id"]
        try:
            ws = await ws_connect(server, auth, workspace_id)
            try:
                models = await exec_command(
                    ws,
                    [
                        "bash",
                        "-c",
                        'curl -s -H "Authorization: Bearer '
                        '$(klangk-workspace-token)" '
                        '"$KLANGKWS_LLM_PROXY_URL/models"',
                    ],
                )
                assert "fake-model" in models, models

                completion = await exec_command(
                    ws,
                    [
                        "bash",
                        "-c",
                        "curl -s "
                        '-H "Authorization: Bearer '
                        '$(klangk-workspace-token)" '
                        '-H "Content-Type: application/json" '
                        '-d \'{"model": "fake-model", '
                        '"messages": [{"role": "user", '
                        '"content": "hi"}]}\' '
                        '"$KLANGKWS_LLM_PROXY_URL/chat/completions"',
                    ],
                )
                assert "Hello from fake-model!" in completion, completion

                status = await exec_command(
                    ws,
                    [
                        "bash",
                        "-c",
                        'curl -s -o /dev/null -w "%{http_code}" '
                        '"$KLANGKWS_LLM_PROXY_URL/models"',
                    ],
                )
                assert status.strip() == "401", status
            finally:
                await ws.close()
        finally:
            try:
                client.delete(
                    f"/api/v1/workspaces/{workspace_id}",
                    headers=auth["headers"],
                    timeout=30,
                )
            except Exception:
                pass
