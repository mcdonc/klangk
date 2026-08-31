"""Module-level helper functions for the wshandler package."""

import logging

from ..container import _workspace_container_name, _workspace_name_slug
from .safe_websocket import SafeWebSocket
from .constants import log_ws_msg
from .session import (
    WebSocketState,
    get_shared_terminals as get_shared_terminals,
)

logger = logging.getLogger(__name__)


async def reset_workspace_state(
    sockets: WebSocketState,
    workspace_id: str,
    expected_container_id: str | None = None,
) -> None:
    """Thin wrapper for backward compatibility with external callers.

    *expected_container_id* (#331) is the dead container id threading
    through to ``remove_state``'s re-bind guard: a racing user-driven
    start that re-bound the workspace keeps its fresh registry state.
    """
    await sockets.reset_workspace(
        workspace_id, expected_container_id=expected_container_id
    )


async def disconnect_all_websockets(sockets: WebSocketState) -> None:
    """Drop every WebSocket connection and clear all session state.

    Used by the SIGHUP runtime-restart path (see
    ``main.Lifecycle.runtime_shutdown``).
    Connected clients are closed with code 1012 so they reconnect and
    rebuild state against the freshly-started containers.
    """
    await sockets.disconnect_all()


async def disconnect_user(
    sockets: WebSocketState,
    user_id: str,
    *,
    code: int = 4001,
    reason: str = "",
) -> int:
    """Close every live connection for *user_id* (#2588).

    Used when an account is disabled: 4001 makes the client log out
    rather than reconnect-loop. Thin delegation to
    ``WebSocketState.disconnect_user``.
    """
    return await sockets.disconnect_user(user_id, code=code, reason=reason)


async def refresh_user_handle(
    sockets: WebSocketState, user_id: str, new_handle: str
) -> None:
    """Update the cached handle on all active connections for a user.

    The per-connection ``user`` dict carries the handle read at
    connect time; a rename must propagate to every live connection so
    subsequent frames (terminal window labels, shared-terminal
    attribution) show the new handle.
    """
    for conn in list(sockets.connections.values()):
        if conn.user["id"] == user_id:
            conn.user["handle"] = new_handle


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
    workspace_id: str, ports: list, instance_id: str, workspace_name: str = ""
) -> tuple[str, str]:
    """Return (container_name, ports_str) for status messages.

    The name mirrors the real container name (slugified workspace name +
    id[:8], #2286) so an operator can ``podman ps | grep`` the name shown here
    and find the container.
    """
    slug = _workspace_name_slug(workspace_name)
    name = _workspace_container_name(instance_id, workspace_id, slug)
    ports_str = f" (ports {','.join(str(p) for p in ports)})" if ports else ""
    return name, ports_str


def send_error(
    sock: SafeWebSocket, message: str, code: str | None = None
) -> None:
    """Send an error frame; *code* adds a machine-readable kind.

    The optional ``code`` (e.g. ``"capacity"``, #2525; ``"forbidden"`` /
    ``"not_found"``, #2891) lets clients tell a *class* of failure apart
    from other start errors without parsing the message text — the WS
    counterpart of the API's 503. Omitted for legacy callers; unknown
    codes are ignorable by old clients.
    """
    msg = {"type": "error", "message": message}
    if code is not None:
        msg["code"] = code
    log_ws_msg("SEND", msg)
    sock.send_json(msg)
