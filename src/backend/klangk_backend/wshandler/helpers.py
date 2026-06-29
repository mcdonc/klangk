"""Module-level helper functions for the wshandler package."""

import logging

from .. import agent, container, model
from .safe_websocket import SafeWebSocket
from ._constants import _log_ws_msg
from .session import state

logger = logging.getLogger(__name__)


async def _get_presence_list(workspace_id: str) -> list[dict]:
    """Return deduplicated list of users connected to a workspace."""
    session = state.get_session(workspace_id)
    if not session:
        return []
    seen: set[str] = set()
    users: list[dict] = []
    for sock in session.subscribers:
        conn = state.connections.get(sock)
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

    if agent.is_running(workspace_id):
        agent_user = await model.get_agent_user()
        users.append(
            {
                "user_id": model.AGENT_USER_ID,
                "user_email": agent_user["email"],
                "user_handle": agent_user.get("handle", ""),
            }
        )
    return users


def _get_shared_terminals(ws_session) -> list[dict]:
    """Collect all shared windows across all users in a workspace."""
    # Build viewer map: (owner_user_id, window_id) -> [{user_id, email}]
    viewer_map: dict[tuple[str, str], list[dict]] = {}
    for sock in ws_session.subscribers:
        conn = state.connections.get(sock)
        if not conn or not conn._viewing_shared:
            continue
        key = (
            conn._viewing_shared["user_id"],
            conn._viewing_shared["window_id"],
        )
        viewer_map.setdefault(key, []).append(
            {"user_id": conn.user["id"], "email": conn.user.get("email", "")}
        )

    terminals = []
    for user_id, windows in ws_session.terminal_windows.items():
        # Look up the user's handle from any active connection
        handle = None
        for sock in ws_session.subscribers:
            conn = state.connections.get(sock)
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
                    }
                )
    return terminals


async def reset_workspace_state(workspace_id: str) -> None:
    """Thin wrapper for backward compatibility with external callers."""
    await state.reset_workspace(workspace_id)


async def refresh_user_handle(user_id: str, new_handle: str) -> None:
    """Update the cached handle on all active connections for a user,
    re-broadcast presence, and post a system chat message to each
    affected workspace."""
    old_handle: str | None = None
    affected_workspaces: set[str] = set()
    user_email: str = ""
    for conn in list(state.connections.values()):
        if conn.user["id"] == user_id:
            if old_handle is None:
                old_handle = conn.user.get("handle", "")
                user_email = conn.user.get("email", "")
            conn.user["handle"] = new_handle
            if conn.workspace_id:
                affected_workspaces.add(conn.workspace_id)
    for ws_id in affected_workspaces:
        session = state.get_session(ws_id)
        if session:
            presence = await _get_presence_list(ws_id)
            session.broadcast({"type": "presence_list", "users": presence})
            sys_msg = await model.add_chat_message(
                ws_id,
                user_id,
                user_email,
                f"{old_handle} is now known as {new_handle}",
                message_type=model.MSG_SYSTEM,
            )
            session.broadcast({"type": "chat_message", **sys_msg})


def _send_event(
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


def _format_idle_timeout(seconds: int | float) -> str:
    """Format an idle timeout as a human-readable suffix."""
    mins = seconds / 60
    if mins == int(mins):
        return f" — idle timeout: {int(mins)}m"
    return f" — idle timeout: {mins:.1f}m"


def _format_container_info(workspace_id: str, ports: list) -> tuple[str, str]:
    """Return (container_name, ports_str) for status messages."""
    name = f"klangk-{container.INSTANCE_ID}-{workspace_id[:12]}"
    ports_str = f" (ports {','.join(str(p) for p in ports)})" if ports else ""
    return name, ports_str


def send_error(sock: SafeWebSocket, message: str) -> None:
    msg = {"type": "error", "message": message}
    _log_ws_msg("SEND", msg)
    sock.send_json(msg)
