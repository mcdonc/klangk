"""WebSocket command dispatch: handle_websocket and command tables."""

import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from .. import auth
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


async def ws_authenticate(websocket: WebSocket, app):
    """Validate the socket's token; close and return None on failure."""
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return None

    result = await app.state.auth.get_user_from_token(token)
    if result is auth.Auth.TOKEN_EXPIRED:
        await websocket.close(code=4002, reason="Token expired")
        return None
    if result is None:
        await websocket.close(code=4001, reason="Invalid token")
        return None
    return result


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
    table first, then state-command table, else an error frame."""
    while True:
        raw = await safe_ws.receive_text()
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
    user = await ws_authenticate(websocket, app)
    if user is None:
        return

    await websocket.accept()
    safe_ws = SafeWebSocket(websocket)
    safe_ws.start_sender()
    conn = Connection(safe_ws, user, app)
    app.state.sockets.connections[safe_ws] = conn
    # Everything from here on is inside the try so a failure in the
    # connect-time work below still runs the ``finally`` cleanup — the
    # snapshot (#1714) awaits DB queries, and a raise there used to
    # leak the sender task and skip conn.cleanup().
    try:
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
