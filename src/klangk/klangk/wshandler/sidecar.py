"""Egress-sidecar WebSocket endpoint (#2311).

The network sidecar connects here -- one socket per workspace, at startup --
and sends each blocked egress destination as ``{type:egress, id, dst, dport}``.
For each, the coordinator gate-checks: interactive + a live decider -> the
request is *held* (the sidecar keeps the connection in-flight) and this
endpoint relays the eventual verdict back as ``{type:verdict, id, decision}`;
otherwise the coordinator records a static denial and returns deny at once,
and the sidecar NXDOMAIN/DROPs immediately (no hold). It also accepts
``{type:egress_dns, decision, host}`` frames -- the DNS proxy's unconditional
outcome audit (#2304; every allow-listed resolution ``allowed``, every
reject-listed NXDOMAIN ``denied``) -- recorded as policy rows without a
verdict round-trip.

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
    # #2339: record this workspace's live sidecar socket so a revoke can push
    # a drop-rule to it + correlate the ack.
    app.state.sidecar_connections.register(workspace_id, safe)
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
            mtype = msg.get("type")
            if mtype == "drop_ack":
                # Sidecar confirmed a rule-drop (revocation, #2339): resolve
                # the awaiting revoke's ack Future.
                app.state.sidecar_connections.resolve_ack(
                    msg.get("id"), bool(msg.get("ok", False))
                )
                continue
            if mtype == "activity":
                # Sidecar reports egress/network activity (#2479): an
                # egress-only workload bypasses klangkd, so without this its
                # idle timer would never advance and the container would be
                # reaped mid-egress. The sidecar flood-gates the frame; here
                # the bump is a single float write on this loop thread.
                state = app.state.container_registry.states.get(workspace_id)
                if state is not None:
                    state.record_activity()
                continue
            if mtype == "egress_dns":
                # Sidecar reports a DNS-layer egress outcome (#2304): every
                # allow-listed resolution (allowed) and reject-listed
                # NXDOMAIN (denied) is recorded unconditionally -- full
                # egress auditing with no opt-in setting. One task per frame
                # (like the egress relays) so the DB write never blocks the
                # receive loop; best-effort inside the coordinator.
                decision = msg.get("decision")
                host = msg.get("host")
                if (
                    decision in ("allowed", "denied")
                    and isinstance(host, str)
                    and host
                ):
                    task = asyncio.create_task(
                        coordinator.record_dns_event(
                            workspace_id, decision, host
                        )
                    )
                    relay_tasks.add(task)
                    task.add_done_callback(relay_tasks.discard)
                continue
            if mtype != "egress":
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
        # Sidecar gone (disconnect/restart/crash): drop its registration (any
        # in-flight revoke ack fails at once) + cancel in-flight relays.
        app.state.sidecar_connections.deregister(workspace_id)
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
                "duration": verdict.get("duration", "once"),
            }
        )
    except (asyncio.CancelledError, SlowClientError, *WS_ERRORS):
        # Cancelled: the sidecar socket closed (the relay is moot). SlowClient
        # / WS_ERRORS: the outbound socket is gone. Nothing more to do.
        pass
