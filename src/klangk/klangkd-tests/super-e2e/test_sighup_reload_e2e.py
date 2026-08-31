"""SIGHUP runtime recycle + config reload against the appliance (#2561).

Docker containers have no service manager — supervisord is PID 1, so
SIGHUP targets klangkd's own PID inside the appliance (found via the
control channel), exactly what an operator's ``kill -HUP`` does on a
bare-metal install.

Asserts the shipped behaviors (#1212, #1587): the HTTP listener stays
up, WS clients are closed with 1012 and can reconnect, a second SIGHUP
is survived, and a config-file change is picked up by the reload.
"""

import asyncio
import json
import time

import websockets

from _ws import dial


# The baked runtime config (src/containers/host/Dockerfile) — rewritten
# by the reload test with a product_name added. Env vars still override
# it (the test knobs ride in via docker run -e), so rewriting the file
# only changes what the yaml governs.
_BASE_YAML = "\n".join(
    [
        'port: "8997"',
        'listen: "0.0.0.0"',
        'egress_port: "8995"',
        'data_dir: "/home/klangk/data"',
        'customize_dir: "/home/klangk/custom"',
        'image_name: "klangk-workspace"',
        'version_file: "/home/klangk/version.json"',
        'state_dir: "/tmp/klangk-state"',
    ]
)


def _sighup(appliance):
    pids = appliance.service_pids("klangk.main")
    assert pids, "no klangkd process to signal"
    appliance.exec("kill", "-HUP", pids[0])


def _wait_healthy(appliance, api, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if api.get("/health", timeout=5).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


async def _wait_settled(appliance, api, auth, timeout=180):
    """Block until the recycle fully finished: /health up AND a fresh WS
    survives a ping. The appliance is shared across the whole session —
    a still-draining or queued restart must not bleed into the next
    module's connections (observed: a queued second-SIGHUP restart
    closing the next test's WS with 1012).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _wait_healthy(appliance, api, timeout=10):
            await asyncio.sleep(1)
            continue
        try:
            ws = await dial(appliance, auth["token"])
            try:
                pong = await ws.ping()
                await asyncio.wait_for(pong, timeout=10)
                return
            finally:
                await ws.close()
        except Exception:
            await asyncio.sleep(1)
    raise AssertionError("appliance never settled after SIGHUP")


async def test_sighup_closes_ws_with_1012_and_recovers(appliance, api, auth):
    ws = await dial(appliance, auth["token"])
    try:
        _sighup(appliance)
        # The server broadcasts the recycle phases, then closes with 1012.
        closed = None
        phases = []
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=90)
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if msg.get("type") == "server_recycle":
                    phases.append(msg.get("phase"))
        except websockets.ConnectionClosed as exc:
            closed = exc.rcvd.code if exc.rcvd is not None else None
        assert closed == 1012, f"expected close 1012, got {closed}"
        assert "draining" in phases, f"missing draining phase, got {phases}"
    finally:
        await ws.close()

    # HTTP survives the recycle and a fresh WS connects.
    await _wait_settled(appliance, api, auth)


async def test_double_sighup_is_survived(appliance, api, auth):
    assert _wait_healthy(appliance, api)
    _sighup(appliance)
    # Settle between the two: the second HUP must queue behind a real
    # restart (the serialization under test), not leave a queued restart
    # that fires after the test ends — this appliance is shared by the
    # whole session, and a late queued recycle closes the next module's
    # sockets with 1012.
    await _wait_settled(appliance, api, auth)
    _sighup(appliance)
    await _wait_settled(appliance, api, auth, timeout=240)


async def test_config_reload_via_sighup(appliance, api, auth):
    """A klangkd.yaml edit applies after SIGHUP without a container restart."""
    assert _wait_healthy(appliance, api)
    headers = auth["headers"]
    resp = api.get("/api/v1/config", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["product_name"] != "Super After"

    # Rewrite the runtime config with a product_name (as the klangk user,
    # who owns the file), then SIGHUP klangkd's PID.
    appliance.exec(
        "sh",
        "-c",
        f"printf '%s\\nproduct_name: \"Super After\"\\n' "
        f"'{_BASE_YAML}' > /home/klangk/etc/klangkd.yaml",
    )
    _sighup(appliance)
    await _wait_settled(appliance, api, auth, timeout=240)

    deadline = time.monotonic() + 60
    product = None
    while time.monotonic() < deadline:
        resp = api.get("/api/v1/config", headers=headers)
        product = resp.json().get("product_name")
        if product == "Super After":
            break
        time.sleep(1)
    assert product == "Super After", (
        f"config not reloaded after SIGHUP; product_name={product!r}"
    )

    # Restore the shipped yaml so later reruns against the same image
    # start from the baked state.
    appliance.exec(
        "sh",
        "-c",
        f"printf '%s\\n' '{_BASE_YAML}' > /home/klangk/etc/klangkd.yaml",
    )
