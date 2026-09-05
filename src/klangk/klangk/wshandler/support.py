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
    from .connection import Connection
    from .safe_websocket import SafeWebSocket
    from .session import WebSocketState, WorkspaceSession

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

# The WS auth subprotocol name (#3201). Browser WebSocket clients cannot
# set request headers, so the session JWT rides the handshake as the
# ``Sec-WebSocket-Protocol`` header instead of a ``?token=`` query
# param: query strings land in reverse-proxy and server access logs,
# headers do not. The client offers ``["bearer", <jwt>]``; the server
# extracts the non-"bearer" entry and echoes ``bearer`` on accept.
WS_AUTH_SUBPROTOCOL = "bearer"


def ws_bearer_token(websocket) -> str | None:
    """The JWT from the handshake's ``Sec-WebSocket-Protocol`` header.

    The client offers ``bearer, <jwt>`` (comma-separated when multiple
    subprotocols are requested). The **last** entry that is not the
    ``bearer`` marker is the token: a future client negotiating an
    application subprotocol ahead of the pair (``json, bearer, <jwt>``)
    still parses correctly, and a lone ``[<jwt>]`` offer works too.
    ``None`` when no token was offered.
    """
    offered = websocket.headers.get("sec-websocket-protocol", "")
    entries = [e.strip() for e in offered.split(",")]
    for entry in reversed(entries):
        if entry and entry != WS_AUTH_SUBPROTOCOL:
            return entry
    return None


def ws_echo_subprotocol(websocket) -> str | None:
    """The subprotocol to echo on accept, or None (#3201).

    Per RFC 6455 the server may only select a protocol the client
    offered, and a browser fails the handshake when the selection is
    absent from its list — so echo ``bearer`` only when it was offered.
    """
    offered = websocket.headers.get("sec-websocket-protocol", "")
    entries = {e.strip() for e in offered.split(",")}
    return WS_AUTH_SUBPROTOCOL if WS_AUTH_SUBPROTOCOL in entries else None


def _ws_debug_label(user: dict | None) -> str:
    """The optional [email] label for a debug log line."""
    return f" [{user['email']}]" if user else ""


def _terminal_io_debug(
    direction: str, msg: dict, msg_type: str, user: dict | None
) -> None:
    """Log a terminal_output/terminal_input frame with a truncated data
    preview (to avoid log spam)."""
    data = msg.get("data", "")
    preview = repr(data[:80]) + ("..." if len(data) > 80 else "")
    logger.debug(
        "WS %s%s: %s data=%s",
        direction,
        _ws_debug_label(user),
        msg_type,
        preview,
    )


def log_ws_msg(direction: str, msg: dict, user: dict | None = None) -> None:
    """Log a WebSocket message for debugging (KLANGKD_WEBSOCKET_DEBUG=1)."""
    if not WS_DEBUG:
        return
    msg_type = msg.get("type") or msg.get("cmd") or "?"
    # Truncate terminal_output/terminal_input data to avoid log spam
    if msg_type in ("terminal_output", "terminal_input"):
        _terminal_io_debug(direction, msg, msg_type, user)
        return
    logger.debug(
        "WS %s%s: %s",
        direction,
        _ws_debug_label(user),
        json.dumps(msg)[:200],
    )


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


async def disconnect_by_jti(
    sockets: WebSocketState,
    jti: str,
    *,
    code: int = 4001,
    reason: str = "",
) -> int:
    """Close every live connection authenticated with *jti* (#3152).

    Hard revocation (logout, session-limit eviction) cuts the sockets
    that token opened; 4001 makes the client log out rather than
    reconnect-loop. Thin delegation to ``WebSocketState.disconnect_by_jti``.
    """
    return await sockets.disconnect_by_jti(jti, code=code, reason=reason)


async def disconnect_deciders_by_user(
    app, user_id: str, *, code: int = 4001, reason: str = ""
) -> int:
    """Close the user's live consent-decider sockets (#3162).

    Account-disable kicks (admin route, inactivity sweep) must cut the
    decider surface too — a decider holds egress-consent authority.
    Minimal app states (tests) may not wire ``consent_deciders`` — then
    there is nothing to close (returns 0).
    """
    deciders = getattr(app.state, "consent_deciders", None)
    if deciders is None:
        return 0
    return await deciders.disconnect_by_user(user_id, code=code, reason=reason)


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


def custom_event_frame(name: str, reason: str | None = None) -> dict:
    """Build a CUSTOM event frame (container_ready, container_stopped, …)."""
    value = {"reason": reason} if reason else {}
    return {
        "type": "event",
        "event": {"type": "CUSTOM", "name": name, "value": value},
    }


def send_event(
    sock: SafeWebSocket, name: str, reason: str | None = None
) -> None:
    """Send a CUSTOM event (container_ready, container_stopped, etc.)."""
    sock.send_json(custom_event_frame(name, reason))


def broadcast_event(
    session: WorkspaceSession | None,
    sock: SafeWebSocket,
    name: str,
    reason: str | None = None,
) -> None:
    """Send a CUSTOM event to every subscriber, plus *sock* if it isn't one.

    #3008: workspace lifecycle events (container_restart, container_ready)
    must reach every connection in the workspace, not only the acting one —
    sibling pages recover exactly like the restarting client instead of
    stranding a dead terminal. *sock* (the acting connection) gets a direct
    send only when it is not a session subscriber, so it is never
    double-sent.

    A subscribed *sock* whose send fails is pruned by ``session.broadcast``
    and gets zero copies with the failure swallowed (``broadcast_to_set``
    catches WS_ERRORS) — a slow/dead acting client must not abort the
    restart itself. That silent drop is logged here so the lost lifecycle
    frames are diagnosable.
    """
    frame = custom_event_frame(name, reason)
    subscribed = session is not None and sock in session.subscribers
    if session is not None:
        session.broadcast(frame)
    if not subscribed:
        sock.send_json(frame)
    elif _pruned_by_broadcast(session, sock, subscribed):
        # Subscribed before the broadcast but pruned by it: the acting
        # connection's send failed and its copy was dropped.
        logger.warning(
            "Acting socket missed %s broadcast (send failed, pruned)", name
        )


def _pruned_by_broadcast(session, sock, subscribed: bool) -> bool:
    """Whether the acting socket was subscribed before the broadcast but
    pruned by it (its send failed and its copy was dropped)."""
    return (
        subscribed and session is not None and sock not in session.subscribers
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


async def refused_without_perm(conn: "Connection", *perms: str) -> bool:
    """Refuse a frame unless the connection holds one of *perms*.

    The per-frame defense-in-depth gate for commands whose only historic
    protection was the ``workspace_connect`` handshake checking
    ``terminal`` (#3022): with ``join-workspace`` as the connect gate, a
    join-only member (or a spectator whose grouped joiner session exists
    after ``join_shared_terminal``) can reach the socket, so frames that
    exec into the container must carry their own check. Sends a plain
    code-less ``Permission denied`` and returns True when the connection
    lacks every listed permission; returns False (and sends nothing) when
    any one is held, so callers write
    ``if await refused_without_perm(...): return``.

    Deliberately NOT stamped with the machine-readable ``forbidden``
    code: that code is reserved for connect-level refusals
    (``workspace_connect`` / ``restart_container``) because the frontend
    classifies any ``forbidden`` frame as an irrevocable dead-end and
    swaps the whole page for the access-revoked view (#2891) — a
    sub-action denial must leave the rest of the page working.
    """
    for perm in perms:
        if await conn.has_perm(perm):
            return False
    send_error(conn.sock, "Permission denied")
    return True
