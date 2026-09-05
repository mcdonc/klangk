"""WebSocket command dispatch: handle_websocket and command tables."""

import json
import logging

from fastapi import WebSocket, WebSocketDisconnect
from jose import ExpiredSignatureError, JWTError

from .safe_websocket import SafeWebSocket, SlowClientError
from .support import send_error, log_ws_msg
from .connection import Connection

logger = logging.getLogger(__name__)


# --- WebSocket command dispatch tables -------------------------------
#
# `handle_websocket` routes each incoming command by looking it up in
# these catalogs instead of walking a long if/elif chain. Adding a new
# command is a one-line edit to the relevant table below.

# Commands dispatched to a Connection method. The boolean is True when
# the handler takes the message dict, False when it takes no arguments.
WS_CONNECTION_COMMANDS: dict[str, tuple[str, bool]] = {
    "workspace_connect": ("handle_workspace_connect", True),
    "workspace_disconnect": ("handle_workspace_disconnect", False),
    "ui_ready": ("handle_ui_ready", False),
    "set_handle": ("handle_set_handle", True),
    "terminal_start": ("handle_terminal_start", True),
    "browser_reattach": ("handle_browser_reattach", True),
    "terminal_input": ("handle_terminal_input", True),
    "terminal_resize": ("handle_terminal_resize", True),
    "terminal_stop": ("handle_terminal_stop", False),
    "terminal_new_window": ("handle_terminal_new_window", True),
    "terminal_select_window": ("handle_terminal_select_window", True),
    "terminal_close_window": ("handle_terminal_close_window", True),
    "terminal_rename_window": ("handle_terminal_rename_window", True),
    "terminal_list_windows": ("handle_terminal_list_windows", False),
    "share_window": ("handle_share_window", True),
    "unshare_window": ("handle_unshare_window", True),
    "create_shared_terminal": ("handle_create_shared_terminal", True),
    "join_shared_terminal": ("handle_join_shared_terminal", True),
    "delete_shared_terminal": ("handle_delete_shared_terminal", True),
    "list_shared_terminals": ("handle_list_shared_terminals", False),
    "restart_container": ("handle_restart_container", False),
    "exec_start": ("handle_exec_start", True),
    "exec_input": ("handle_exec_input", True),
    "exec_close_stdin": ("handle_exec_close_stdin", False),
    "exec_stop": ("handle_exec_stop", False),
    "ssh_agent_start": ("handle_ssh_agent_start", False),
    "ssh_agent_data": ("handle_ssh_agent_data", True),
    "ssh_agent_stop": ("handle_ssh_agent_stop", False),
    "heartbeat": ("handle_heartbeat", False),
}

# Commands dispatched to the shared `state` object instead of a
# Connection. These are synchronous and take (msg, sender).
WS_STATE_COMMANDS: dict[str, str] = {
    "browser_response": "handle_browser_response",
    "browser_chunk": "handle_browser_chunk",
    "subscribe_health_heartbeat": "handle_subscribe_health_heartbeat",
}


async def ws_authenticate(
    websocket: WebSocket, app
) -> tuple[dict, str, float] | None:
    """Validate the socket's token; close and return None on failure.

    Success returns ``(user, jti, exp)`` — the token's JTI rides along
    so per-frame traffic can stamp the session's ``last_seen_at``
    (#3151) and so the connection can be targeted for closing when
    that token is later hard-revoked (#3152); ``exp`` (Unix epoch)
    lets the connection schedule its own close when the token expires.
    With session binding armed (#3194), the connect is also checked
    against the workstation the session was established from — a
    token replayed from a different machine closes 4001 and its
    session is revoked. A DPoP-bound token must also prove
    possession (#3218).
    """
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return None

    a = app.state.auth
    payload = await _decode_socket_token(websocket, a, token)
    if payload is None:
        return None
    if not await _dpop_gate(websocket, app, token, payload):
        return None
    workstation = ws_workstation(websocket, app)
    user = await _user_or_close(websocket, a, payload, workstation)
    if user is None:
        return None
    return user, payload.get("jti"), payload.get("exp")


async def _decode_socket_token(websocket: WebSocket, a, token: str):
    """The token payload, or None after closing the socket on failure."""
    try:
        return a.decode_token(token)
    except ExpiredSignatureError:
        await websocket.close(code=4002, reason="Token expired")
        return None
    except JWTError:
        await websocket.close(code=4001, reason="Invalid token")
        return None


async def _dpop_gate(
    websocket: WebSocket, app, token: str, payload: dict
) -> bool:
    """A DPoP-bound token must prove possession at connect (#3218).

    The browser client appends a one-shot ``dpop`` query parameter —
    the same compact proof the HTTP ``DPoP`` header carries (its jti
    is single-use and its ath binds it to this exact token, so its
    presence in the URL leaks nothing reusable). Unbound tokens
    (CLI/TUI, pre-#3218 clients) pass untouched.
    """
    reason = app.state.auth.check_dpop(
        websocket.query_params.get("dpop"),
        "GET",
        websocket.url.path,
        token,
        payload,
    )
    if reason is None:
        return True
    await websocket.close(code=4001, reason="Invalid DPoP proof")
    return False


def ws_workstation(websocket: WebSocket, app) -> tuple[str | None, str | None]:
    """The ``(ip, user_agent)`` a WebSocket connect presents (#3194).

    Same resolver as HTTP requests (:meth:`Util.workstation`) — the
    handshake headers and peer address, proxy-trust-aware — shared
    with the consent-decider gate. Minimal test doubles may carry no
    ``client``; that reads as an unknown peer (fail-open, like every
    other unresolvable workstation).
    """
    client = getattr(websocket, "client", None)
    host = client.host if client else None
    headers = getattr(websocket, "headers", None)
    return app.state.util.workstation(headers, host)


async def _user_or_close(
    websocket: WebSocket, a, payload, workstation
) -> dict | None:
    """The authenticated user for a main-WS connect, or None after
    closing the socket.

    Three refusal arms: an unknown user (4001), a session under the
    must_change_password flag (4004, #3172 — the client must change
    the password first; 4004, not 4003, because the decider socket
    already uses 4003 for authz refusals, and duplicate close codes
    are indistinguishable to clients, #3172 review), and a token
    presented from a different workstation (4001, #3194).
    """
    user = await a._user_from_valid_payload(payload, workstation)
    if user is None:
        await websocket.close(code=4001, reason="Invalid token")
        return None
    if user.get("must_change_password"):
        await websocket.close(code=4004, reason="Password change required")
        return None
    return user


# Exceptions raised by a *handler* that mean the connection itself is
# dead (client gone, outbound queue full, starlette's "not connected"):
# they must end the session — ``_run_websocket_session`` maps each to
# its log line. Any other handler exception is a frame-level failure
# and is answered with an error frame instead (#3071): a buggy or
# malicious client must not be able to tear down its own session with
# a malformed frame.
WS_SESSION_ERRORS = (WebSocketDisconnect, SlowClientError, RuntimeError)


async def _dispatch_connection_command(conn, cmd, msg) -> bool:
    """Dispatch a Connection-table command; False when *cmd* is not one."""
    entry = WS_CONNECTION_COMMANDS.get(cmd)
    if entry is None:
        return False
    method_name, takes_msg = entry
    method = getattr(conn, method_name)
    if takes_msg:
        await method(msg)
    else:
        await method()
    return True


async def _dispatch_frame(conn, safe_ws, msg, app) -> None:
    """Dispatch one decoded frame, guarding handler failures per-frame.

    Connection-table first, then state-command table, else an error
    frame. A handler exception is logged and answered with an error
    frame so the loop keeps the session alive (#3071) — except the
    connection-level failures (``WS_SESSION_ERRORS``), which must keep
    propagating to end the session.
    """
    cmd = msg.get("cmd")
    try:
        if await _dispatch_connection_command(conn, cmd, msg):
            return
        state_method = WS_STATE_COMMANDS.get(cmd)
        if state_method is not None:
            getattr(app.state.sockets, state_method)(msg, safe_ws)
        else:
            send_error(safe_ws, f"Unknown command: {cmd}")
    except WS_SESSION_ERRORS:
        raise
    except Exception:
        logger.exception("Error handling WS command %r", cmd)
        send_error(safe_ws, f"Error handling command: {cmd}")


async def dispatch_ws_loop(conn, safe_ws, user: dict, app) -> None:
    """Receive/dispatch frames until the socket drops. Connection-command
    table first, then state-command table, else an error frame.

    Every inbound frame is session activity (#3151): it bumps the
    connection's in-memory idle clock (the sweeper's signal) and —
    throttled — stamps the session row's ``last_seen_at`` so the refresh
    seam sees it too. An open browser's 60-second heartbeat keeps a
    watched terminal alive while a closed one times out.
    """
    while True:
        raw = await safe_ws.receive_text()
        await conn.mark_frame_activity()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            send_error(safe_ws, "Invalid JSON")
            continue
        # A JSON frame must be an object: a list/scalar/None payload has
        # no "cmd", and ``msg.get`` on it would AttributeError (#3071).
        if not isinstance(msg, dict):
            send_error(safe_ws, "Invalid frame: expected a JSON object")
            continue

        log_ws_msg("RECV", msg, user)

        await _dispatch_frame(conn, safe_ws, msg, app)


async def _send_connect_snapshots(safe_ws, app) -> None:
    """Replay the connect-time snapshots to a just-connected socket."""
    # Replay current health of every health-checked workspace the
    # user can open so a pure-WS consumer (e.g. ``klangk monitor``)
    # sees steady-state status immediately instead of being blind
    # until the next transition (#1175 item 1). Scoped to the
    # user's memberships (#1714).
    await app.state.sockets.send_service_health_snapshot(safe_ws)
    # #2661: replay any pending server stop/recycle schedule so a
    # just-connected client can show the countdown immediately instead
    # of waiting for the scheduler's next periodic broadcast. Guarded:
    # minimal test apps may not wire the scheduler.
    server_scheduler = getattr(app.state, "server_scheduler", None)
    if server_scheduler is not None:
        await server_scheduler.send_snapshot_to(safe_ws)


async def _run_websocket_session(conn, safe_ws, user: dict, app) -> None:
    """Dispatch frames until the socket drops, mapping the known
    disconnect/slow-client failures to their log lines."""
    try:
        await _send_connect_snapshots(safe_ws, app)
        await dispatch_ws_loop(conn, safe_ws, user, app)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for user %s", user["email"])
    except RuntimeError as e:
        # Starlette raises RuntimeError("WebSocket is not connected...")
        # when the client disconnects before or during receive_text().
        logger.info("WebSocket disconnected for user %s: %s", user["email"], e)
    except SlowClientError as e:
        # Carry the reason: "outbound queue full" is a genuinely slow
        # client, "sender stopped" is this connection already tearing down
        # (#2623 — the distinction was invisible in CI logs before).
        logger.warning(
            "Slow client dropped for user %s (%s)", user["email"], e
        )
    except Exception as e:
        logger.exception("WebSocket error: %s", e)


async def handle_websocket(websocket: WebSocket, app) -> None:
    """Main WebSocket handler."""
    # Authenticate via query param
    authed = await ws_authenticate(websocket, app)
    if authed is None:
        return
    user, jti, token_exp = authed

    await websocket.accept()
    safe_ws = SafeWebSocket(websocket)
    safe_ws.start_sender()
    conn = Connection(safe_ws, user, app, jti=jti, token_exp=token_exp)
    app.state.sockets.connections[safe_ws] = conn
    conn.schedule_token_expiry()
    # Everything from here on is inside the try so a failure in the
    # connect-time work below still runs the ``finally`` cleanup — the
    # snapshot (#1714) awaits DB queries, and a raise there used to
    # leak the sender task and skip conn.cleanup(). That includes the
    # stable session-id resolution (#3151): the row's JTI is rekeyed
    # on every token refresh, so frame stamps must go through a key
    # that survives the rotation (a missing row — a pre-#2585 token —
    # leaves None and the connection simply doesn't stamp; fail-open).
    # Registration deliberately happens BEFORE the SELECT: a broadcast
    # (the SIGHUP draining fanout) racing the connect must not miss a
    # just-accepted socket that still eats the 1012 close.
    try:
        conn.session_id = await app.state.model.sessions.get_session_id(jti)
        await _run_websocket_session(conn, safe_ws, user, app)
    finally:
        await safe_ws.stop_sender()
        try:
            await conn.cleanup()
        except Exception:  # noqa: BLE001
            # #3069: cleanup failing must not skip the registry pop below
            # (a stale SafeWebSocket->Connection entry would leak until the
            # next fanout pruned it). Cleanup itself guards each teardown
            # step, so the session bookkeeping still ran; whatever raised
            # here is logged and the teardown continues.
            logger.exception("Connection cleanup failed")
        # Container is intentionally left running — idle timeout will clean it up.
        # This allows instant reconnection when navigating back to the workspace.
        app.state.sockets.connections.pop(safe_ws, None)
