"""Consent-decider WebSocket endpoint (#2308, #2244).

A decider (a live client that can approve/deny held egress -- the ``klangk``
CLI in #2310, or a Flutter client) connects here to register its live presence
for a workspace AND to act on held requests. The connection
lifecycle drives the :class:`~klangk.consent.deciders.ConsentDeciderRegistry`:
connect -> register, client ``ping`` -> touch (liveness), disconnect ->
deregister. While at least one decider is registered the workspace is
"interactive" (its blocked egress is held for a decision, #2311); with none,
it reverts to static allow-list.

Consent is strictly a workspace concern (#2976): the ``workspace`` query
param is required, and there is no deploy-wide flavor. A workspace with
no connected decider simply reverts to the static allow-list fallback.

#2244 adds the decision half on top of #2308's registration/liveness:

- **fan-in**: on connect, the workspace's currently-pending requests are
  replayed (snapshot) so a decider joining mid-flight sees in-flight holds; new
  holds are pushed live as ``egress_request`` frames by the coordinator's
  fanout (over this same socket).
- **fan-out**: a ``verdict`` message from the decider is fed to
  :meth:`ConsentCoordinator.resolve`, which records the decision, releases the
  held sidecar connection, and broadcasts ``egress_resolved`` so co-deciders
  drop it (first-decision-wins).

Authorization (#2244 closes the #2308 authz gap): a decider must be
workspace-scoped (``?workspace=<id>``) and hold the ``egress-consent``
permission on that workspace (owner, coder, or collaborator -- #2883;
spectators are watch-only and never register). A verdict is honored only if
it targets the decider's own workspace (defense-in-depth), enforced in
``resolve`` via ``decider_workspace``. Pause/unpause share the same single
gate as the connection itself (#2883): anyone who may register may also
pause. Auth mirrors the main ``/ws`` handler: a user JWT in the handshake's
``Sec-WebSocket-Protocol`` header (#3201 -- browsers cannot set headers
like ``Authorization`` on a WS connect, and a ``?token=`` query param
would land in proxy/server access logs) — including its revocation story (#3162): the handshake
records the token's JTI on the registry entry, and a hard revocation
(logout, session-limit eviction) closes the decider socket with 4001,
just like the main handler's connections (#3152), and an account
disable closes every decider of the user (#3162, mirroring the #2588
per-user kick). Refresh rotation retargets the entry onto the new JTI
instead of closing it.

Outbound writes go through :class:`SafeWebSocket` (bounded queue +
``SlowClientError``) like the main ``/ws`` handler.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import WebSocket, WebSocketDisconnect
from jose import ExpiredSignatureError, JWTError

from .safe_websocket import SafeWebSocket, SlowClientError
from .dispatch import ws_workstation
from .support import ws_bearer_token, ws_echo_subprotocol
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
        _log_safe(websocket.query_params.get("workspace") or "missing"),
        websocket.headers.get("user-agent", "?"),
    )
    await websocket.close(code=code, reason=reason)


async def _refuse_invalid_handshake(
    websocket: WebSocket, app, workspace: str, user: dict, *, hs_mark
) -> bool:
    """Authorization + static-mode gate for a consent decider.

    The decider must have the ``egress-consent`` permission on
    the workspace (owner, coder, or collaborator -- #2883; a spectator
    has only watch access and is refused here, so it can never reach the
    verdict/revoke/pause handlers).
    Mirrors the main /ws handler's workspace gate
    (wshandler/connection.py).

    #2394: a static-mode workspace never holds egress for a human decision
    (its non-allow-listed egress is denied immediately with a static
    verdict). Refuse a workspace-scoped decider at registration so the
    static/interactive boundary is structural (enforced here), not just
    behavioral (the coordinator's hold-time interactivity gate stays
    as defense-in-depth). Reads the same egress_mode the coordinator does.

    NB: these closes run before websocket.accept(), so the ASGI server
    (uvicorn) answers the handshake with a bare HTTP 403 -- the close code
    and reason are NOT transmitted to the client (same as the authz close
    above). _refuse() therefore also logs the reason server-side (#2490):
    the log line is the only place a 403 storm is attributable.

    Returns True when the handshake was refused."""
    principals = await app.state.acl.get_principals(user["id"])
    allowed = await app.state.acl.check_permission(
        f"/workspaces/{workspace}", principals, "egress-consent"
    )
    hs_mark("authz")
    if not allowed:
        await _refuse(websocket, 4003, "Forbidden", user.get("email"))
        return True
    ws = await app.state.model.workspaces.get_workspace(workspace)
    hs_mark("workspace")
    if ws is None:
        # Vanished between the authz check and now (a delete race) -- the
        # workspace authz just passed against can no longer be registered.
        await _refuse(websocket, 4003, "Forbidden", user.get("email"))
        return True
    if ws.get("egress_mode") != EGRESS_MODE_INTERACTIVE:
        await _refuse(
            websocket,
            4003,
            "workspace egress mode is not interactive",
            user.get("email"),
        )
        return True
    return False


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


async def _dispatch_decision_frame(
    app, safe_ws, msg, workspace, user_id, mtype
) -> bool:
    """Handle a verdict/revoke frame; False when *mtype* is neither."""
    if mtype == "verdict":
        await _handle_verdict(app, safe_ws, msg, workspace, user_id)
        return True
    if mtype == "revoke":
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
        return True
    return False


async def _dispatch_session_frame(
    app, registry, safe_ws, msg, workspace, decider_id, mtype
) -> bool:
    """Handle a ping/pause/unpause frame; False when *mtype* is none of
    them (unknown types are ignored)."""
    if mtype == "ping":
        registry.touch(decider_id)
        safe_ws.send_json({"type": "pong"})
        return True
    if mtype == "pause":
        # #2332: pause interactive consent prompting for this
        # decider's workspace for a window.
        await _handle_pause(app, safe_ws, msg, workspace)
        return True
    if mtype == "unpause":
        # #2332: clear an active pause for this decider's workspace.
        await _handle_unpause(app, safe_ws, workspace)
        return True
    return False


async def _dispatch_decider_message(
    app,
    registry,
    safe_ws,
    msg: dict,
    workspace: str,
    user_id: str,
    decider_id: str,
) -> None:
    """Act on one parsed decider frame by its ``type``.

    Unknown types are ignored. Raises whatever the per-type handler raises
    (SlowClientError from sends is handled by the receive loop)."""
    mtype = msg.get("type")
    if await _dispatch_decision_frame(
        app, safe_ws, msg, workspace, user_id, mtype
    ):
        return
    await _dispatch_session_frame(
        app, registry, safe_ws, msg, workspace, decider_id, mtype
    )


async def _decider_authenticate(websocket: WebSocket, app, _hs_mark):
    """Validate the decider socket's token; refuse + None on failure.

    Success returns ``(user, jti)`` — the token's JTI rides along so the
    registration can be targeted for closing when that token is later
    hard-revoked (#3162), mirroring ``ws_authenticate`` (#3152); a
    session bound to another workstation is refused (#3194), and a
    DPoP-bound token must prove possession (#3218).

    #3201: the token arrives in the ``Sec-WebSocket-Protocol`` handshake
    header, not a ``?token=`` query param (query strings land in
    proxy/server access logs); the one-shot DPoP proof still rides a
    ``dpop`` query param (#3218).
    """
    token = ws_bearer_token(websocket)
    if not token:
        await _refuse(websocket, 4001, "Missing token")
        return None
    a = app.state.auth
    payload = await _decode_decider_token(websocket, a, token)
    if payload is None:
        return None
    if not await _decider_dpop_gate(websocket, app, token, payload):
        return None
    user = await _decider_user_or_refuse(
        websocket, a, payload, ws_workstation(websocket, app)
    )
    if user is None:
        return None
    _hs_mark("token")
    return user, payload.get("jti")


async def _decode_decider_token(websocket: WebSocket, a, token: str):
    """The token payload, or None after refusing on failure."""
    try:
        return a.decode_token(token)
    except ExpiredSignatureError:
        await _refuse(websocket, 4002, "Token expired")
        return None
    except JWTError:
        await _refuse(websocket, 4001, "Invalid token")
        return None


async def _decider_dpop_gate(
    websocket: WebSocket, app, token, payload
) -> bool:
    """DPoP proof gate for the decider handshake (#3218) — mirrors the
    main socket's ``_dpop_gate`` (one-shot ``dpop`` query parameter)."""
    reason = app.state.auth.check_dpop(
        websocket.query_params.get("dpop"),
        "GET",
        websocket.url.path,
        token,
        payload,
    )
    if reason is None:
        return True
    await _refuse(websocket, 4001, "Invalid DPoP proof")
    return False


async def _decider_user_or_refuse(
    websocket: WebSocket, a, payload, workstation
):
    """The authenticated user for a decider registration, or None
    after refusing the handshake.

    Three refusal arms: an unknown user (4001), a session under the
    must_change_password flag (4004, #3172) — resolving egress holds
    is exactly the kind of action a forced-change session must not
    take, and 4004 matches the main WS gate in ws_authenticate — and
    a token presented from a different workstation (4001 via the
    binding check, #3194, mirroring the main gate).
    """
    user = await a._user_from_valid_payload(payload, workstation)
    if user is None:
        await _refuse(websocket, 4001, "Invalid token")
        return None
    if user.get("must_change_password"):
        await _refuse(websocket, 4004, "Password change required")
        return None
    return user


_FRAME_DISCONNECTED = object()


async def _receive_decider_frame(safe_ws):
    """The next decoded dict frame; None on a malformed/non-dict message
    to skip, or ``_FRAME_DISCONNECTED`` when the socket dropped."""
    # Starlette raises RuntimeError ("WebSocket is not connected...")
    # on a client disconnect during receive_text(); treat it the same
    # as WebSocketDisconnect.
    try:
        raw = await safe_ws.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return _FRAME_DISCONNECTED
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(msg, dict):
        return None
    return msg


async def _dispatch_decider_frame(
    app, registry, safe_ws, frame, workspace, user, decider_id
) -> bool:
    """Dispatch one frame; False when the client fell behind (the caller
    breaks). Any other handler error is logged and swallowed — a single
    bad verdict or transient send error must not tear down the whole
    connection (and every other in-flight prompt on it)."""
    mtype = frame.get("type")
    try:
        await _dispatch_decider_message(
            app,
            registry,
            safe_ws,
            frame,
            workspace,
            user["id"],
            decider_id,
        )
    except SlowClientError:
        # Outbound queue full -- the client can't keep up. Drop it
        # (matches the main /ws handler's slow-client handling).
        return False
    except Exception:
        logger.exception("consent decider: error handling %r message", mtype)
    return True


async def _decider_receive_loop(
    app,
    registry,
    safe_ws,
    workspace,
    user: dict,
    decider_id: str,
    session_id: str | None = None,
) -> None:
    """Receive + dispatch decider frames until the socket drops or the
    client falls behind.

    Every frame is session activity (#3151) — a decider socket
    authenticated with a user session JWT, so its traffic keeps that
    session's idle clock alive exactly like the main /ws handler's
    frames (stamped by stable session id, surviving rekeying)."""
    while True:
        frame = await _receive_decider_frame(safe_ws)
        if frame is _FRAME_DISCONNECTED:
            break
        if frame is None:
            continue
        if not await _stamp_and_dispatch(
            app,
            registry,
            safe_ws,
            frame,
            workspace,
            user,
            decider_id,
            session_id,
        ):
            break


async def _stamp_and_dispatch(
    app,
    registry,
    safe_ws,
    frame,
    workspace,
    user,
    decider_id,
    session_id,
) -> bool:
    """Stamp the session's idle clock for the frame, then dispatch it.

    Returns the dispatch outcome (False = client fell behind, the
    receive loop breaks)."""
    if session_id is not None:
        await app.state.auth.record_ws_session_activity(session_id)
    return await _dispatch_decider_frame(
        app, registry, safe_ws, frame, workspace, user, decider_id
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

    authed = await _decider_authenticate(websocket, app, _hs_mark)
    if authed is None:
        return
    user, jti = authed
    workspace = websocket.query_params.get("workspace")
    if workspace is None:
        # #2976: consent is strictly a workspace concern -- there is no
        # deploy-wide decider flavor, so the workspace param is required.
        await _refuse(
            websocket,
            4003,
            "workspace query parameter is required",
            user.get("email"),
        )
        return

    if await _refuse_invalid_handshake(
        websocket, app, workspace, user, hs_mark=_hs_mark
    ):
        return
    await websocket.accept(subprotocol=ws_echo_subprotocol(websocket))
    _hs_mark("accept")
    _log_handshake_timing(_hs_t0, _hs_marks)
    safe_ws = SafeWebSocket(websocket)
    safe_ws.start_sender()
    registry = app.state.consent_deciders
    decider_id = str(uuid.uuid4())
    email = user.get("email")
    try:
        registry.register(
            decider_id, workspace, email, safe_ws, jti=jti, user_id=user["id"]
        )
        # Register BEFORE reading the snapshot: a hold created between the two
        # is then delivered twice (once live via fanout, once from the
        # snapshot). That duplicate is benign -- the second verdict no-ops
        # (resolve pops first) -- whereas snapshot-then-register would MISS
        # holds created in the gap (silent fail-close). Tolerate the duplicate
        # to never miss.
        await _replay_decider_snapshot(app, safe_ws, workspace)
        await _decider_receive_loop(
            app,
            registry,
            safe_ws,
            workspace,
            user,
            decider_id,
            session_id=await app.state.model.sessions.get_session_id(jti),
        )
    finally:
        # Connection gone (clean disconnect, error, or crash) -> drop the
        # registration so the workspace reverts to static (#2308).
        registry.deregister(decider_id)
        await safe_ws.stop_sender()


async def _replay_decider_snapshot(app, safe_ws, workspace) -> None:
    """Replay the pending holds and the in-effect rules snapshot to a
    just-registered decider (fan-in, #2244; rules slice A, #2335)."""
    for frame in await app.state.consent_coordinator.snapshot(workspace):
        safe_ws.send_json(frame)
    # The in-effect rules snapshot (#2335 slice A): lets a decider joining
    # mid-flight see what's currently allowed/denied, not just holds.
    rules = await app.state.consent_coordinator.rules_frame(workspace)
    if rules is not None:
        safe_ws.send_json(rules)


async def _handle_pause(app, safe_ws, msg, workspace) -> None:
    """Pause consent prompting for the decider's workspace (#2332)."""
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


async def _handle_unpause(app, safe_ws, workspace) -> None:
    """Clear an active consent pause for the decider's workspace (#2332)."""
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
