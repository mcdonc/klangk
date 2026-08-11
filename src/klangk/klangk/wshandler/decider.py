"""Consent-decider WebSocket endpoint (#2308, #2244).

A decider (a live client that can approve/deny held egress -- the ``klangk``
CLI in #2310, or a Flutter client) connects here to register its live presence
for a workspace (or deploy-wide) AND to act on held requests. The connection
lifecycle drives the :class:`~klangk.consent_deciders.ConsentDeciderRegistry`:
connect -> register, client ``ping`` -> touch (liveness), disconnect ->
deregister. While at least one decider is registered the workspace is
"interactive" (its blocked egress is held for a decision, #2311); with none,
it reverts to static allow-list.

#2244 adds the decision half on top of #2308's registration/liveness:

- **fan-in**: on connect, the workspace's currently-pending requests are
  replayed (snapshot) so a decider joining mid-flight sees in-flight holds; new
  holds are pushed live as ``egress_request`` frames by the coordinator's
  fanout (over this same socket).
- **fan-out**: a ``verdict`` message from the decider is fed to
  :meth:`ConsentCoordinator.resolve`, which records the decision, releases the
  held sidecar connection, and broadcasts ``egress_resolved`` so co-deciders
  drop it (first-decision-wins).

Authorization (#2244 closes the #2308 authz gap): a workspace-scoped decider
(``?workspace=<id>``) must have ``terminal`` access to that workspace (owner,
member, or spectator -- anyone who can open a terminal there); a deploy-wide
decider (no ``workspace``) must be an admin. A verdict is honored only if it
targets the decider's own workspace (defense-in-depth), enforced in ``resolve``
via ``decider_workspace``. Auth mirrors the main ``/ws`` handler: a user JWT in
the ``token`` query param.

Outbound writes go through :class:`SafeWebSocket` (bounded queue +
``SlowClientError``) like the main ``/ws`` handler.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from .. import auth
from .safe_websocket import SafeWebSocket, SlowClientError
from ..model.egress_consent import (
    DECISION_ALLOWED,
    DECISION_DENIED,
    DURATIONS,
    DURATION_DEFAULT,
    SCOPES,
)

logger = logging.getLogger(__name__)


async def handle_consent_decider(websocket: WebSocket, app) -> None:
    """Register a consent decider for its connection lifetime + act on holds."""
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

    # Authorization: workspace-scoped deciders need terminal access to the
    # workspace (owner or member); deploy-wide deciders need admin. Mirrors the
    # main /ws handler's workspace gate (wshandler/connection.py).
    principals = await app.state.acl.get_principals(user["id"])
    if workspace is not None:
        allowed = await app.state.acl.check_permission(
            f"/workspaces/{workspace}", principals, "terminal"
        )
    else:
        allowed = await app.state.acl.check_permission(
            "/admin", principals, "admin"
        )
    if not allowed:
        await websocket.close(code=4003, reason="Forbidden")
        return

    await websocket.accept()
    safe_ws = SafeWebSocket(websocket)
    safe_ws.start_sender()
    registry = app.state.consent_deciders
    decider_id = str(uuid.uuid4())
    email = user.get("email")
    try:
        registry.register(decider_id, workspace, email, safe_ws)
        # Register BEFORE reading the snapshot: a hold created between the two
        # is then delivered twice (once live via fanout, once from the
        # snapshot). That duplicate is benign -- the second verdict no-ops
        # (resolve pops first) -- whereas snapshot-then-register would MISS
        # holds created in the gap (silent fail-close). Tolerate the duplicate
        # to never miss.
        for frame in await app.state.consent_coordinator.snapshot(workspace):
            safe_ws.send_json(frame)
        while True:
            # Starlette raises RuntimeError ("WebSocket is not connected...")
            # on a client disconnect during receive_text(); treat it the same
            # as WebSocketDisconnect.
            try:
                raw = await safe_ws.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            try:
                if mtype == "ping":
                    registry.touch(decider_id)
                    safe_ws.send_json({"type": "pong"})
                elif mtype == "verdict":
                    await _handle_verdict(
                        app, safe_ws, msg, workspace, user["id"]
                    )
                # unknown types are ignored
            except SlowClientError:
                # Outbound queue full -- the client can't keep up. Drop it
                # (matches the main /ws handler's slow-client handling).
                break
            except Exception:
                # A single bad verdict or transient send error must not tear
                # down the whole connection (and every other in-flight prompt
                # on it). Log + keep going.
                logger.exception(
                    "consent decider: error handling %r message", mtype
                )
    finally:
        # Connection gone (clean disconnect, error, or crash) -> drop the
        # registration so the workspace reverts to static (#2308).
        registry.deregister(decider_id)
        await safe_ws.stop_sender()


async def _handle_verdict(app, safe_ws, msg, workspace, user_id) -> None:
    """Validate + apply a decider's verdict to a held request (#2244)."""
    decision = msg.get("decision")
    if decision not in (DECISION_ALLOWED, DECISION_DENIED):
        safe_ws.send_json(
            {"type": "error", "message": f"invalid decision: {decision!r}"}
        )
        return
    scope = msg.get("scope")
    if scope is not None and scope not in SCOPES:
        safe_ws.send_json(
            {"type": "error", "message": f"invalid scope: {scope!r}"}
        )
        return
    duration = msg.get("duration") or DURATION_DEFAULT
    if duration not in DURATIONS:
        safe_ws.send_json(
            {"type": "error", "message": f"invalid duration: {duration!r}"}
        )
        return
    request_id = msg.get("request_id")
    if not isinstance(request_id, str):
        safe_ws.send_json(
            {"type": "error", "message": "verdict requires a request_id"}
        )
        return
    await app.state.consent_coordinator.resolve(
        request_id,
        decision,
        scope,
        # decided_by is the stable user id (egress_consent.decided_by REFERENCES
        # users(id); the email is volatile + not a key). Never NULL for a human
        # decision -- NULL means "no human" (the static/expired case).
        user_id,
        duration=duration,
        decider_workspace=workspace,
    )
