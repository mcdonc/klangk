"""WorkspaceSession and WebSocketState: per-workspace and global state."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Coroutine
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from .. import model
from ..acl import check_permission_inmemory, resource_ancestors
from ..terminal import SERVICE_CMD_WINDOW
from .window_watcher import WindowEventWatcher
from .safe_websocket import SafeWebSocket, WS_ERRORS, broadcast_to_set
from .support import log_ws_msg

if TYPE_CHECKING:
    from .connection import Connection

logger = logging.getLogger(__name__)

# Strong references to the session's fire-and-forget tasks (#2913): the
# event loop only keeps strong references to tasks while they are
# scheduled, so an unreferenced task suspended in an await is
# GC-eligible mid-execution — the same hazard guarded against by
# ``lifecycle._status_broadcast_tasks`` (#1714 review) and
# ``consent._activity_tasks`` in klangksidecar. Module-level, NOT an
# instance attribute: the ``reset()`` case spawns ``watcher.stop()``
# (the per-workspace tmux ``-C`` control-mode teardown), which must
# finish after its session has been dropped and popped from the
# sockets map — an instance set would die with the session and leave
# the teardown task unreferenced again. The done-callback
# (:func:`_finish_session_task`) discards from the set and logs a
# failure, since nobody awaits a fire-and-forget task to observe it.
_session_tasks: set[asyncio.Task] = set()


def _finish_session_task(task: asyncio.Task) -> None:
    """Done-callback for :func:`spawn_session_task` (#2913).

    Discards the strong reference so the set cannot grow without
    bound, and logs an unobserved failure: these tasks are
    fire-and-forget, so without this an exception would surface only
    as asyncio's context-free ``Task exception was never retrieved``
    at task GC — the same reason ``lifecycle.broadcast_container_status``
    wraps its broadcast in try/except + logging (#1714). Retrieving the
    exception here also marks it seen for asyncio.
    """
    _session_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Background session task failed", exc_info=exc)


def spawn_session_task(coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Schedule *coro* in the background, holding a strong reference (#2913).

    See ``_session_tasks`` above: the caller may drop every other
    reference (fire-and-forget) without exposing the task to
    mid-execution garbage collection.
    """
    task = asyncio.create_task(coro)
    _session_tasks.add(task)
    task.add_done_callback(_finish_session_task)
    return task


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
    :meth:`WorkspaceSession.apply_window_list` (which wraps this helper)
    before broadcasting — the share/unshare handlers read
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


def _shared_viewer_map(
    ws_session, sockets: "WebSocketState"
) -> dict[tuple[str, str], list[dict]]:
    """(owner_user_id, window_id) -> [{user_id, email}] for shared viewers."""
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
    return viewer_map


def _session_user_handle(
    ws_session, sockets: "WebSocketState", user_id: str
) -> str | None:
    """The user's handle from any active connection.

    The agent (AGENT_USER_ID) has no WS connection, so its handle is the
    cached ``agent_handle`` populated by ``sync_service_windows`` -- the
    agent is always attributable, never "offline" (#1133)."""
    if user_id == model.AGENT_USER_ID:
        return ws_session.agent_handle
    for sock in ws_session.subscribers:
        conn = sockets.connections.get(sock)
        if conn and conn.user.get("id") == user_id:
            return conn.user.get("handle")
    return None


def get_shared_terminals(ws_session, sockets: "WebSocketState") -> list[dict]:
    """Collect all shared windows across all users in a workspace."""
    viewer_map = _shared_viewer_map(ws_session, sockets)
    terminals = []
    for user_id, windows in ws_session.terminal_windows.items():
        handle = _session_user_handle(ws_session, sockets, user_id)
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


def _shared_id_sets(
    old: list[dict], new_entries: list[dict]
) -> tuple[set, set]:
    """(old, new) sets of shared-window ids."""
    old_shared = {w["id"] for w in old if w.get("shared") and "id" in w}
    new_shared = {w["id"] for w in new_entries if w.get("shared")}
    return old_shared, new_shared


def _shared_name_sets(
    old: list[dict], new_entries: list[dict]
) -> tuple[set, set]:
    """(old, new) sets of (id, name) pairs for shared windows."""
    old_shared_names = {
        (w["id"], w["name"]) for w in old if w.get("shared") and "id" in w
    }
    new_shared_names = {
        (w["id"], w["name"]) for w in new_entries if w.get("shared")
    }
    return old_shared_names, new_shared_names


def _shared_set_changed(old: list[dict], new_entries: list[dict]) -> bool:
    """Broadcast-worthy delta: shared set changed (a shared window was
    added or closed) or any shared window was renamed."""
    old_shared, new_shared = _shared_id_sets(old, new_entries)
    old_shared_names, new_shared_names = _shared_name_sets(old, new_entries)
    return old_shared != new_shared or old_shared_names != new_shared_names


def iso_utc(ts: float | None) -> str | None:
    """Render an epoch timestamp as an ISO-8601 UTC string, or ``None``."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def service_health_frame(
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
        "health_checked_at": iso_utc(health_checked_at),
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
        # (#1133). Populated by ``sync_service_windows``.
        self.agent_handle: str | None = None
        # Workspace token renewal tracking.
        self.workspace_token_expiry: datetime | None = None
        self._token_renewal_task: asyncio.Task | None = None
        # Window-sync via a persistent tmux control-mode client: last
        # list_windows snapshot per user_id, so we re-broadcast only on a
        # real change (#2161 / #2171).
        self._last_windows: dict[str, list] = {}
        # Monotonic per-user generation of the applied window list
        # (#2653). Bumped by every apply_window_list; the debounced
        # watcher stamps it before starting its list_windows exec and
        # discards a snapshot that returns to a moved counter — a podman
        # exec that started before a tmux rename/new/close committed can
        # land after the command handler applied the newer list, and
        # applying it would transiently revert the map, the baseline,
        # and the broadcasts (new → old → new flap).
        self._window_generations: dict[str, int] = {}
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
            spawn_session_task(watcher.stop())
        self._last_windows.clear()
        self._window_generations.clear()

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

    def apply_window_list(self, user_id: str, windows: list[dict]) -> bool:
        """Fold a fresh tmux ``list_windows`` result into the session map.

        Returns True when the shared-window set or any shared window name
        changed — the signal that ``shared_terminals`` viewers need a
        re-broadcast. Both window-list producers (the terminal command
        handlers and the debounced window-watcher sync) MUST go through
        this method so a shared rename is detected exactly once, by
        whichever path applies it first (#2651: the watcher merged
        renamed entries into the map without broadcasting, erasing the
        delta the rename handler's own broadcast relied on, so other
        users' shared-terminal tab lists never updated).

        Entries are matched by window id (``@N`` — unique and never
        reused within a tmux server's lifetime, stable across renames
        and index reuse, #2192) and ``shared`` flags carry over; the
        agent's ``service-cmd`` window is shared by definition (#1114).

        Applying also bumps the per-user window generation so the
        watcher can tell a stale in-flight snapshot from a current one
        (#2653).
        """
        old = self.terminal_windows.get(user_id, [])
        new_entries = merge_window_entries(old, windows)
        self.terminal_windows[user_id] = new_entries
        # This list is now the newest applied state for the user (#2653):
        # a watcher list_windows exec still in flight predates it, and
        # its snapshot must be discarded instead of applied over it.
        self._window_generations[user_id] = (
            self._window_generations.get(user_id, 0) + 1
        )
        return _shared_set_changed(old, new_entries)

    def broadcast_shared_terminals(self) -> None:
        """Send the current shared-terminal list to all subscribers."""
        if self.app is None:
            return
        terminals = get_shared_terminals(self, self.app.state.sockets)
        self.broadcast({"type": "shared_terminals", "terminals": terminals})

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
        spawn_session_task(self._window_watcher.start())

    def _schedule_window_sync(self) -> None:
        """Debounce a burst of control-mode events into one re-sync."""
        if self._window_sync_handle is not None:
            self._window_sync_handle.cancel()
        loop = asyncio.get_running_loop()
        self._window_sync_handle = loop.call_later(
            0.15, self._dispatch_window_sync
        )

    def _connected_user_ids(self, sockets) -> set[str]:
        """Ids of the connected non-agent subscribers."""
        user_ids: set[str] = set()
        for sock in list(self.subscribers):
            conn = sockets.connections.get(sock)
            uid = conn.user.get("id") if conn else None
            if uid and uid != model.AGENT_USER_ID:
                user_ids.add(uid)
        return user_ids

    def _send_windows_to_user(
        self, sockets, user_id: str, windows: list[dict]
    ) -> None:
        """Broadcast one user's terminal_windows frame to just their sockets."""
        msg = {"type": "terminal_windows", "windows": windows}
        for sock in list(self.subscribers):
            conn = sockets.connections.get(sock)
            if conn and conn.user.get("id") == user_id:
                try:
                    sock.send_json(msg)
                except WS_ERRORS:
                    pass

    def _dispatch_window_sync(self) -> None:
        self._window_sync_handle = None
        spawn_session_task(self._sync_windows_once())

    async def _sync_windows_once(self) -> None:
        """Re-broadcast ``terminal_windows`` to each connected user when tmux's
        windows changed (add/close/active) so every client's tab strip updates.
        """
        if self.app is None or not self.container_id or not self.subscribers:
            return
        sockets = self.app.state.sockets
        terminal = self.app.state.terminal
        user_ids = self._connected_user_ids(sockets)
        for uid in user_ids:
            # Stamp the apply generation before the exec (#2653): the
            # list_windows below is a podman round-trip that can straddle
            # a command handler's tmux mutation and its apply. If the
            # counter moved by the time the exec returns, a newer list
            # was applied while we were in flight and this snapshot is
            # stale — applying it would revert the map, the baseline,
            # and the client frames to the pre-mutation state.
            generation = self._window_generations.get(uid, 0)
            try:
                windows = await terminal.list_windows(self.container_id, uid)
            except Exception:
                continue
            if self._window_generations.get(uid, 0) != generation:
                # Stale in-flight snapshot (#2653): the newer applied
                # list already updated the map, the baseline, and the
                # broadcasts; drop ours instead of reverting them.
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
            shared_changed = self.apply_window_list(uid, windows)
            self._send_windows_to_user(sockets, uid, windows)
            # A rename/close/add that touched a shared window must also
            # reach shared-terminal viewers: this path can be the first
            # to apply the change (under load its list_windows exec can
            # beat the command handler's), and the handler's own delta
            # check would then see no change (#2651).
            if shared_changed:
                self.broadcast_shared_terminals()

    async def _token_renewal_loop(self) -> None:
        """Periodically renew the workspace token before it expires."""
        while True:
            expiry = self.workspace_token_expiry
            if expiry is None:
                return

            # Renew at 80% of the token lifetime.
            lifetime = self.app.state.auth.workspace_token_expire_hours * 3600
            delay = lifetime * 0.8
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return

            container_id = self.container_id
            if container_id is None:
                return

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

    async def full_reset(
        self, expected_container_id: str | None = None
    ) -> None:
        """Clean up all shared state for this workspace.

        Called when a container is killed externally (idle timeout,
        manual stop) so the next workspace_connect starts fresh.
        *expected_container_id* (#331) threads the dead container id
        down to :meth:`container_registry.remove_state`'s re-bind guard:
        a racing user-driven start may have re-bound the workspace to a
        fresh container, in which case the fresh registry state survives.
        """
        await self.app.state.sockets.remove_session(self.workspace_id)
        await self.app.state.container_registry.remove_state(
            self.workspace_id, expect_container_id=expected_container_id
        )
        logger.info("Reset workspace state for %s", self.workspace_id)


class WebSocketState:
    """Module-level singleton holding mutable WebSocket handler state."""

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
            # Fall back to the state object's own app when a caller
            # omits it: a session built with ``app=None`` would silently
            # skip every ``shared_terminals`` broadcast
            # (WorkspaceSession.broadcast_shared_terminals guards on
            # ``self.app``), with no error to surface the mistake (#2652
            # review).
            session = WorkspaceSession(workspace_id, app=app or self.app)
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

        # Cancel abandoned browser-delegate requests so they don't fire
        # against state we're about to drop.
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
        ``finally`` block then runs the normal disconnect cleanup, the same
        as a natural
        client disconnect (session bookkeeping, terminal teardown), the same
        as a natural disconnect. Returns how many connections were closed.
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

    async def reset_workspace(
        self, workspace_id: str, *, expected_container_id: str | None = None
    ) -> None:
        """Clean up shared state for a workspace.

        Called when a container is killed externally (idle timeout,
        manual stop) so the next workspace_connect starts fresh.
        Delegates to WorkspaceSession.full_reset if a session exists.
        *expected_container_id* (#331) is the dead container id; it
        guards ``remove_state`` against a racing re-bind.
        """
        session = self.get_session(workspace_id)
        if session:
            await session.full_reset(
                expected_container_id=expected_container_id
            )
        else:
            await self.app.state.container_registry.remove_state(
                workspace_id, expect_container_id=expected_container_id
            )
            logger.info("Reset workspace state for %s", workspace_id)

    async def _send_to_workspace_members(
        self, workspace_id: str, message: dict
    ) -> None:
        """Send a per-workspace *message* to that workspace's members (#1714).

        The pre-#1714 fan-outs iterated every authenticated connection
        and left filtering to the client, which leaked every workspace's
        id, running state, and health (including the bounded
        ``health_message`` tail of another tenant's service output) to
        anyone with a WebSocket. This scopes delivery server-side:
        connections are grouped by user and each distinct user is
        ACL-checked for ``monitor`` on ``/workspaces/{workspace_id}`` —
        the dedicated status-observation permission (#2783). Every
        grant path that carries ``terminal`` also seeds ``monitor``
        (the owner's wildcard ACE, a direct member share, and every
        role group), and ``monitor`` can be granted alone for
        monitoring-only members, while the deployment-wide
        ``view``-for-authenticated seed ACE at ``/`` is deliberately
        too weak to count as membership.

        Sends to all of an allowed user's connections (a user with the
        workspace open in two tabs gets both). Dead sockets are pruned;
        dispatch.py owns cleanup on disconnect.

        One ACE fetch for the whole fan-out: the entries for the
        resource's ancestor paths are loaded once and each distinct
        user is then evaluated in memory
        (:func:`klangk.acl.check_permission_inmemory`, the same
        preload-then-evaluate shape ``permissions_for_resources`` uses)
        — a transition burst doesn't re-query identical paths per user.
        """
        by_user: dict[str, list[tuple[SafeWebSocket, "Connection"]]] = {}
        for sock, conn in self.connections.items():
            uid = conn.user.get("id")
            if uid is None:
                continue
            by_user.setdefault(uid, []).append((sock, conn))
        if not by_user:
            return
        resource = f"/workspaces/{workspace_id}"
        acl = self.app.state.acl
        entries = await self.app.state.model.acl.get_acl_entries_map(
            resource_ancestors(resource)
        )
        dead = []
        for uid, conns in by_user.items():
            principals = await acl.get_principals(uid)
            if not check_permission_inmemory(
                resource, principals, "monitor", entries
            ):
                continue
            for sock, _conn in conns:
                try:
                    sock.send_json(message)
                except WS_ERRORS:
                    dead.append(sock)
        for sock in dead:
            self.connections.pop(sock, None)

    async def notify_container_status(
        self,
        workspace_id: str,
        running: bool,
        service_started_at: float | None = None,
    ) -> None:
        """Broadcast container running/stopped status to workspace members.

        Sent when a workspace container starts or is killed so the
        workspace list page can update status icons in real time.
        Scoped to members of *workspace_id* (#1714) via
        :meth:`_send_to_workspace_members`.
        """
        message: dict = {
            "type": "container_status",
            "workspace_id": workspace_id,
            "running": running,
        }
        if service_started_at is not None:
            message["service_started_at"] = service_started_at
        await self._send_to_workspace_members(workspace_id, message)

    async def notify_workspace_evicted(
        self, workspace_id: str, *, reason: str = "host memory pressure"
    ) -> None:
        """Broadcast a workspace-evicted event to workspace members (#2526).

        Scoped to members of *workspace_id* (#1714) like
        :meth:`notify_container_status`, so workspace list pages and any
        client watching the workspace learn *why* it stopped — the
        eviction path's own analogue of the idle monitor's
        ``container_stopped``/idle-timeout event, kept distinct from it
        so clients can tell a memory eviction from an idle timeout.
        Recipients get the normal ``container_status`` running=False
        frame separately via ``notify_workspace_killed``.
        """
        message: dict = {
            "type": "workspace_evicted",
            "workspace_id": workspace_id,
            "reason": reason,
        }
        await self._send_to_workspace_members(workspace_id, message)

    def broadcast_to_all(self, message: dict) -> None:
        """Broadcast *message* to every authenticated connection.

        Generic fan-out counterpart to the typed ``notify_host_*``
        methods, used by the host scheduler (#2661) for
        ``server_schedule`` / ``server_schedule_fired`` frames. Dead sockets
        are dropped (dispatch.py owns cleanup on disconnect).
        """
        self.fanout(message)

    def fanout(
        self,
        message: dict,
        *,
        user_id: str | None = None,
        predicate=None,
    ) -> None:
        """Send *message* to every (matching) connection; drop dead ones.

        The shared broadcast core of the typed ``notify_*`` methods
        (host shutdown/recycle/started, per-user workspace/terminal
        changes, the heartbeat opt-in). ``user_id`` restricts delivery
        to that user's connections (None = every authenticated
        connection); *predicate* further filters on the connection (the
        heartbeat opt-in flag). A socket that errors on send is
        discarded from the registry — dispatch.py owns the rest of
        disconnect cleanup.
        """
        dead = []
        for sock, conn in self.connections.items():
            uid = conn.user.get("id")
            if uid is None or (user_id is not None and uid != user_id):
                continue
            if predicate is not None and not predicate(conn):
                continue
            try:
                sock.send_json(message)
            except WS_ERRORS:
                dead.append(sock)
        for sock in dead:
            self.connections.pop(sock, None)

    def notify_host_shutdown(self) -> None:
        """Broadcast host-shutdown to all connections (#2527).

        Sent by the TERM/INT graceful-shutdown hook (main.py) and by a
        scheduled stop firing (#2661) before uvicorn closes the
        WebSockets, so clients can show "server went away" instead of a
        bare reconnect loop. A recycle (SIGHUP or scheduled) does NOT
        send this — it sends :meth:`notify_server_recycle` /
        :meth:`notify_host_started` instead, because its runtime comes
        back.
        """
        self.fanout({"type": "host_shutdown"})

    def notify_server_recycle(self, phase: str) -> None:
        """Broadcast a server-recycle progress event (#2527, #2661).

        Sent at each phase of the graceful runtime recycle (``draining``,
        ``recycling``) — SIGHUP and a scheduled recycle both — so clients
        can show a "server recycling" notice before the per-workspace
        stop frames and the 1012 disconnect arrive.
        """
        self.fanout({"type": "server_recycle", "phase": phase})

    def notify_host_started(self) -> None:
        """Broadcast host-started to all connections (#2527).

        Sent when a graceful recycle completes. Most clients see it after
        reconnecting (the recycle drops every WebSocket with 1012); it is
        the all-clear counterpart to :meth:`notify_server_recycle`.
        """
        self.fanout({"type": "host_started"})

    async def notify_service_health(
        self,
        workspace_id: str,
        *,
        healthy: bool,
        message: str | None = None,
        running: bool = True,
        health_checked_at: float | None = None,
        seq: int = 0,
    ) -> None:
        """Broadcast service-health status to workspace members.

        Scoped to members of *workspace_id* (#1714) via
        :meth:`_send_to_workspace_members`, so the workspace list page
        still reflects health transitions for auto-started services even
        when no one is connected to that workspace's terminal session
        (#1015) — but other tenants no longer learn them.

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
        message_dict = service_health_frame(
            workspace_id,
            healthy=healthy,
            message=message,
            running=running,
            health_checked_at=health_checked_at,
            seq=seq,
        )
        await self._send_to_workspace_members(workspace_id, message_dict)

    async def send_service_health_snapshot(self, sock: SafeWebSocket) -> None:
        """Send the current health of the member workspaces to one socket.

        The ``service_health`` stream is **deltas only**: it fires on a
        status transition, not every poll, so a steady-state unhealthy
        workspace is invisible to a consumer that connects after the
        transition (#1175).  This closes that hole by replaying the
        current status of every workspace the registry has *already*
        checked (``health_check`` configured and at least one poll
        completed) to a single connection right after it registers.

        Scoped to the connection's user's memberships (#1714): the
        allowed workspace set is resolved with one
        ``permissions_for_resources`` pass (``monitor`` on each
        candidate resource — the dedicated status-observation
        permission, #2783), so a connecting client learns the health
        of workspaces it can observe — never other tenants'.

        Mirrors :meth:`notify_service_health`'s frame shape.  Consumer-side
        the frame is applied idempotently, so a transition arriving just
        after the snapshot is harmless; a transition or death arriving
        *during* the snapshot's ACL pass is detected (seq/state re-check)
        and the stale replay frame is dropped rather than sent over the
        newer delta.  Workspaces whose container has died are absent
        from ``registry.states`` and thus skipped (the container-death
        hole is #1175 item 2).
        """
        conn = self.connections.get(sock)
        user_id = conn.user.get("id") if conn else None
        if user_id is None:
            return
        registry = self.app.state.container_registry
        # Snapshot (state, seq) pairs: the ACL pass below awaits, and a
        # container that dies or a health transition that fires in that
        # window must not be overwritten by a stale replay frame — the
        # per-workspace seq bumps on every emit and the state is dropped
        # on death, so a mismatch means a newer frame already went out.
        candidates = [
            (cs, cs.health_seq)
            for cs in registry.states.values()
            if cs.health_check is not None and cs.health_status is not None
        ]
        if not candidates:
            return
        acl = self.app.state.acl
        principals = await acl.get_principals(user_id)
        resources = [f"/workspaces/{cs.workspace_id}" for cs, _ in candidates]
        allowed = await acl.permissions_for_resources(
            resources, principals, ["monitor"]
        )
        for cs, seq in candidates:
            if not self._replay_service_health(
                sock, registry, cs, seq, allowed
            ):
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
        self.fanout(
            {
                "type": "service_health_heartbeat",
                "timestamp": iso_utc(time.time()),
            },
            predicate=lambda conn: getattr(
                conn, "wants_health_heartbeat", False
            ),
        )

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
        self.fanout({"type": "workspaces_changed"}, user_id=user_id)

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
        self.fanout(message, user_id=user_id)

    def _replay_service_health(
        self, sock, registry, cs, seq: int, allowed: set
    ) -> bool:
        """Replay one workspace's health frame; False when the socket died
        (caller stops). Stale/died/moved-on candidates are skipped."""
        if f"/workspaces/{cs.workspace_id}" not in allowed:
            return True
        if registry.states.get(cs.workspace_id) is not cs:
            # Died (state removed) or was re-bound while the ACL
            # pass was in flight — its terminal/death frame already
            # went to this member; don't replay over it.
            return True
        if cs.health_seq != seq:
            # A transition fired mid-snapshot; its delta frame
            # already went out — replaying the older status over it
            # would flip the client backwards.
            return True
        try:
            sock.send_json(
                service_health_frame(
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
            return False
        return True

    @staticmethod
    def _wrong_connection(request_id, expected_sock, sender) -> bool:
        """Whether a browser response came from other than the dispatched
        connection (dispatched requests accept only their addressee)."""
        if expected_sock is None or sender is expected_sock:
            return False
        logger.warning(
            "Browser response from wrong connection for request %s",
            request_id,
        )
        return True

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
            if self._wrong_connection(request_id, expected_sock, sender):
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
        if self._wrong_connection(request_id, expected_sock, sender):
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
