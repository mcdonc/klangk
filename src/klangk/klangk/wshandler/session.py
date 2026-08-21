"""WorkspaceSession and WebSocketState: per-workspace and global state."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .. import model
from ..terminal import SERVICE_CMD_WINDOW
from ..window_watcher import WindowEventWatcher
from .safe_websocket import SafeWebSocket, WS_ERRORS, broadcast_to_set
from .constants import (
    agent_conversations,
    cancel_agent_task,
    log_ws_msg,
)

if TYPE_CHECKING:
    from .connection import Connection

logger = logging.getLogger(__name__)


def merge_window_entries(old: list[dict], windows: list[dict]) -> list[dict]:
    """Fold a fresh tmux ``list_windows`` result into the session map.

    Single merge for every ``terminal_windows`` producer (#2633 CI race):
    entries are matched by window id (``@N`` — tmux-assigned, never reused
    within a server's lifetime, stable across renames/index reuse, #2192),
    ``shared`` flags carry over from the old entry, and the agent's
    ``service-cmd`` window is shared by definition (#1114).

    Both the controller's sync (:meth:`sync_terminal_windows` in
    controllers.py) and the window-watcher's debounced re-sync
    (:meth:`_sync_windows_once`) MUST update the in-memory map through
    this helper before broadcasting — the share/unshare handlers read
    the map, and a frame sent without the matching map update let a
    client act on a window the server would then not find (the
    ``klangk terminal share`` 10s-blind-timeout flake).
    """
    old_by_id = {w["id"]: w for w in old if "id" in w}
    entries = []
    for w in windows:
        prev = old_by_id.get(w["id"])
        prev_shared = prev.get("shared", False) if prev else False
        entries.append(
            {
                "id": w["id"],
                "name": w["name"],
                "index": w["index"],
                "shared": (w["name"] == SERVICE_CMD_WINDOW or prev_shared),
            }
        )
    return entries


def _iso_utc(ts: float | None) -> str | None:
    """Render an epoch timestamp as an ISO-8601 UTC string, or ``None``."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _service_health_frame(
    workspace_id: str,
    *,
    healthy: bool,
    message: str | None,
    running: bool = True,
    health_checked_at: float | None = None,
    seq: int = 0,
) -> dict:
    """Build a ``service_health`` frame.

    Single source of truth for the event shape shared by the transition
    broadcast (:meth:`WebSocketState.notify_service_health`), the
    container-death broadcast, and the connect-time snapshot
    (:meth:`send_service_health_snapshot`).

    Fields beyond the original ``healthy`` / ``health_message`` pair are
    *additive* -- consumers that ignore unknown keys are unaffected
    (#1175):

    - ``running`` (bool): ``False`` only on the container-death frame,
      so a consumer watching *only* ``service_health`` learns the
      service is down instead of seeing "healthy, then silence"
      (#1175 item 2).  ``True`` for every live-container frame.
    - ``health_checked_at`` (ISO-8601 str | None): when the check last
      ran; ``None`` until the first poll completes.  Lets a consumer
      judge freshness without correlating its own receive clock
      (#1175 item 3a).
    - ``seq`` (int): per-workspace monotonic counter, incremented on
      every emitted frame (transition and death).  On reconnect a
      consumer reconciles snapshot + seq to detect a missed transition
      (#1175 item 4).  Resets when the container state is recreated
      (restart), which is fine -- the connect-time snapshot is the
      reconciliation authority.
    """
    return {
        "type": "service_health",
        "workspace_id": workspace_id,
        "healthy": healthy,
        "health_message": message,
        "running": running,
        "health_checked_at": _iso_utc(health_checked_at),
        "seq": seq,
    }


class WorkspaceSession:
    """Shared state for a single workspace.

    Created by the first WebSocket connection, cleaned up by the last.
    """

    def __init__(self, workspace_id: str, app=None):
        self.workspace_id = workspace_id
        self.app = app
        self.container_id: str | None = None
        self.subscribers: set[SafeWebSocket] = set()
        self.browser_subscribers: set[SafeWebSocket] = set()
        self.lock = asyncio.Lock()
        # Per-user terminal window state, keyed by user_id.
        # Each value is a list of {"name": str, "shared": bool}.
        # The agent's ``service`` session windows are keyed by
        # AGENT_USER_ID (#1133).
        self.terminal_windows: dict[str, list[dict]] = {}
        # Cached agent handle so the ``service:service-cmd`` window stays
        # attributable (and visible in the shared list) even though the
        # agent has no active WS connection -- the agent is never
        # "offline" the way the owner could be under the old model
        # (#1133). Populated by ``_sync_service_windows``.
        self.agent_handle: str | None = None
        # Workspace token renewal tracking.
        self.workspace_token_expiry: datetime | None = None
        self._token_renewal_task: asyncio.Task | None = None
        # Window-sync via a persistent tmux control-mode client: last
        # list_windows snapshot per user_id, so we re-broadcast only on a
        # real change (#2161 / #2171).
        self._last_windows: dict[str, list] = {}
        self._window_watcher: WindowEventWatcher | None = None
        self._window_sync_handle: asyncio.TimerHandle | None = None

    async def reset(self) -> None:
        self.subscribers.clear()
        self.browser_subscribers.clear()
        self.terminal_windows.clear()
        self.agent_handle = None
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
        if self._window_sync_handle is not None:
            self._window_sync_handle.cancel()
            self._window_sync_handle = None
        watcher = self._window_watcher
        self._window_watcher = None
        if watcher is not None:
            asyncio.create_task(watcher.stop())  # pragma: no cover
        self._last_windows.clear()

    async def add_subscriber(
        self,
        sock: SafeWebSocket,
        container_id: str,
        *,
        token_expiry: datetime | None = None,
    ) -> None:
        """Register a connection as a subscriber (acquires lock).

        When *token_expiry* is provided and no renewal loop is running
        yet, ``start_token_renewal`` is called under the session lock so
        two concurrent callers cannot both observe ``expiry is None``
        and create duplicate renewal tasks.
        """
        async with self.lock:
            self.container_id = container_id
            self.subscribers.add(sock)
            if (
                token_expiry is not None
                and self.workspace_token_expiry is None
            ):
                self.start_token_renewal(token_expiry)
            self.start_window_sync()

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
        return broadcast_to_set(self.subscribers, message)

    def start_token_renewal(self, expiry: datetime) -> None:
        """Schedule periodic workspace token renewal.

        The token is refreshed at 80% of its lifetime so container
        processes never lose access to the LLM proxy or bridge.
        """
        self.workspace_token_expiry = expiry
        self._token_renewal_task = asyncio.create_task(
            self._token_renewal_loop()
        )

    def start_window_sync(self) -> None:
        """Start (once) the tmux control-mode window-change watcher so every
        client's tab strip stays in sync with tmux (#2161 / #2171)."""
        if self._window_watcher is not None or self.app is None:
            return
        if self.container_id is None:
            return
        self._window_watcher = WindowEventWatcher(
            self.app.state.podman,
            self.container_id,
            self._schedule_window_sync,
        )
        asyncio.create_task(self._window_watcher.start())  # pragma: no cover

    def _schedule_window_sync(self) -> None:
        """Debounce a burst of control-mode events into one re-sync."""
        if self._window_sync_handle is not None:
            self._window_sync_handle.cancel()
        loop = asyncio.get_running_loop()
        self._window_sync_handle = loop.call_later(
            0.15, self._dispatch_window_sync
        )

    def _dispatch_window_sync(self) -> None:
        self._window_sync_handle = None
        asyncio.create_task(self._sync_windows_once())  # pragma: no cover

    async def _sync_windows_once(self) -> None:
        """Re-broadcast ``terminal_windows`` to each connected user when tmux's
        windows changed (add/close/active) so every client's tab strip updates.
        """
        if self.app is None or not self.container_id or not self.subscribers:
            return
        sockets = self.app.state.sockets
        terminal = self.app.state.terminal
        user_ids: set[str] = set()
        for sock in list(self.subscribers):
            conn = sockets.connections.get(sock)
            uid = conn.user.get("id") if conn else None
            if uid and uid != model.AGENT_USER_ID:
                user_ids.add(uid)
        for uid in user_ids:
            try:
                windows = await terminal.list_windows(self.container_id, uid)
            except Exception:  # pragma: no cover - container mid-restart
                continue
            if self._last_windows.get(uid) == windows:
                continue
            self._last_windows[uid] = windows
            # Update the in-memory map BEFORE broadcasting (#2633 CI
            # race): share/unshare read terminal_windows, and a client
            # acting on this frame must find every listed window in the
            # map. Without this, a watcher frame that beat
            # _start_terminal's sync left the map empty/stale and
            # ``klangk terminal share`` blind-timed-out on the missing
            # window.
            self.terminal_windows[uid] = merge_window_entries(
                self.terminal_windows.get(uid, []), windows
            )
            msg = {"type": "terminal_windows", "windows": windows}
            for sock in list(self.subscribers):
                conn = sockets.connections.get(sock)
                if conn and conn.user.get("id") == uid:
                    try:
                        sock.send_json(msg)
                    except WS_ERRORS:
                        pass

    async def _token_renewal_loop(self) -> None:
        """Periodically renew the workspace token before it expires."""
        while True:
            expiry = self.workspace_token_expiry
            if expiry is None:
                return  # pragma: no cover

            # Renew at 80% of the token lifetime.
            lifetime = self.app.state.auth.workspace_token_expire_hours * 3600
            delay = lifetime * 0.8
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

            container_id = self.container_id
            if container_id is None:
                return  # pragma: no cover

            try:
                new_token = self.app.state.auth.create_workspace_token(
                    self.workspace_id
                )
                await self.app.state.terminal.set_workspace_token(
                    container_id, new_token
                )
                # #2242: refresh the sidecar's bind-mounted token file too —
                # it can't be exec-pushed (no token-setter in the sidecar
                # image), so it reads this file on each consent POST.
                self.app.state.container_registry.write_sidecar_token(
                    self.workspace_id, new_token
                )
                self.workspace_token_expiry = datetime.now(
                    timezone.utc
                ) + timedelta(
                    hours=self.app.state.auth.workspace_token_expire_hours
                )
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
        return broadcast_to_set(self.browser_subscribers, message)

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
        self.app.state.sockets.pending_browser_requests[request_id] = (
            future,
            None,
        )

        if not self.browser_subscribers:
            self.app.state.sockets.pending_browser_requests.pop(
                request_id, None
            )
            return {"error": "No browser client connected to this workspace"}

        message = {**request, "type": "browser_request", "id": request_id}
        log_ws_msg("BCAST", message)
        delivered = self.broadcast_to_browsers(message)
        if delivered == 0:
            self.app.state.sockets.pending_browser_requests.pop(
                request_id, None
            )
            return {"error": "No browser client connected to this workspace"}

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self.app.state.sockets.pending_browser_requests.pop(
                request_id, None
            )
            return {"error": "Browser client did not respond within timeout"}
        except asyncio.CancelledError:
            self.app.state.sockets.pending_browser_requests.pop(
                request_id, None
            )
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
        self.app.state.sockets.pending_browser_requests[request_id] = (
            future,
            target_sock,
        )

        message = {**request, "type": "browser_request", "id": request_id}
        log_ws_msg("BCAST", message)
        try:
            target_sock.send_json(message)
        except WS_ERRORS:
            self.app.state.sockets.pending_browser_requests.pop(
                request_id, None
            )
            return {"error": "Browser connection not available"}

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self.app.state.sockets.pending_browser_requests.pop(
                request_id, None
            )
            return {"error": "Browser client did not respond within timeout"}
        except asyncio.CancelledError:
            self.app.state.sockets.pending_browser_requests.pop(
                request_id, None
            )
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
        self.app.state.sockets.streaming_browser_requests[request_id] = (
            queue,
            target_sock,
        )
        message = {
            **request,
            "type": "browser_request",
            "id": request_id,
            "stream": True,
        }
        log_ws_msg("SEND", message)
        try:
            target_sock.send_json(message)
        except WS_ERRORS:
            self.app.state.sockets.streaming_browser_requests.pop(
                request_id, None
            )
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
            self.app.state.sockets.streaming_browser_requests.pop(
                request_id, None
            )

    async def full_reset(self) -> None:
        """Clean up all shared state for this workspace.

        Called when a container is killed externally (idle timeout,
        manual stop) so the next workspace_connect starts fresh.
        """
        await self.app.state.sockets.remove_session(self.workspace_id)
        await self.app.state.container_registry.remove_state(self.workspace_id)
        logger.info("Reset workspace state for %s", self.workspace_id)


class WebSocketState:
    """Module-level singleton holding mutable WebSocket handler state."""

    # Delay before broadcasting presence_leave after a user disconnects.
    # If the same user reconnects within this window the leave (and the
    # subsequent re-join) are suppressed, avoiding flicker during
    # WebSocket reconnection with backoff.
    PRESENCE_LEAVE_DELAY = 10.0  # seconds

    def __init__(self, app=None) -> None:
        self.app = app
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

    def reconfigure(self, app) -> None:
        self.app = app

    def get_session(self, workspace_id: str) -> WorkspaceSession | None:
        return self.sessions.get(workspace_id)

    def get_or_create_session(
        self, workspace_id: str, app=None
    ) -> WorkspaceSession:
        try:
            return self.sessions[workspace_id]
        except KeyError:
            session = WorkspaceSession(workspace_id, app=app)
            return self.sessions.setdefault(workspace_id, session)

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

    async def disconnect_all(self) -> None:
        """Close every connection and clear all in-memory session state.

        Used by the SIGHUP runtime-restart path.  Connected clients are
        closed with code 1012 ("service restarted") so they reconnect
        and rebuild state against the freshly-started containers.
        Deliberately leaves the container registry untouched -- the
        registry needs its container-id -> workspace map intact for the
        subsequent ``registry.shutdown()`` to find containers to stop.

        Each handler coroutine's own ``finally`` block then runs a
        no-op cleanup once the event loop schedules it: by then
        ``connections`` and ``sessions`` are empty, so there is nothing
        left for it to do.
        """
        socks = list(self.connections.keys())
        self.connections.clear()

        # Cancel pending presence-leave broadcasts and abandoned
        # browser-delegate requests so they don't fire against state
        # we're about to drop.
        for task in self._pending_leaves.values():
            task.cancel()
        self._pending_leaves.clear()
        for fut, _sock in self.pending_browser_requests.values():
            if not fut.done():
                fut.cancel()
        self.pending_browser_requests.clear()
        self.streaming_browser_requests.clear()

        # Reset each workspace session (cancels token-renewal tasks,
        # clears subscriber sets) then drop the entries.
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            await session.reset()

        # Tell every client to reconnect (1012 = "service restart").
        for sock in socks:
            try:
                await sock.close(code=1012)
            except Exception:  # noqa: BLE001
                logger.debug("Error closing socket during restart")

    async def disconnect_user(
        self, user_id: str, *, code: int = 4001, reason: str = ""
    ) -> int:
        """Close every live connection for *user_id* (#2588).

        Used when an account is disabled (admin action or the inactivity
        sweep): close code 4001 makes the client log out rather than
        reconnect-loop. Only the sockets are closed — each handler's own
        ``finally`` block then runs the normal disconnect cleanup
        (presence leave, session bookkeeping), the same as a natural
        client disconnect. Returns how many connections were closed.
        """
        socks = [
            sock
            for sock, conn in self.connections.items()
            if conn.user.get("id") == user_id
        ]
        for sock in socks:
            try:
                await sock.close(code=code, reason=reason)
            except Exception:  # noqa: BLE001
                logger.debug("Error closing socket for user %s", user_id)
        return len(socks)

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

                sys_msg = await self.app.state.model.chat.add_chat_message(
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
            await self.app.state.container_registry.remove_state(workspace_id)
            logger.info("Reset workspace state for %s", workspace_id)

        # Clean up module-level agent state for this workspace.
        agent_conversations.pop(workspace_id, None)
        cancel_agent_task(workspace_id)
        # Stop the Pi RPC subprocess so it doesn't outlive the container.
        await self.app.state.agents.stop_session(workspace_id)

    def notify_container_status(
        self,
        workspace_id: str,
        running: bool,
        service_started_at: float | None = None,
    ) -> None:
        """Broadcast container running/stopped status to all connections.

        Sent when a workspace container starts or is killed so the
        workspace list page can update status icons in real time.
        """
        message: dict = {
            "type": "container_status",
            "workspace_id": workspace_id,
            "running": running,
        }
        if service_started_at is not None:
            message["service_started_at"] = service_started_at
        dead = []
        for sock, conn in self.connections.items():
            if conn.user.get("id") is None:
                continue
            try:
                sock.send_json(message)
            except WS_ERRORS:
                dead.append((sock, conn))
        for sock, _ in dead:
            self.connections.pop(sock, None)

    def notify_service_health(
        self,
        workspace_id: str,
        *,
        healthy: bool,
        message: str | None = None,
        running: bool = True,
        health_checked_at: float | None = None,
        seq: int = 0,
    ) -> None:
        """Broadcast service-health status to all connections.

        Fanned out to every connection (like
        :meth:`notify_container_status`) so the workspace list page can
        reflect health transitions for auto-started services even when no
        one is connected to that workspace's terminal session (#1015).
        The frontend ignores events for workspaces it doesn't display.

        ``message`` carries the failure *reason* (a bounded tail of the
        check's stderr/stdout) so an unhealthy workspace isn't a black
        box -- operators can see *why* it failed without log access
        (#1088).  ``None`` when healthy.

        ``running`` is ``True`` for live-container transitions and
        ``False`` for the terminal container-death frame (#1175 item 2);
        ``health_checked_at`` is the epoch of the last poll (#1175 item
        3a); ``seq`` is the per-workspace monotonic counter (#1175 item
        4).  All additive -- defaults preserve the legacy shape.
        """
        message_dict = _service_health_frame(
            workspace_id,
            healthy=healthy,
            message=message,
            running=running,
            health_checked_at=health_checked_at,
            seq=seq,
        )
        dead = []
        for sock, conn in self.connections.items():
            if conn.user.get("id") is None:
                continue
            try:
                sock.send_json(message_dict)
            except WS_ERRORS:
                dead.append((sock, conn))
        for sock, _ in dead:
            self.connections.pop(sock, None)

    def send_service_health_snapshot(self, sock: SafeWebSocket) -> None:
        """Send the current health of every health-checked workspace.

        The ``service_health`` stream is **deltas only**: it fires on a
        status transition, not every poll, so a steady-state unhealthy
        workspace is invisible to a consumer that connects after the
        transition (#1175).  This closes that hole by replaying the
        current status of every workspace the registry has *already*
        checked (``health_check`` configured and at least one poll
        completed) to a single connection right after it registers.

        Mirrors :meth:`notify_service_health`'s fan-out scope (every
        workspace, consumer filters): server-side scoping is tracked
        separately (#1175 item 5).  Consumer-side the frame is applied
        idempotently, so a transition arriving just after the snapshot
        is harmless.  Workspaces whose container has died are absent
        from ``registry.states`` and thus skipped (the container-death
        hole is #1175 item 2).
        """
        for cs in list(self.app.state.container_registry.states.values()):
            if cs.health_check is None or cs.health_status is None:
                continue
            try:
                sock.send_json(
                    _service_health_frame(
                        cs.workspace_id,
                        healthy=cs.health_status == "healthy",
                        message=cs.health_message,
                        running=True,
                        health_checked_at=cs.health_checked_at,
                        seq=cs.health_seq,
                    )
                )
            except WS_ERRORS:
                # The just-registered socket is already gone; nothing to
                # snapshot to.  dispatch.py owns cleanup on disconnect.
                break

    def send_health_heartbeats(self) -> None:
        """Send a liveness heartbeat to connections that opted in.

        The ``service_health`` stream is deltas-only, so a consumer
        can't tell "nothing changed" from "the health loop stalled /
        the server wedged" -- silence looks like health, the worst
        failure mode (#1175 item 3b).  This emits a
        ``service_health_heartbeat`` frame (its own type, so
        ``--type service_health`` consumers are unaffected) to every
        connection that asked for it via the
        ``subscribe_health_heartbeat`` command.

        Called from ``HealthMonitor.run_health_loop`` at the end of
        each tick, so the heartbeat's presence proves the health loop
        itself is alive -- if the loop stalls, heartbeats stop.
        """
        frame = {
            "type": "service_health_heartbeat",
            "timestamp": _iso_utc(time.time()),
        }
        dead = []
        for sock, conn in self.connections.items():
            if conn.user.get("id") is None:
                continue
            if not getattr(conn, "wants_health_heartbeat", False):
                continue
            try:
                sock.send_json(frame)
            except WS_ERRORS:
                dead.append((sock, conn))
        for sock, _ in dead:
            self.connections.pop(sock, None)

    def handle_subscribe_health_heartbeat(
        self, msg: dict, sock: SafeWebSocket
    ) -> None:
        """Opt a connection into (or out of) health heartbeats.

        Request: ``{"cmd": "subscribe_health_heartbeat", "enabled":
        true}``.  ``enabled`` defaults to True when omitted.  Stores the
        flag on the connection so :meth:`send_health_heartbeats`
        includes it on every health-loop tick (#1175 item 3b).
        """
        conn = self.connections.get(sock)
        if conn is None:
            return
        conn.wants_health_heartbeat = bool(msg.get("enabled", True))

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
            except WS_ERRORS:
                dead.append((sock, conn))
        for sock, _ in dead:
            self.connections.pop(sock, None)

    def notify_user_terminals_changed(
        self,
        user_id: str,
        workspace_id: str,
        windows: list[dict] | None = None,
    ) -> None:
        """Send ``terminals_changed`` to all of a user's connections.

        Carries the current ``windows`` list so push-fed consumers (e.g. the
        TUI workspace-detail screen) can update directly, the way the Flutter
        UI receives ``terminal_windows`` over its workspace WS -- avoiding a
        ``terminal_start`` re-enumeration round-trip per change (#1894).
        ``windows`` is optional for backward compatibility with older callers;
        the key is omitted entirely when it is ``None`` so legacy consumers
        that test ``"windows" in event`` are not misled.
        """
        message = {"type": "terminals_changed", "workspace_id": workspace_id}
        if windows is not None:
            message["windows"] = windows
        dead = []
        for sock, conn in self.connections.items():
            if conn.user.get("id") != user_id:
                continue
            try:
                sock.send_json(message)
            except WS_ERRORS:
                dead.append((sock, conn))
        for sock, _ in dead:
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
