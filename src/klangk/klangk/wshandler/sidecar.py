"""Egress-sidecar WebSocket endpoint (#2311).

The network sidecar connects here -- one socket per workspace, at startup --
and sends each blocked egress destination as ``{type:egress, id, dst, dport}``.
For each, the coordinator gate-checks: interactive + a live decider -> the
request is *held* (the sidecar keeps the connection in-flight) and this
endpoint relays the eventual verdict back as ``{type:verdict, id, decision}``;
otherwise the coordinator records a static denial and returns deny at once,
and the sidecar NXDOMAIN/DROPs immediately (no hold).

Auth is the workspace's own JWT (the sidecar shares it, validated by Caddy's
``forward_auth`` and re-decoded here for the workspace id) -- the same
credential the fire-and-forget POST endpoint uses, now over the sidecar leg of
the consent WS contract (``sidecar <-WS-> klangkd <-WS-> decider``). No bespoke
credential; the workspace id comes straight from the token.

This is the klangkd half of #2311. The sidecar's kernel-level hold (suspending
DNS queries, deferring NFQUEUE verdicts) + its WS client are implemented in
the sidecar (``src/containers/network/proxy.py``); #2244 wires the decider
fanout to :meth:`klangk.consent_coordinator.ConsentCoordinator.resolve`.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from .. import auth
from .safe_websocket import WS_ERRORS, SlowClientError, SafeWebSocket

logger = logging.getLogger(__name__)


async def handle_egress_sidecar(websocket: WebSocket, app) -> None:
    """Receive blocked-egress events from the sidecar; relay verdicts back."""
    # forward_auth validated the workspace JWT from the Authorization header
    # on the egress site; re-read it here for the workspace id. The ?token=
    # query-param fallback covers the ingress path and handler-level tests.
    authorization = websocket.headers.get("authorization", "")
    token = authorization[7:] if authorization.startswith("Bearer ") else None
    if not token:
        token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return
    result = app.state.auth.decode_workspace_token(token)
    if result is auth.Auth.WORKSPACE_TOKEN_EXPIRED:
        await websocket.close(code=4002, reason="Token expired")
        return
    if result is None:
        await websocket.close(code=4001, reason="Invalid token")
        return
    workspace_id = result

    await websocket.accept()
    coordinator = app.state.consent_coordinator
    safe = SafeWebSocket(websocket)
    safe.start_sender()
    relay_tasks: set[asyncio.Task] = set()
    try:
        while True:
            # Starlette raises RuntimeError ("WebSocket is not connected...")
            # on a client disconnect during receive_text(); treat it the same
            # as WebSocketDisconnect.
            try:
                raw = await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") != "egress":
                continue
            local_id = msg.get("id")
            dst = msg.get("dst")
            dport = msg.get("dport")
            if not isinstance(local_id, str) or not isinstance(dst, str):
                continue
            if dport is not None and (
                not isinstance(dport, int) or isinstance(dport, bool)
            ):
                continue
            # One relay task per egress event: it holds via the coordinator
            # (awaiting the verdict Future) and sends the verdict when ready,
            # without blocking the receive loop (other events stream through).
            task = asyncio.create_task(
                _relay_verdict(
                    safe, coordinator, workspace_id, local_id, dst, dport
                )
            )
            relay_tasks.add(task)
            task.add_done_callback(relay_tasks.discard)
    finally:
        # Sidecar gone (disconnect/restart/crash): cancel in-flight relays.
        # The sidecar's own held connections die with it (fail-close); the
        # coordinator's per-hold timeout expires the orphaned pending rows.
        for task in list(relay_tasks):
            task.cancel()
        await safe.stop_sender()


async def _relay_verdict(
    safe: SafeWebSocket,
    coordinator,
    workspace_id: str,
    local_id: str,
    dst: str,
    dport: int | None,
) -> None:
    """Hold the egress via the coordinator and send the verdict when resolved."""
    try:
        future = await coordinator.hold(workspace_id, dst, dport)
        # shield: if the sidecar disconnects and this relay is cancelled, the
        # coordinator's owned Future must NOT be cancelled with it -- the hold
        # is cleaned by its own timeout (expire), not by the relay dying.
        verdict = await asyncio.shield(future)
        safe.send_json(
            {
                "type": "verdict",
                "id": local_id,
                "decision": verdict["decision"],
            }
        )
    except (asyncio.CancelledError, SlowClientError, *WS_ERRORS):
        # Cancelled: the sidecar socket closed (the relay is moot). SlowClient
        # / WS_ERRORS: the outbound socket is gone. Nothing more to do.
        pass
