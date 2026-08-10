"""Consent-decider WebSocket endpoint (#2308).

A decider (a live client that can approve/deny held egress -- the ``klangk``
CLI in #2310, or a Flutter client) connects here to register its live presence
for a workspace (or deploy-wide). The connection lifecycle drives the
:class:`~klangk.consent_deciders.ConsentDeciderRegistry`: connect -> register,
client ``ping`` -> touch (liveness), disconnect -> deregister. While at least
one decider is registered the workspace is "interactive" (its blocked egress is
held for a decision, #2311); with none, it reverts to static allow-list.

This endpoint owns **registration + liveness only**. The event content --
pushing held requests down and receiving verdicts -- lands with #2244 and is
carried over this same socket; until then the only client message is ``ping``.

Auth mirrors the main ``/ws`` handler: a user JWT in the ``token`` query param.
Scope is ``?workspace=<id>`` (workspace-scoped); omit it for a deploy-wide
decider. Authorization to decide for a workspace is enforced when decision
power arrives (#2244); presence registration itself grants no decision power.
"""

from __future__ import annotations

import json
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from .. import auth


async def handle_consent_decider(websocket: WebSocket, app) -> None:
    """Register a consent decider for its connection lifetime."""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return
    result = await app.state.auth.get_user_from_token(token)
    if result is auth.Auth.TOKEN_EXPIRED:
        await websocket.close(code=4002, reason="Token expired")
        return
    if result is None:
        await websocket.close(code=4001, reason="Invalid token")
        return
    user = result
    workspace = websocket.query_params.get("workspace")  # None = deploy-wide
    # TODO(#2244): authorize workspace membership here. Today any authenticated
    # user can register a decider for any workspace (or deploy-wide); presence
    # alone grants no decision power, but it flips an interactive workspace
    # from static-deny to held-pending. #2244 must enforce membership before
    # granting any decision power.

    await websocket.accept()
    decider_id = str(uuid.uuid4())
    registry = app.state.consent_deciders
    try:
        # register inside the try so the finally-deregister invariant holds
        # structurally rather than relying on register() not raising.
        registry.register(decider_id, workspace, user.get("email"))
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
            if msg.get("type") == "ping":
                registry.touch(decider_id)
                # NOTE(#2244): when this endpoint fans out held requests, route
                # outbound writes through SafeWebSocket (bounded queue +
                # SlowClientError) like the main /ws handler; raw send_text is
                # fine while pong is the only outbound message.
                await websocket.send_text(json.dumps({"type": "pong"}))
    finally:
        # Connection gone (clean disconnect, error, or crash) -> drop the
        # registration so the workspace reverts to static (#2308).
        registry.deregister(decider_id)
