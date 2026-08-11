"""E2E: consent-decide TUI against real klangkd + sidecar (#2327, #2336).

Drives the real ConsentDeciderApp in-process via Pilot against a real klangkd
(with a workspace in egress_mode=interactive + a network sidecar). Two
concurrent connections to non-allowed hosts must both appear as held requests
in the TUI concurrently (#2336).

Run with: devenv shell -- test-backend-e2e -k TestConsentDecideTuiE2E
"""

import asyncio
import json
import time

import pytest

from _e2e_server import start_server, stop_server, ws_connect as _ws_dial

from klangk.cli.tui.consent import ConsentDeciderApp


@pytest.fixture(scope="module")
def server():
    server = start_server(
        uds=False,  # TCP: the ConsentDeciderApp uses the CLI WS transport
        KLANGKD_JWT_SECRET="consent-tui-e2e-secret",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_ALLOW_AUTOSTART="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        KLANGKD_EGRESS_CONSENT_TIMEOUT="30",
        LOGFIRE_TOKEN="",
    )
    yield server
    stop_server(server)


@pytest.fixture
def auth(server):
    resp = server["client"].post(
        "/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    return {"token": token, "headers": {"Authorization": f"Bearer {token}"}}


@pytest.fixture
def workspace(server, auth):
    resp = server["client"].post(
        "/api/v1/workspaces",
        headers=auth["headers"],
        json={
            "name": f"consent-tui-e2e-{int(time.time())}",
            "allowed_domains": ["allowed.local"],
            "egress_mode": "interactive",
            "auto_start": True,
        },
        timeout=60,
    )
    assert resp.status_code == 200, resp.text
    ws_id = resp.json()["id"]
    yield ws_id
    try:
        server["client"].delete(
            f"/api/v1/workspaces/{ws_id}",
            headers=auth["headers"],
            timeout=30,
        )
    except Exception:
        pass


async def _wait_container_ready(server, auth, ws_id):
    ws = await _ws_dial(server, f"/ws?token={auth['token']}", max_size=2**20)
    await ws.send(
        json.dumps({"cmd": "workspace_connect", "workspaceId": ws_id})
    )
    deadline = asyncio.get_event_loop().time() + 90
    while asyncio.get_event_loop().time() < deadline:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=90))
        if msg.get("type") == "container_ready":
            return ws
    await ws.close()
    raise AssertionError("container_ready not received within 90s")


class TestConsentDecideTuiE2E:
    """#2336: two concurrent connections must both appear in the consent-decide
    TUI while both are still pending (not serialized behind the first)."""

    @pytest.mark.asyncio
    async def test_two_concurrent_flows_both_appear(
        self, server, auth, workspace
    ):
        ws_id = workspace
        ws_conn = await _wait_container_ready(server, auth, ws_id)
        try:
            app = ConsentDeciderApp(
                server_url=server["url"],
                token=auth["token"],
                workspace_id=ws_id,
                workspace_name="consent-tui-e2e",
                hold_timeout=30.0,
            )
            async with app.run_test() as pilot:
                # Wait for the app's WS to connect to klangkd's decider stream.
                for _ in range(60):
                    if app._connected:
                        break
                    await pilot.pause()
                    await asyncio.sleep(0.1)
                assert app._connected, (
                    "consent-decide TUI did not connect to klangkd"
                )

                # Trigger two concurrent connections to non-allowed hosts.
                # Real hosts resolve via the sidecar's DNS proxy; the SYNs hit
                # the consent gate (not in allowed_domains) -> held -> consent
                # request fanned out to this decider. Command is an argv LIST
                # (a string gets iterated char-by-char -> crun "p" not found).
                await ws_conn.send(
                    json.dumps(
                        {
                            "cmd": "exec_start",
                            "command": [
                                "python3",
                                "-c",
                                "import socket,threading,time;"
                                "[threading.Thread("
                                "target=lambda h=h:socket.create_connection"
                                "((h,80),timeout=8),daemon=True).start()"
                                " for h in ('example.com','ford.com')];"
                                "time.sleep(10)",
                            ],
                        }
                    )
                )

                # Wait for both consent requests to arrive in the TUI.
                deadline = time.time() + 30
                while time.time() < deadline:
                    await pilot.pause()
                    await asyncio.sleep(0.2)
                    if len(app.controller.pending) >= 2:
                        break
                # Both must be pending concurrently (neither decided).
                import subprocess

                ps = subprocess.run(
                    ["podman", "ps", "--format", "{{.Names}} {{.Image}}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                ).stdout
                assert len(app.controller.pending) >= 2, (
                    f"expected >=2 concurrent consent requests in the TUI, "
                    f"got {len(app.controller.pending)}: "
                    f"{list(app.controller.pending.keys())}\n"
                    f"podman ps:\n{ps}"
                )
        finally:
            await ws_conn.close()
