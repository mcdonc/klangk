"""Shared constants and helper functions used across the wshandler package.

Merges the former ``constants`` and ``helpers`` submodules (#2908).
This module is the dependency root of the wshandler package: apart from
``..container`` it has no runtime intra-package imports — ``SafeWebSocket``
and ``WebSocketState`` appear only in annotations (TYPE_CHECKING), and
``log_ws_msg`` lives here (with the constants it reads) so ``session``
and ``safe_websocket`` can import it without a cycle.  Any sibling
module can import from here without creating a circular dependency.
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from ..container import workspace_container_name, workspace_name_slug

if TYPE_CHECKING:
    from .safe_websocket import SafeWebSocket
    from .session import WebSocketState

logger = logging.getLogger(__name__)

# Plain debug flag (not a KlangkSettings field): read straight from the
# environment, no file:/cmd: resolution (#1516).
WS_DEBUG = bool(os.environ.get("KLANGKD_WEBSOCKET_DEBUG"))

# Max size for terminal/exec input data (base64-decoded bytes).
# Matches uvicorn's --ws-max-size (16 MB) so the app-level cap isn't
# stricter than the transport cap — see #1257.
MAX_INPUT_SIZE = 16777216

# Max outbound messages before we declare the client too slow and close.
SEND_QUEUE_SIZE = 256


def log_ws_msg(direction: str, msg: dict, user: dict | None = None) -> None:
    """Log a WebSocket message for debugging (KLANGKD_WEBSOCKET_DEBUG=1)."""
    if not WS_DEBUG:
        return
    msg_type = msg.get("type") or msg.get("cmd") or "?"
    # Truncate terminal_output/terminal_input data to avoid log spam
    if msg_type in ("terminal_output", "terminal_input"):
        data = msg.get("data", "")
        preview = repr(data[:80]) + ("..." if len(data) > 80 else "")
        who = f" [{user['email']}]" if user else ""
        logger.debug("WS %s%s: %s data=%s", direction, who, msg_type, preview)
    else:
        who = f" [{user['email']}]" if user else ""
        logger.debug("WS %s%s: %s", direction, who, json.dumps(msg)[:200])


# ---------------------------------------------------------------------------
# Helpers (former ``helpers`` submodule).
# ---------------------------------------------------------------------------


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
    slug = workspace_name_slug(workspace_name)
    name = workspace_container_name(instance_id, workspace_id, slug)
    ports_str = f" (ports {','.join(str(p) for p in ports)})" if ports else ""
    return name, ports_str


def send_error(
    sock: SafeWebSocket, message: str, code: str | None = None
) -> None:
    """Send an error frame; *code* adds a machine-readable kind.

    The optional *code* (e.g. ``"capacity"``, #2525; ``"forbidden"`` /
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
