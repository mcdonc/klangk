"""WebSocket helpers for the super-E2E suite (#2561).

Thin black-box client helpers speaking the same public WS protocol the
shipped frontend uses: ``/ws?token=...`` through the appliance's
published port (caddy → UDS → klangkd — the real deployed data path).
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import websockets


async def dial(appliance, token: str, path: str = "/ws"):
    """Open an authenticated WS to the appliance (any endpoint path)."""
    url = f"{appliance.url.replace('http://', 'ws://')}{path}?token={token}"
    return await websockets.connect(url, max_size=2**20, open_timeout=30)


def _is_container_ready(msg: dict) -> bool:
    if msg.get("type") == "container_ready":
        return True
    event = msg.get("event", {})
    return (
        event.get("type") == "CUSTOM"
        and event.get("name") == "container_ready"
    )


async def recv_until(ws, predicate, timeout: float = 60.0) -> list[dict]:
    """Collect messages until one matches; empty list on timeout."""
    deadline = time.monotonic() + timeout
    messages: list[dict] = []
    while time.monotonic() < deadline:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
        except asyncio.TimeoutError:
            continue
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            continue
        messages.append(msg)
        if predicate(msg):
            return messages
    return messages


async def connect_workspace(appliance, token: str, workspace_id: str):
    """Open a WS, connect to the workspace, wait for container_ready.

    Retries the whole dial through a graceful-restart window: a recycle
    still in flight closes fresh sockets with 1012 (the drain's
    ``disconnect_all``), so a single attempt is not conclusive — keep
    dialing until the container genuinely comes up or the deadline
    passes (the #2527 pattern the backend e2e suite uses).
    """
    deadline = time.monotonic() + 180.0
    last: dict | None = None
    while time.monotonic() < deadline:
        try:
            ws = await dial(appliance, token)
        except websockets.ConnectionClosed:
            await asyncio.sleep(1)  # mid-drain close; retry
            continue
        try:
            await ws.send(
                json.dumps(
                    {
                        "cmd": "workspace_connect",
                        "workspaceId": workspace_id,
                    }
                )
            )
            got = await recv_until(ws, _is_container_ready, timeout=60.0)
            if any(_is_container_ready(m) for m in got):
                return ws
            last = got[-1] if got else None
        except websockets.ConnectionClosed:
            pass  # residual 1012 from a still-draining recycle; retry
        await ws.close()
        await asyncio.sleep(1)
    raise AssertionError(
        f"container_ready not received within 180s; last={last!r}"
    )


async def exec_command(
    ws, command: list[str], timeout: float = 120.0
) -> tuple[str, int | None]:
    """Run a command via the WS exec path; return (output, exit code)."""
    await ws.send(json.dumps({"cmd": "exec_start", "command": command}))
    messages = await recv_until(
        ws, lambda m: m.get("type") == "exec_exit", timeout=timeout
    )
    outputs = [
        base64.b64decode(m["data"])
        for m in messages
        if m.get("type") == "exec_output" and "data" in m
    ]
    exits = [m.get("code") for m in messages if m.get("type") == "exec_exit"]
    return (
        b"".join(outputs).decode(errors="replace"),
        exits[0] if exits else None,
    )
