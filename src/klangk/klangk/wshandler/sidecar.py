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
the sidecar (the ``klangksidecar`` package, ``src/klangksidecar``); #2244 wires the decider
fanout to :meth:`klangk.consent.coordinator.ConsentCoordinator.resolve`.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from .. import auth
from .safe_websocket import WS_ERRORS, SlowClientError, SafeWebSocket

logger = logging.getLogger(__name__)


def _egress_token(websocket: WebSocket) -> str | None:
    """The workspace token from the Authorization header or ?token=."""
    authorization = websocket.headers.get("authorization", "")
    token = authorization[7:] if authorization.startswith("Bearer ") else None
    if not token:
        token = websocket.query_params.get("token")
    return token


async def authenticate_egress_socket(websocket: WebSocket, app) -> str | None:
    """Validate the sidecar socket's workspace token; close and return None
    on failure (forward_auth already checked the header on the egress site)."""
    token = _egress_token(websocket)
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return None
    result = app.state.auth.decode_workspace_token(token)
    if result is auth.Auth.WORKSPACE_TOKEN_EXPIRED:
        await websocket.close(code=4002, reason="Token expired")
        return None
    if result is None:
        await websocket.close(code=4001, reason="Invalid token")
        return None
    return result


async def handle_egress_sidecar(websocket: WebSocket, app) -> None:
    """Receive blocked-egress events from the sidecar; relay verdicts back."""
    # forward_auth validated the workspace JWT from the Authorization header
    # on the egress site; re-read it here for the workspace id. The ?token=
    # query-param fallback covers the ingress path and handler-level tests.
    workspace_id = await authenticate_egress_socket(websocket, app)
    if workspace_id is None:
        return

    await websocket.accept()
    coordinator = app.state.consent_coordinator
    safe = SafeWebSocket(websocket)
    safe.start_sender()
    # #2339: record this workspace's live sidecar socket so a revoke can push
    # a drop-rule to it + correlate the ack.
    app.state.sidecar_connections.register(workspace_id, safe)
    relay_tasks: set[asyncio.Task] = set()
    try:
        await _sidecar_event_loop(
            app, websocket, coordinator, workspace_id, safe, relay_tasks
        )
    finally:
        # Sidecar gone (disconnect/restart/crash): drop its registration (any
        # in-flight revoke ack fails at once) + cancel in-flight relays.
        # Identity-guarded (#3069): a stale socket's teardown must not drop
        # a replacement socket's registration.
        app.state.sidecar_connections.deregister(workspace_id, safe)
        for task in list(relay_tasks):
            task.cancel()
        await safe.stop_sender()


async def _sidecar_event_loop(
    app,
    websocket: WebSocket,
    coordinator,
    workspace_id: str,
    safe: SafeWebSocket,
    relay_tasks: set[asyncio.Task],
) -> None:
    """Receive and dispatch sidecar frames until the socket drops."""
    while True:
        stop, msg = await _receive_sidecar_frame(websocket)
        if stop:
            break
        if msg is not None:
            dispatch_sidecar_message(
                app, safe, coordinator, workspace_id, relay_tasks, msg
            )


def _record_sidecar_activity(app, workspace_id: str) -> None:
    """Record the sidecar's egress/network activity bump (#2479): an
    egress-only workload bypasses klangkd, so without this its idle
    timer would never advance and the container would be reaped
    mid-egress. The sidecar flood-gates the frame; here the bump is a
    single float write on this loop thread."""
    state = app.state.container_registry.states.get(workspace_id)
    if state is not None:
        state.record_activity()


async def _receive_sidecar_frame(websocket) -> tuple[bool, dict | None]:
    """(stop, frame): stop on disconnect; a None frame is skipped
    (malformed / non-dict)."""
    try:
        raw = await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        # Starlette raises RuntimeError ("WebSocket is not connected...")
        # on a client disconnect during receive_text(); treat it the same
        # as WebSocketDisconnect.
        return True, None
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return False, None
    if not isinstance(msg, dict):
        return False, None
    return False, msg


def dispatch_sidecar_message(
    app,
    safe: SafeWebSocket,
    coordinator,
    workspace_id: str,
    relay_tasks: set[asyncio.Task],
    msg: dict,
) -> None:
    """Act on one decoded sidecar frame (drop-ack, activity, or egress)."""
    mtype = msg.get("type")
    if mtype == "drop_ack":
        # Sidecar confirmed a rule-drop (revocation, #2339): resolve
        # the awaiting revoke's ack Future.
        app.state.sidecar_connections.resolve_ack(
            msg.get("id"), bool(msg.get("ok", False))
        )
        return
    if mtype == "activity":
        _record_sidecar_activity(app, workspace_id)
        return
    if mtype != "egress":
        return
    fields = parse_egress_fields(msg)
    if fields is None:
        return
    local_id, dst, dport = fields
    # One relay task per egress event: it holds via the coordinator
    # (awaiting the verdict Future) and sends the verdict when ready,
    # without blocking the receive loop (other events stream through).
    task = asyncio.create_task(
        _relay_verdict(safe, coordinator, workspace_id, local_id, dst, dport)
    )
    relay_tasks.add(task)
    task.add_done_callback(relay_tasks.discard)


def _valid_dport(dport) -> bool:
    """A dport is None or an int (bool excluded: it subclasses int)."""
    if dport is None:
        return True
    return isinstance(dport, int) and not isinstance(dport, bool)


def parse_egress_fields(msg: dict) -> tuple[str, str, int | None] | None:
    """(local_id, dst, dport) from an egress frame, or None on a malformed
    one."""
    local_id = msg.get("id")
    dst = msg.get("dst")
    dport = msg.get("dport")
    if not isinstance(local_id, str) or not isinstance(dst, str):
        return None
    if not _valid_dport(dport):
        return None
    return local_id, dst, dport


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
                "duration": verdict.get("duration", "once"),
            }
        )
    except (asyncio.CancelledError, SlowClientError, *WS_ERRORS):
        # Cancelled: the sidecar socket closed (the relay is moot). SlowClient
        # / WS_ERRORS: the outbound socket is gone. Nothing more to do.
        pass
