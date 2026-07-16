"""Module-level helper functions for the wshandler package."""

import logging

from .. import model
from .safe_websocket import SafeWebSocket
from .constants import log_ws_msg
from .session import WebSocketState

logger = logging.getLogger(__name__)


async def get_presence_list(
    workspace_id: str, sockets: WebSocketState
) -> list[dict]:
    """Return deduplicated list of users connected to a workspace."""
    session = sockets.get_session(workspace_id)
    if not session:
        return []
    seen: set[str] = set()
    users: list[dict] = []
    for sock in session.subscribers:
        conn = sockets.connections.get(sock)
        if conn and conn.user["id"] not in seen:
            seen.add(conn.user["id"])
            users.append(
                {
                    "user_id": conn.user["id"],
                    "user_email": conn.user["email"],
                    "user_handle": conn.user.get("handle", ""),
                }
            )
    # Include agent only if its RPC process is alive in this workspace.

    if sockets.app_state.agents.is_running(workspace_id):
        agent_user = await sockets.app_state.model.users.get_agent_user()
        users.append(
            {
                "user_id": model.AGENT_USER_ID,
                "user_email": agent_user["email"],
                "user_handle": agent_user.get("handle", ""),
            }
        )
    return users


def get_shared_terminals(ws_session, sockets: WebSocketState) -> list[dict]:
    """Collect all shared windows across all users in a workspace."""
    # Build viewer map: (owner_user_id, window_id) -> [{user_id, email}]
    viewer_map: dict[tuple[str, str], list[dict]] = {}
    for sock in ws_session.subscribers:
        conn = sockets.connections.get(sock)
        if not conn or not conn.viewing_shared:
            continue
        key = (
            conn.viewing_shared["user_id"],
            conn.viewing_shared["window_id"],
        )
        viewer_map.setdefault(key, []).append(
            {"user_id": conn.user["id"], "email": conn.user.get("email", "")}
        )

    terminals = []
    for user_id, windows in ws_session.terminal_windows.items():
        # Look up the user's handle from any active connection. The
        # agent (AGENT_USER_ID) has no WS connection, so its handle is
        # the cached ``agent_handle`` populated by ``_sync_service_windows``
        # -- the agent is always attributable, never "offline" (#1133).
        handle = None
        if user_id == model.AGENT_USER_ID:
            handle = ws_session.agent_handle
        else:
            for sock in ws_session.subscribers:
                conn = sockets.connections.get(sock)
                if conn and conn.user.get("id") == user_id:
                    handle = conn.user.get("handle")
                    break
        if not handle:
            continue
        for w in windows:
            if w.get("shared"):
                wid = w.get("id", "")
                viewers = viewer_map.get((user_id, wid), [])
                terminals.append(
                    {
                        "user_id": user_id,
                        "handle": handle,
                        "window_name": w["name"],
                        "window_id": wid,
                        "viewers": viewers,
                        # The agent's shared windows live in the standalone
                        # ``service`` tmux session (#1158); flag them so the
                        # UI can present the service tab distinctly (#1159).
                        "is_service": user_id == model.AGENT_USER_ID,
                    }
                )
    return terminals


async def reset_workspace_state(
    sockets: WebSocketState, workspace_id: str
) -> None:
    """Thin wrapper for backward compatibility with external callers."""
    await sockets.reset_workspace(workspace_id)


async def disconnect_all_websockets(sockets: WebSocketState) -> None:
    """Drop every WebSocket connection and clear all session state.

    Used by the SIGHUP runtime-restart path (see
    ``main.Lifecycle.runtime_shutdown``).
    Connected clients are closed with code 1012 so they reconnect and
    rebuild state against the freshly-started containers.
    """
    await sockets.disconnect_all()


async def refresh_user_handle(
    sockets: WebSocketState, user_id: str, new_handle: str
) -> None:
    """Update the cached handle on all active connections for a user,
    re-broadcast presence, and post a system chat message to each
    affected workspace."""
    old_handle: str | None = None
    affected_workspaces: set[str] = set()
    user_email: str = ""
    for conn in list(sockets.connections.values()):
        if conn.user["id"] == user_id:
            if old_handle is None:
                old_handle = conn.user.get("handle", "")
                user_email = conn.user.get("email", "")
            conn.user["handle"] = new_handle
            if conn.workspace_id:
                affected_workspaces.add(conn.workspace_id)
    for ws_id in affected_workspaces:
        session = sockets.get_session(ws_id)
        if session:
            presence = await get_presence_list(ws_id, sockets)
            session.broadcast({"type": "presence_list", "users": presence})
            sys_msg = await model.add_chat_message(
                ws_id,
                user_id,
                user_email,
                f"{old_handle} is now known as {new_handle}",
                message_type=model.MSG_SYSTEM,
            )
            session.broadcast({"type": "chat_message", **sys_msg})


def send_event(
    sock: SafeWebSocket, name: str, reason: str | None = None
) -> None:
    """Send a CUSTOM event (container_ready, container_stopped, etc.)."""
    value = {"reason": reason} if reason else {}
    sock.send_json(
        {
            "type": "event",
            "event": {"type": "CUSTOM", "name": name, "value": value},
        }
    )


def format_idle_timeout(seconds: int | float) -> str:
    """Format an idle timeout as a human-readable suffix."""
    mins = seconds / 60
    if mins == int(mins):
        return f" — idle timeout: {int(mins)}m"
    return f" — idle timeout: {mins:.1f}m"


def format_container_info(
    workspace_id: str, ports: list, instance_id: str
) -> tuple[str, str]:
    """Return (container_name, ports_str) for status messages."""
    name = f"klangk-{instance_id}-{workspace_id[:12]}"
    ports_str = f" (ports {','.join(str(p) for p in ports)})" if ports else ""
    return name, ports_str


def send_error(sock: SafeWebSocket, message: str) -> None:
    msg = {"type": "error", "message": message}
    log_ws_msg("SEND", msg)
    sock.send_json(msg)
