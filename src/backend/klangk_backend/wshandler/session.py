"""WorkspaceSession and WebSocketState: per-workspace and global state."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .. import agent, auth, container, model, terminal
from .safe_websocket import SafeWebSocket, _WS_ERRORS, _broadcast_to_set
from ._constants import (
    _agent_conversations,
    _cancel_agent_task,
    _log_ws_msg,
)

if TYPE_CHECKING:
    from .connection import Connection  # noqa: allow-deferred-import

logger = logging.getLogger(__name__)


class WorkspaceSession:
    """Shared state for a single workspace.

    Created by the first WebSocket connection, cleaned up by the last.
    """

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.container_id: str | None = None
        self.subscribers: set[SafeWebSocket] = set()
        self.browser_subscribers: set[SafeWebSocket] = set()
        self.lock = asyncio.Lock()
        # Per-user terminal window state, keyed by user_id.
        # Each value is a list of {"name": str, "shared": bool}.
        # This is the in-memory authority; snapshots are persisted
        # to /home/.workspace-state.json for crash recovery.
        self.terminal_windows: dict[str, list[dict]] = {}
        self._save_lock = asyncio.Lock()
        # Workspace token renewal tracking.
        self.workspace_token_expiry: datetime | None = None
        self._token_renewal_task: asyncio.Task | None = None

    async def reset(self) -> None:
        self.subscribers.clear()
        self.browser_subscribers.clear()
        self.terminal_windows.clear()
        # Cancel the token renewal loop so it doesn't keep renewing
        # tokens for a container that has been killed or reset.
        task = self._token_renewal_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._token_renewal_task = None
        self.workspace_token_expiry = None

    async def add_subscriber(
        self, sock: SafeWebSocket, container_id: str
    ) -> None:
        """Register a connection as a subscriber (acquires lock)."""
        async with self.lock:
            self.container_id = container_id
            self.subscribers.add(sock)

    async def remove_subscriber(self, sock: SafeWebSocket) -> bool:
        """Unregister a connection (acquires lock).

        Returns True if no subscribers remain (session should be removed).
        """
        async with self.lock:
            self.subscribers.discard(sock)
            self.browser_subscribers.discard(sock)
            return not self.subscribers

    def broadcast(self, message: dict) -> int:
        """Send message to all subscribers, removing dead ones."""
        return _broadcast_to_set(self.subscribers, message)

    def start_token_renewal(self, expiry: datetime) -> None:
        """Schedule periodic workspace token renewal.

        The token is refreshed at 80% of its lifetime so container
        processes never lose access to the LLM proxy or bridge.
        """
        self.workspace_token_expiry = expiry
        self._token_renewal_task = asyncio.create_task(
            self._token_renewal_loop()
        )

    async def _token_renewal_loop(self) -> None:
        """Periodically renew the workspace token before it expires."""
        while True:
            expiry = self.workspace_token_expiry
            if expiry is None:
                return  # pragma: no cover

            # Renew at 80% of the token lifetime.
            lifetime = auth.WORKSPACE_TOKEN_EXPIRE_HOURS * 3600
            delay = lifetime * 0.8
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

            container_id = self.container_id
            if container_id is None:
                return  # pragma: no cover

            try:
                new_token = auth.create_workspace_token(self.workspace_id)
                await terminal.set_workspace_token(container_id, new_token)
                self.workspace_token_expiry = datetime.now(
                    timezone.utc
                ) + timedelta(hours=auth.WORKSPACE_TOKEN_EXPIRE_HOURS)
                logger.info(
                    "Renewed workspace token for %s",
                    self.workspace_id,
                )
            except Exception:
                logger.warning(
                    "Failed to renew workspace token for %s, retrying in 60s",
                    self.workspace_id,
                    exc_info=True,
                )
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    return

    def broadcast_to_browsers(self, message: dict) -> int:
        """Send message to browser subscribers only, removing dead ones."""
        return _broadcast_to_set(self.browser_subscribers, message)

    async def dispatch_browser_request(
        self, request: dict, timeout: float = 30.0
    ) -> dict:
        """Send a browser_request to browser subscribers and wait for response.

        Called by the /api/browser-delegate HTTP endpoint.  Only sends to
        browser_subscribers (connections that sent ui_ready), not CLI.
        """
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        state.pending_browser_requests[request_id] = (future, None)

        if not self.browser_subscribers:
            state.pending_browser_requests.pop(request_id, None)
            return {"error": "No browser client connected to this workspace"}

        message = {**request, "type": "browser_request", "id": request_id}
        _log_ws_msg("BCAST", message)
        delivered = self.broadcast_to_browsers(message)
        if delivered == 0:
            state.pending_browser_requests.pop(request_id, None)
            return {"error": "No browser client connected to this workspace"}

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            state.pending_browser_requests.pop(request_id, None)
            return {"error": "Browser client did not respond within timeout"}
        except asyncio.CancelledError:
            state.pending_browser_requests.pop(request_id, None)
            raise

    async def dispatch_browser_request_to(
        self, target_sock: SafeWebSocket, request: dict, timeout: float = 30.0
    ) -> dict:
        """Send a browser_request to a specific browser connection.

        Used when a per-connection bridge token identifies the exact
        browser that should handle the request.  Only a response from
        target_sock is accepted.
        """
        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        state.pending_browser_requests[request_id] = (future, target_sock)

        message = {**request, "type": "browser_request", "id": request_id}
        _log_ws_msg("BCAST", message)
        try:
            target_sock.send_json(message)
        except _WS_ERRORS:
            state.pending_browser_requests.pop(request_id, None)
            return {"error": "Browser connection not available"}

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            state.pending_browser_requests.pop(request_id, None)
            return {"error": "Browser client did not respond within timeout"}
        except asyncio.CancelledError:
            state.pending_browser_requests.pop(request_id, None)
            raise

    async def dispatch_browser_request_stream_to(
        self,
        target_sock: "SafeWebSocket",
        request: dict,
        idle_timeout: float,
    ):
        """Stream a browser_request's response chunks to the HTTP caller.

        Yields newline-delimited JSON: zero or more ``{"type":"chunk",...}``
        as the browser streams output, then a terminal ``{"type":"done",...}``
        or ``{"type":"error",...}``.  Unlike the single-response variant, the
        [idle_timeout] bounds the gap *between* chunks, not the total duration,
        so a long-but-progressing query never times out.
        """
        request_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        state.streaming_browser_requests[request_id] = (queue, target_sock)
        message = {
            **request,
            "type": "browser_request",
            "id": request_id,
            "stream": True,
        }
        _log_ws_msg("SEND", message)
        try:
            target_sock.send_json(message)
        except _WS_ERRORS:
            state.streaming_browser_requests.pop(request_id, None)
            yield (
                json.dumps(
                    {
                        "type": "error",
                        "error": "Browser connection not available",
                    }
                )
                + "\n"
            )
            return

        try:
            while True:
                try:
                    item = await asyncio.wait_for(
                        queue.get(), timeout=idle_timeout
                    )
                except asyncio.TimeoutError:
                    yield (
                        json.dumps(
                            {
                                "type": "error",
                                "error": "Browser client did not respond "
                                "within timeout",
                            }
                        )
                        + "\n"
                    )
                    return
                yield json.dumps(item) + "\n"
                if item["type"] != "chunk":
                    return
        finally:
            state.streaming_browser_requests.pop(request_id, None)

    async def full_reset(self) -> None:
        """Clean up all shared state for this workspace.

        Called when a container is killed externally (idle timeout,
        manual stop) so the next workspace_connect starts fresh.
        """
        await state.remove_session(self.workspace_id)
        container.registry.remove_state(self.workspace_id)
        logger.info("Reset workspace state for %s", self.workspace_id)


class WebSocketState:
    """Module-level singleton holding mutable WebSocket handler state."""

    # Delay before broadcasting presence_leave after a user disconnects.
    # If the same user reconnects within this window the leave (and the
    # subsequent re-join) are suppressed, avoiding flicker during
    # WebSocket reconnection with backoff.
    PRESENCE_LEAVE_DELAY = 10.0  # seconds

    def __init__(self) -> None:
        # Active connections: SafeWebSocket -> Connection
        self.connections: dict[SafeWebSocket, "Connection"] = {}
        # Active sessions keyed by workspace_id.
        self.sessions: dict[str, WorkspaceSession] = {}
        # Pending browser-delegate requests: request_id -> asyncio.Future
        # request_id → (future, expected_sock) — the expected_sock is the
        # connection that should send the response.  None means any connection.
        self.pending_browser_requests: dict[
            str, tuple[asyncio.Future, SafeWebSocket | None]
        ] = {}
        # Streaming browser-delegate requests: request_id → (queue, sock).
        # The browser pushes browser_chunk messages onto the queue and a final
        # browser_response terminates it.
        self.streaming_browser_requests: dict[
            str, tuple[asyncio.Queue, SafeWebSocket | None]
        ] = {}
        # Pending presence leave tasks: (workspace_id, user_id) → Task.
        # When a user's last connection drops we schedule a delayed
        # broadcast; if they reconnect before it fires we cancel it.
        self._pending_leaves: dict[tuple[str, str], asyncio.Task] = {}

    def get_session(self, workspace_id: str) -> WorkspaceSession | None:
        return self.sessions.get(workspace_id)

    def get_or_create_session(self, workspace_id: str) -> WorkspaceSession:
        if workspace_id not in self.sessions:
            self.sessions[workspace_id] = WorkspaceSession(workspace_id)
        return self.sessions[workspace_id]

    async def remove_session(self, workspace_id: str) -> None:
        """Remove workspace session (acquires session lock).

        For internal use when the caller does NOT already hold the lock.
        Use ``remove_session_locked`` when the lock is already held.
        """
        session = self.sessions.get(workspace_id)
        if not session:
            return
        async with session.lock:
            # Re-check: someone may have added a subscriber while we waited.
            if session.subscribers:
                return
            self.sessions.pop(workspace_id, None)
            await session.reset()

    async def remove_session_locked(self, session: WorkspaceSession) -> None:
        """Remove session when caller already holds ``session.lock``."""
        self.sessions.pop(session.workspace_id, None)
        await session.reset()

    def cancel_pending_leave(self, workspace_id: str, user_id: str) -> bool:
        """Cancel a pending presence_leave for *user_id* in *workspace_id*.

        Returns True if a pending leave was cancelled (meaning the
        subsequent join broadcast should also be suppressed).
        """
        key = (workspace_id, user_id)
        task = self._pending_leaves.pop(key, None)
        if task and not task.done():
            task.cancel()
            logger.info(
                "Cancelled pending presence_leave for user %s in %s",
                user_id,
                workspace_id,
            )
            return True
        return False

    def schedule_pending_leave(
        self,
        workspace_id: str,
        user: dict,
        session: "WorkspaceSession",
    ) -> None:
        """Schedule a delayed presence_leave broadcast.

        If the user reconnects before the delay expires the task is
        cancelled via ``cancel_pending_leave``.
        """
        user_id = user["id"]
        key = (workspace_id, user_id)
        # Cancel any already-pending leave (shouldn't happen, but be safe).
        old = self._pending_leaves.pop(key, None)
        if old and not old.done():  # pragma: no cover
            old.cancel()

        async def _fire() -> None:
            try:
                await asyncio.sleep(self.PRESENCE_LEAVE_DELAY)
            except asyncio.CancelledError:
                return
            finally:
                self._pending_leaves.pop(key, None)

            # Still no connection for this user in the workspace?
            cur_session = self.get_session(workspace_id)
            if cur_session:
                still_connected = any(
                    self.connections.get(s) is not None
                    and self.connections[s].user["id"] == user_id
                    for s in cur_session.subscribers
                )
                if still_connected:  # pragma: no cover
                    return

                sys_msg = await model.add_chat_message(
                    workspace_id,
                    user_id,
                    user["email"],
                    f"{user.get('handle') or user['email']} left",
                    message_type=model.MSG_SYSTEM,
                )
                cur_session.broadcast({"type": "chat_message", **sys_msg})
                cur_session.broadcast(
                    {
                        "type": "presence_leave",
                        "user_id": user_id,
                        "user_email": user["email"],
                    }
                )

        self._pending_leaves[key] = asyncio.create_task(_fire())

    async def reset_workspace(self, workspace_id: str) -> None:
        """Clean up shared state for a workspace.

        Called when a container is killed externally (idle timeout,
        manual stop) so the next workspace_connect starts fresh.
        Delegates to WorkspaceSession.full_reset if a session exists.
        """
        session = self.get_session(workspace_id)
        if session:
            await session.full_reset()
        else:
            container.registry.remove_state(workspace_id)
            logger.info("Reset workspace state for %s", workspace_id)

        # Clean up module-level agent state for this workspace.
        _agent_conversations.pop(workspace_id, None)
        _cancel_agent_task(workspace_id)
        # Stop the Pi RPC subprocess so it doesn't outlive the container.
        await agent.stop_session(workspace_id)

    async def logout_user(self, user_id: str) -> None:
        """Stop containers for a logging-out user, skipping any that
        have active subscribers belonging to other users."""
        user_workspaces = await model.get_user_workspaces_with_containers(
            user_id
        )
        for ws in user_workspaces:
            if not ws["container_id"]:
                continue
            session = self.get_session(ws["id"])
            if session:
                has_others = any(
                    conn.user["id"] != user_id
                    for sock, conn in self.connections.items()
                    if sock in session.subscribers
                )
                if has_others:
                    continue
            await container.registry.stop_and_remove_container(
                ws["container_id"]
            )
            await self.reset_workspace(ws["id"])

    def notify_container_status(
        self, workspace_id: str, running: bool
    ) -> None:
        """Broadcast container running/stopped status to all connections.

        Sent when a workspace container starts or is killed so the
        workspace list page can update status icons in real time.
        """
        message = {
            "type": "container_status",
            "workspace_id": workspace_id,
            "running": running,
        }
        dead = []
        for sock, conn in self.connections.items():
            if conn.user.get("id") is None:
                continue
            try:
                sock.send_json(message)
            except _WS_ERRORS:
                dead.append(sock)
        for sock in dead:
            self.connections.pop(sock, None)

    def notify_user_workspaces_changed(self, user_id: str) -> None:
        """Send ``workspaces_changed`` to all of a user's connections.

        The frontend re-fetches its workspace list on receipt, so the
        list page reflects creates/deletes made via CLI, API, or another
        tab without a manual refresh.  Fire-and-forget like the other
        per-connection sends; a dead socket is simply discarded.
        """
        message = {"type": "workspaces_changed"}
        dead = []
        for sock, conn in self.connections.items():
            if conn.user.get("id") != user_id:
                continue
            try:
                sock.send_json(message)
            except _WS_ERRORS:
                dead.append(sock)
        for sock in dead:
            self.connections.pop(sock, None)

    def handle_browser_response(
        self, msg: dict, sender: SafeWebSocket | None = None
    ) -> None:
        """Resolve a pending browser-delegate request.

        If the request was dispatched to a specific connection, only
        a response from that connection is accepted.
        """
        request_id = msg.get("id")
        if not request_id:
            return
        # Streaming request: the response is the terminal "done" item.
        stream_entry = self.streaming_browser_requests.get(request_id)
        if stream_entry is not None:
            queue, expected_sock = stream_entry
            if expected_sock is not None and sender is not expected_sock:
                logger.warning(
                    "Browser response from wrong connection for request %s",
                    request_id,
                )
                return
            result = {
                k: v for k, v in msg.items() if k not in ("id", "cmd", "type")
            }
            queue.put_nowait({"type": "done", "result": result})
            return
        entry = self.pending_browser_requests.get(request_id)
        if entry is None:
            logger.debug(
                "Browser response for unknown/completed request %s",
                request_id,
            )
            return
        future, expected_sock = entry
        if expected_sock is not None and sender is not expected_sock:
            logger.warning(
                "Browser response from wrong connection for request %s",
                request_id,
            )
            return
        self.pending_browser_requests.pop(request_id, None)
        if not future.done():
            future.set_result(msg)

    def handle_browser_chunk(
        self, msg: dict, sender: SafeWebSocket | None = None
    ) -> None:
        """Push a streamed chunk onto its request's queue.

        Ignored if the request is unknown or the chunk comes from a
        connection other than the one the request was dispatched to.
        """
        request_id = msg.get("id")
        if not request_id:
            return
        entry = self.streaming_browser_requests.get(request_id)
        if entry is None:
            return
        queue, expected_sock = entry
        if expected_sock is not None and sender is not expected_sock:
            return
        queue.put_nowait({"type": "chunk", "delta": msg.get("delta", "")})


state = WebSocketState()

# Wire up the agent broadcast callback so agent.py can broadcast
# to WebSocket sessions without importing wshandler (breaking the
# agent ↔ wshandler circular dependency).
agent._get_workspace_session = state.get_session
