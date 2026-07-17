"""WebSocket command dispatch: handle_websocket and command tables."""

import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from .. import auth
from .safe_websocket import SafeWebSocket, SlowClientError
from .helpers import send_error, log_ws_msg
from .connection import Connection

logger = logging.getLogger(__name__)


# --- WebSocket command dispatch tables -------------------------------
#
# `handle_websocket` routes each incoming command by looking it up in
# these catalogs instead of walking a long if/elif chain. Adding a new
# command is a one-line edit to the relevant table below.

# Commands dispatched to a Connection method. The boolean is True when
# the handler takes the message dict, False when it takes no arguments.
_WS_CONNECTION_COMMANDS: dict[str, tuple[str, bool]] = {
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
    "shutdown_container": ("handle_shutdown_container", False),
    "exec_start": ("handle_exec_start", True),
    "exec_input": ("handle_exec_input", True),
    "exec_close_stdin": ("handle_exec_close_stdin", False),
    "exec_stop": ("handle_exec_stop", False),
    "ssh_agent_start": ("handle_ssh_agent_start", False),
    "ssh_agent_data": ("handle_ssh_agent_data", True),
    "ssh_agent_stop": ("handle_ssh_agent_stop", False),
    "heartbeat": ("handle_heartbeat", False),
    "chat_send": ("handle_chat_send", True),
    "chat_delete": ("handle_chat_delete", True),
    "chat_load_more": ("handle_chat_load_more", True),
    "chat_agent_abort": ("handle_chat_agent_abort", False),
}

# Commands dispatched to the shared `state` object instead of a
# Connection. These are synchronous and take (msg, sender).
_WS_STATE_COMMANDS: dict[str, str] = {
    "browser_response": "handle_browser_response",
    "browser_chunk": "handle_browser_chunk",
    "subscribe_health_heartbeat": "handle_subscribe_health_heartbeat",
}


async def handle_websocket(websocket: WebSocket, app) -> None:
    """Main WebSocket handler."""
    # Authenticate via query param
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

    await websocket.accept()
    safe_ws = SafeWebSocket(websocket)
    safe_ws.start_sender()
    conn = Connection(safe_ws, user, app)
    app.state.sockets.connections[safe_ws] = conn
    # Replay current health of every health-checked workspace so a
    # pure-WS consumer (e.g. ``klangk monitor``) sees steady-state
    # status immediately instead of being blind until the next
    # transition (#1175 item 1).
    app.state.sockets.send_service_health_snapshot(safe_ws)

    try:
        while True:
            raw = await safe_ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                send_error(safe_ws, "Invalid JSON")
                continue

            log_ws_msg("RECV", msg, user)

            cmd = msg.get("cmd")
            entry = _WS_CONNECTION_COMMANDS.get(cmd)
            if entry is not None:
                method_name, takes_msg = entry
                method = getattr(conn, method_name)
                if takes_msg:
                    await method(msg)
                else:
                    await method()
            else:
                state_method = _WS_STATE_COMMANDS.get(cmd)
                if state_method is not None:
                    getattr(app.state.sockets, state_method)(msg, safe_ws)
                else:
                    send_error(safe_ws, f"Unknown command: {cmd}")

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for user %s", user["email"])
    except RuntimeError as e:
        # Starlette raises RuntimeError("WebSocket is not connected...")
        # when the client disconnects before or during receive_text().
        logger.info("WebSocket disconnected for user %s: %s", user["email"], e)
    except SlowClientError:
        logger.warning("Slow client dropped for user %s", user["email"])
    except Exception as e:
        logger.exception("WebSocket error: %s", e)
    finally:
        await safe_ws.stop_sender()
        await conn.cleanup()
        # Container is intentionally left running — idle timeout will clean it up.
        # This allows instant reconnection when navigating back to the workspace.
        app.state.sockets.connections.pop(safe_ws, None)
