"""Consent-decider WebSocket endpoint (#2308, #2244).

A decider (a live client that can approve/deny held egress -- the ``klangk``
CLI in #2310, or a Flutter client) connects here to register its live presence
for a workspace (or deploy-wide) AND to act on held requests. The connection
lifecycle drives the :class:`~klangk.consent.deciders.ConsentDeciderRegistry`:
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
import time
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from .. import auth
from .safe_websocket import SafeWebSocket, SlowClientError
from ..model.egress_consent import (
    DECISION_ALLOWED,
    DECISION_DENIED,
    DURATIONS,
    DURATION_DEFAULT,
)
from ..model.workspaces import EGRESS_MODE_INTERACTIVE

logger = logging.getLogger(__name__)


def _log_safe(value: str) -> str:
    """Strip CR/LF so a forged query param can't inject log lines."""
    return value.replace("\r", " ").replace("\n", " ")


async def _refuse(
    websocket: WebSocket, code: int, reason: str, email: str | None = None
) -> None:
    """Close a refused decider handshake before ``accept()`` (#2490).

    A pre-accept close is answered by the ASGI server with a bare HTTP 403
    -- the close code and reason never reach the client -- so log the
    refusal server-side (reason, user, workspace, User-Agent) to make the
    403s in the klangkd log self-explanatory instead of anonymous noise.
    """
    logger.warning(
        "consent decider connection refused: %s (code %d) user=%s "
        "workspace=%s ua=%s",
        reason,
        code,
        _log_safe(email or "unknown"),
        _log_safe(websocket.query_params.get("workspace") or "deploy-wide"),
        websocket.headers.get("user-agent", "?"),
    )
    await websocket.close(code=code, reason=reason)


async def _refuse_invalid_handshake(
    websocket: WebSocket, app, workspace: str | None, user: dict
) -> bool:
    """Authorization + static-mode gate for a consent decider.

    Workspace-scoped deciders need terminal access to the workspace (owner
    or member); deploy-wide deciders need admin. Mirrors the main /ws
    handler's workspace gate (wshandler/connection.py).

    #2394: a static-mode workspace never holds egress for a human decision
    (its non-allow-listed egress is denied immediately with a static
    verdict). Refuse a workspace-scoped decider at registration so the
    static/interactive boundary is structural (enforced here), not just
    behavioral (the coordinator's hold-time ``_is_interactive`` gate stays
    as defense-in-depth). Reads the same egress_mode the coordinator does.
    Deploy-wide deciders (workspace None) are unaffected -- they cover
    interactive workspaces without flipping a static one.

    NB: these closes run before websocket.accept(), so the ASGI server
    (uvicorn) answers the handshake with a bare HTTP 403 -- the close code
    and reason are NOT transmitted to the client (same as the authz close
    above). _refuse() therefore also logs the reason server-side (#2490):
    the log line is the only place a 403 storm is attributable.

    Returns (refused, principals) — principals (needed by the pause /
    unpause handlers) is only meaningful when not refused."""
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
        await _refuse(websocket, 4003, "Forbidden", user.get("email"))
        return True, principals
    if workspace is not None:
        ws = await app.state.model.workspaces.get_workspace(workspace)
        if ws is None:
            # Vanished between the authz check and now (a delete race) -- the
            # workspace authz just passed against can no longer be registered.
            await _refuse(websocket, 4003, "Forbidden", user.get("email"))
            return True, principals
        if ws.get("egress_mode") != EGRESS_MODE_INTERACTIVE:
            await _refuse(
                websocket,
                4003,
                "workspace egress mode is not interactive",
                user.get("email"),
            )
            return True, principals
    return False, principals


def _log_handshake_timing(t0: float, marks: list[tuple[str, float]]) -> None:
    """#2420: log the pre-accept step timings (see handle_consent_decider)."""
    prev = t0
    parts: list[str] = []
    for label, t in marks:
        parts.append(f"{label}={(t - prev) * 1000:.0f}ms")
        prev = t
    logger.info(
        "consent decider handshake accepted: %.0fms (%s)",
        (marks[-1][1] - t0) * 1000,
        " ".join(parts),
    )


async def _dispatch_decider_message(
    app,
    registry,
    safe_ws,
    msg: dict,
    workspace: str | None,
    user_id: str,
    decider_id: str,
    principals,
) -> None:
    """Act on one parsed decider frame by its ``type``.

    Unknown types are ignored. Raises whatever the per-type handler raises
    (SlowClientError from sends is handled by the receive loop)."""
    mtype = msg.get("type")
    if mtype == "ping":
        registry.touch(decider_id)
        safe_ws.send_json({"type": "pong"})
    elif mtype == "verdict":
        await _handle_verdict(app, safe_ws, msg, workspace, user_id)
    elif mtype == "revoke":
        # #2339: drop the sidecar rule + mark the verdict revoked.
        # ok=False if it's not an active verdict, is outside this
        # decider's workspace, or the sidecar never acked the drop.
        ok = await app.state.consent_coordinator.revoke(
            msg.get("request_id"),
            user_id,
            decider_workspace=workspace,
        )
        safe_ws.send_json(
            {
                "type": "revoke_ack",
                "request_id": msg.get("request_id"),
                "ok": ok,
            }
        )
    elif mtype == "pause":
        # #2332: pause interactive consent prompting for this
        # decider's workspace for a window. A deploy-wide decider
        # (workspace None) has no single workspace to pause -> nack.
        await _handle_pause(app, safe_ws, msg, workspace, principals)
    elif mtype == "unpause":
        # #2332: clear an active pause for this decider's workspace.
        await _handle_unpause(app, safe_ws, workspace, principals)


async def _decider_authenticate(websocket: WebSocket, app, _hs_mark):
    """Validate the decider socket's token; refuse + None on failure."""
    token = websocket.query_params.get("token")
    if not token:
        await _refuse(websocket, 4001, "Missing token")
        return None
    result = await app.state.auth.get_user_from_token(token)
    _hs_mark("token")
    if result is auth.Auth.TOKEN_EXPIRED:
        await _refuse(websocket, 4002, "Token expired")
        return None
    if result is None:
        await _refuse(websocket, 4001, "Invalid token")
        return None
    return result


async def _decider_receive_loop(
    app,
    registry,
    safe_ws,
    workspace,
    user: dict,
    decider_id: str,
    principals,
) -> None:
    """Receive + dispatch decider frames until the socket drops or the
    client falls behind."""
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
            await _dispatch_decider_message(
                app,
                registry,
                safe_ws,
                msg,
                workspace,
                user["id"],
                decider_id,
                principals,
            )
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


async def handle_consent_decider(websocket: WebSocket, app) -> None:
    """Register a consent decider for its connection lifetime + act on holds."""
    # #2420: time the pre-accept steps so an intermittent opening-handshake
    # timeout (DECIDER2-HANDSHAKE) is attributable from a logged run -- a slow
    # step names the in-handler culprit; a small total whose wall-clock lags
    # the client's connect by the timeout means the stall is pre-handler
    # (event loop / uvicorn / reverse proxy), not in the accept path.
    _hs_t0 = time.monotonic()
    _hs_marks: list[tuple[str, float]] = []

    def _hs_mark(label: str) -> None:
        _hs_marks.append((label, time.monotonic()))

    user = await _decider_authenticate(websocket, app, _hs_mark)
    if user is None:
        return
    workspace = websocket.query_params.get("workspace")  # None = deploy-wide

    refused, principals = await _refuse_invalid_handshake(
        websocket, app, workspace, user
    )
    if refused:
        return
    _hs_mark("authz")
    _hs_mark("workspace")
    await websocket.accept()
    _hs_mark("accept")
    _log_handshake_timing(_hs_t0, _hs_marks)
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
        # The in-effect rules snapshot (#2335 slice A): lets a decider joining
        # mid-flight see what's currently allowed/denied, not just holds. None
        # for a deploy-wide decider (no single workspace) — matches snapshot().
        rules = await app.state.consent_coordinator.rules_frame(workspace)
        if rules is not None:
            safe_ws.send_json(rules)
        await _decider_receive_loop(
            app, registry, safe_ws, workspace, user, decider_id, principals
        )
    finally:
        # Connection gone (clean disconnect, error, or crash) -> drop the
        # registration so the workspace reverts to static (#2308).
        registry.deregister(decider_id)
        await safe_ws.stop_sender()


async def _can_pause(app, workspace, principals) -> bool:
    """Pause/unpause authz (#2332, review I1).

    Pause silences ALL consent prompts workspace-wide for a window (a
    workspace-wide policy change, broader than deciding one request), so it
    needs a higher bar than the connection's ``terminal`` gate: the
    ``share-terminals`` permission (collaborators + owners). Spectators and
    coders (terminal only) may still connect and decide individual requests.
    """
    if workspace is None:
        return False  # deploy-wide decider has no single workspace to pause
    return await app.state.acl.check_permission(
        f"/workspaces/{workspace}", principals, "share-terminals"
    )


async def _handle_pause(app, safe_ws, msg, workspace, principals) -> None:
    """Pause consent prompting for the decider's workspace (#2332)."""
    if not await _can_pause(app, workspace, principals):
        # Missing share-terminals, or a deploy-wide decider with no target.
        safe_ws.send_json({"type": "pause_ack", "ok": False, "until": None})
        return
    result = await app.state.consent_coordinator.pause(
        workspace, msg.get("duration")
    )
    safe_ws.send_json(
        {
            "type": "pause_ack",
            "ok": result["ok"],
            "until": result["until"],
        }
    )


async def _handle_unpause(app, safe_ws, workspace, principals) -> None:
    """Clear an active consent pause for the decider's workspace (#2332)."""
    if not await _can_pause(app, workspace, principals):
        safe_ws.send_json({"type": "pause_ack", "ok": False, "until": None})
        return
    result = await app.state.consent_coordinator.unpause(workspace)
    safe_ws.send_json({"type": "pause_ack", "ok": result["ok"], "until": None})


async def _handle_verdict(app, safe_ws, msg, workspace, user_id) -> None:
    """Validate + apply a decider's verdict to a held request (#2244)."""
    decision = msg.get("decision")
    if decision not in (DECISION_ALLOWED, DECISION_DENIED):
        safe_ws.send_json(
            {"type": "error", "message": f"invalid decision: {decision!r}"}
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
        # decided_by is the stable user id (egress_consent.decided_by REFERENCES
        # users(id); the email is volatile + not a key). Never NULL for a human
        # decision -- NULL means "no human" (the static/expired case).
        user_id,
        duration=duration,
        decider_workspace=workspace,
    )
