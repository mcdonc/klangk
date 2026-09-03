"""End-to-end tests for scheduled server stop/recycle (#2661).

Exercises the REAL fire path in a real process — scheduler loop →
self-SIGTERM for stop (launcher graceful-shutdown hook → drain → exit 0)
and scheduler → in-process recycle for recycle — plus the client-visible
frames (server_schedule countdown snapshot, server_schedule_fired) and
past-due-at-boot firing.

Owns its servers (stops/recycles them); shares nothing with the
module-scoped fixtures of other suites.

Run with: devenv shell -- test-backend-e2e test_server_schedule_e2e.py
"""

import asyncio
import json
import time

import pytest

from _e2e_server import (
    start_server,
    stop_server,
    ws_connect as _ws_dial,
)
from klangk.model import free_port


def _own_server(tag: str):
    return start_server(
        KLANGKD_JWT_SECRET=f"sched-e2e-secret-{tag}",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
        # The UDS path leaves KLANGKD_EGRESS_PORT at its default (8995),
        # which collides with a dev klangkd on the same host — draw a
        # fresh one so the suite runs against a live dev server too.
        KLANGKD_EGRESS_PORT=str(free_port()),
    )


def _login(server) -> str:
    resp = server["client"].post(
        "/api/v1/auth/login",
        json={"identifier": "test@example.com", "password": "testpass"},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


async def _open_ws(server, token):
    ws = await _ws_dial(server, f"/ws?token={token}", max_size=2**20)
    return ws


@pytest.mark.asyncio
async def test_scheduled_stop_fires_graceful_exit():
    """A stop scheduled in the near future: the connected client sees the
    countdown snapshot, then fired + host_shutdown before the process
    exits (code 0) — the real signal path, not the mocked one."""
    own = _own_server("stop")
    try:
        token = _login(own)
        async with await _open_ws(own, token) as ws:
            resp = own["client"].post(
                "/api/v1/server/schedule",
                headers={"Authorization": f"Bearer {token}"},
                json={"action": "stop", "in_seconds": 3},
                timeout=10,
            )
            assert resp.status_code == 200, resp.text
            schedule_id = resp.json()["id"]

            saw_snapshot = False
            saw_fired = False
            saw_host_shutdown = False
            try:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    if mtype == "server_schedule":
                        scheds = msg.get("schedules") or []
                        if any(s["id"] == schedule_id for s in scheds):
                            saw_snapshot = True
                    elif mtype == "server_schedule_fired":
                        assert msg.get("action") == "stop"
                        saw_fired = True
                    elif mtype == "host_shutdown":
                        saw_host_shutdown = True
            except Exception:
                pass  # process exit closes the socket — that's the point

            assert saw_snapshot, "countdown snapshot not received"
            assert saw_fired, "server_schedule_fired not received"
            assert saw_host_shutdown, "host_shutdown not received"

        # The process must exit on its own: after the graceful work,
        # the launcher re-raises the captured SIGTERM (uvicorn
        # capture_signals semantics), so the status is "terminated by
        # SIGTERM" (-15 / 143) — like a normal `systemctl stop`.
        rc = own["proc"].wait(timeout=30)
        assert rc == -15, f"expected SIGTERM status -15, got {rc}"
    finally:
        stop_server(own)


@pytest.mark.asyncio
async def test_scheduled_recycle_stays_up():
    """A recycle scheduled in the near future fires in-process: the
    client sees fired + server_recycle phases + host_started, gets
    closed with 1012, and the SAME process keeps serving."""
    own = _own_server("recycle")
    try:
        token = _login(own)
        async with await _open_ws(own, token) as ws:
            resp = own["client"].post(
                "/api/v1/server/schedule",
                headers={"Authorization": f"Bearer {token}"},
                json={"action": "recycle", "in_seconds": 3},
                timeout=10,
            )
            assert resp.status_code == 200, resp.text

            saw_fired = False
            saw_recycle_phase = False
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    msg = json.loads(raw)
                    mtype = msg.get("type")
                    if mtype == "server_schedule_fired":
                        assert msg.get("action") == "recycle"
                        saw_fired = True
                    elif mtype == "server_recycle":
                        saw_recycle_phase = True
                    elif mtype == "host_started":
                        break
            except Exception:
                # Expected: the 1012 close mid-recycle. host_started may
                # arrive only after reconnecting; the HTTP-alive check
                # below covers the outcome.
                pass

            assert saw_fired, "server_schedule_fired not received"
            assert saw_recycle_phase, "server_recycle phases not received"

        # The SAME process must still be alive and serving HTTP — a
        # recycle never exits.
        assert own["proc"].poll() is None, "recycle exited the process!"
        resp = own["client"].get("/health", timeout=30)
        assert resp.status_code == 200
    finally:
        stop_server(own)


@pytest.mark.asyncio
async def test_past_due_stop_fires_on_boot():
    """A schedule whose fire time passed while klangkd was down fires
    ~immediately on the next boot (deliberate semantics — documented in
    server-scheduling.md)."""
    own = _own_server("pastdue")
    token = _login(own)
    # Create a schedule 1s out, then stop the server before it fires:
    # stop it by killing the process hard so the row survives unfired.
    resp = own["client"].post(
        "/api/v1/server/schedule",
        headers={"Authorization": f"Bearer {token}"},
        json={"action": "stop", "in_seconds": 1},
        timeout=10,
    )
    assert resp.status_code == 200, resp.text
    # Hard-kill BEFORE it fires, and do NOT stop_server() — that would
    # rmtree the data/state dirs, and the schedule row must survive for
    # the reboot below.
    own["proc"].kill()
    own["proc"].wait(timeout=10)

    await asyncio.sleep(2)  # let the fire time pass

    # Boot a second server on the same state dir: the past-due schedule
    # must fire (SIGTERM to self) and take it down again (SIGTERM
    # status).
    # wait_ready=False: this server is EXPECTED to fire the past-due
    # stop within its first scheduler tick and exit during "startup" —
    # the readiness poll would race it and report a spurious failure.
    second = start_server(
        KLANGKD_JWT_SECRET="sched-e2e-secret-pastdue",
        KLANGKD_PREVENT_INSECURE_JWT_SECRET="",
        KLANGKD_DEFAULT_USER="test@example.com",
        KLANGKD_DEFAULT_PASSWORD="testpass",
        KLANGKD_TEST_MODE="1",
        KLANGKD_IDLE_TIMEOUT_SECONDS="300",
        LOGFIRE_TOKEN="",
        KLANGKD_EGRESS_PORT=str(free_port()),
        state_dir=own["state_dir"],
        data_dir=own["data_dir"],
        wait_ready=False,
    )
    try:
        rc = second["proc"].wait(timeout=30)
        assert rc == -15, (
            f"past-due stop should end with SIGTERM status -15, got {rc}"
        )
    finally:
        # One teardown covers both servers (same dirs).
        try:
            own["client"].close()
        except Exception:
            pass
        stop_server(second)
