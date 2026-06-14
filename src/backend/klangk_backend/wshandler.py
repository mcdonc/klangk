"""WebSocket handler: auth, workspace routing, terminal/exec/bridge."""

import asyncio
import json
import logging
import re
import time
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from . import auth, container, model, workspaces
from .util import derive_hosting_info, resolve_env_secret
from .dockerexec import ExecSession
from .terminal import TerminalSession

logger = logging.getLogger(__name__)

_WS_DEBUG = bool(resolve_env_secret("KLANGK_WS_DEBUG"))

# Max size for terminal/exec input data (base64-decoded bytes).
_MAX_INPUT_SIZE = 65536

# Max outbound messages before we declare the client too slow and close.
_SEND_QUEUE_SIZE = 256


def bridge_idle_timeout() -> float:
    """Max seconds between streamed browser chunks before giving up.

    Bounds the gap between chunks (not the total query duration), so a
    long-but-progressing stream never times out.  Override with
    KLANGK_BRIDGE_TIMEOUT_SECONDS.
    """
    raw = resolve_env_secret("KLANGK_BRIDGE_TIMEOUT_SECONDS")
    try:
        return float(raw) if raw else 30.0
    except ValueError:
        return 30.0


class SlowClientError(Exception):
    """Raised when the outbound queue is full (client can't keep up)."""


# Exceptions that indicate a dead or broken WebSocket connection.
_WS_ERRORS = (
    SlowClientError,
    WebSocketDisconnect,
    RuntimeError,
    ConnectionError,
    OSError,
)


class SafeWebSocket:
    """Bounded-queue WebSocket writer.

    All outbound messages are placed on a bounded asyncio.Queue.
    A dedicated sender task drains the queue and writes to the
    underlying WebSocket, serializing concurrent sends.  If the
    queue is full the client is too slow — we drop it immediately
    rather than blocking the read loop or forwarder tasks.
    """

    def __init__(
        self, websocket: WebSocket, *, maxsize: int = _SEND_QUEUE_SIZE
    ):
        self._sock = websocket
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue(
            maxsize=maxsize
        )
        self._sender_task: asyncio.Task | None = None
        self._closed = False

    def start_sender(self) -> None:
        """Launch the background sender coroutine."""
        self._sender_task = asyncio.create_task(self._sender_loop())

    async def _sender_loop(self) -> None:
        """Drain the outbound queue and write to the WebSocket."""
        try:
            while True:
                msg = await self._queue.get()
                if msg is None:
                    break
                await self._sock.send_json(msg)
        except asyncio.CancelledError:
            raise
        except _WS_ERRORS:
            # Socket gone — nothing to do, cleanup handles the rest.
            pass

    async def stop_sender(self) -> None:
        """Signal the sender task to exit and wait for it."""
        self._closed = True
        task = self._sender_task
        if task is None:
            return
        # Sentinel to break out of the loop.
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            # Queue is full — cancel the task directly.
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Sender task failed unexpectedly")
        self._sender_task = None

    def send_json(self, data: dict) -> None:
        """Enqueue *data* for sending.  Non-blocking.

        Raises ``SlowClientError`` if the queue is full or the sender
        has been stopped.
        """
        if self._closed:
            raise SlowClientError("sender stopped — cannot enqueue")
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            raise SlowClientError("outbound queue full — closing slow client")

    async def accept(self) -> None:
        await self._sock.accept()

    async def receive_text(self) -> str:
        return await self._sock.receive_text()

    async def close(self, code: int = 1000) -> None:
        await self._sock.close(code=code)

    @property
    def headers(self):
        """Proxy header access to the underlying WebSocket."""
        return self._sock.headers

    @property
    def raw(self) -> WebSocket:
        """Access the underlying WebSocket (e.g. for identity checks)."""
        return self._sock


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

    async def reset(self) -> None:
        self.subscribers.clear()
        self.browser_subscribers.clear()

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


def _get_presence_list(workspace_id: str) -> list[dict]:
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
                {"user_id": conn.user["id"], "user_email": conn.user["email"]}
            )
    # Include agent only if its RPC process is alive.
    from . import agent  # pragma: no cover

    if agent.any_running():  # pragma: no cover
        users.append(
            {"user_id": model.AGENT_USER_ID, "user_email": model.AGENT_EMAIL}
        )
    return users


def _get_shared_terminals(ws_session) -> list[dict]:
    """Collect all shared windows across all users in a workspace."""
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
            logger.info(
                "_get_shared_terminals: no handle for user_id=%s "
                "(subscribers=%d, has_shared=%s)",
                user_id,
                len(ws_session.subscribers),
                any(w.get("shared") for w in windows),
            )
            continue
        for w in windows:
            if w.get("shared"):
                terminals.append(
                    {
                        "user_id": user_id,
                        "handle": handle,
                        "window_name": w["name"],
                    }
                )
    return terminals


class Connection:
    """Per-WebSocket connection state and command handlers."""

    def __init__(self, ws: SafeWebSocket, user: dict):
        self.sock = ws
        self.user = user
        self.workspace_id: str | None = None
        self.container_id: str | None = None
        self.terminal_session: TerminalSession | None = None
        self.terminal_task: asyncio.Task | None = None
        self.exec_session: ExecSession | None = None
        self.exec_task: asyncio.Task | None = None
        self.workspace: dict | None = None
        self._idle_cb = None
        self.pending_status_msg: str | None = None
        self._bridge_token: str | None = None
        self._user_home: str | None = None
        self._terminal_cols: int = 80
        self._terminal_rows: int = 24

    async def start_workspace_container(
        self, workspace_id: str, workspace: dict
    ) -> None:
        """Start/restart container for a workspace."""
        owner_id = workspace.get("user_id", self.user["id"])
        host_path = str(
            workspaces.get_workspace_host_path(owner_id, workspace_id)
        )
        home_path = str(workspaces.get_home_host_path(owner_id, workspace_id))
        cfg_path = str(workspaces.get_config_host_path(owner_id, workspace_id))

        hosting_hostname, hosting_proto, hosting_base_path = (
            derive_hosting_info(self.sock.headers)
        )
        (
            container_id,
            container_status,
        ) = await container.registry.start_container(
            workspace_id,
            host_path,
            home_path,
            workspace.get("container_id"),
            num_ports=workspace.get(
                "num_ports", container.DEFAULT_PORTS_PER_WORKSPACE
            ),
            hosting_hostname=hosting_hostname,
            hosting_proto=hosting_proto,
            hosting_base_path=hosting_base_path,
            image=workspace.get("image"),
            config_path=cfg_path,
            extra_mounts=workspace.get("mounts"),
            extra_env=workspace.get("env"),
            user_id=self.user["id"],
        )
        self.container_status = container_status
        self.workspace_id = workspace_id
        self.container_id = container_id

        session = state.get_or_create_session(workspace_id)
        await session.add_subscriber(self.sock, container_id)

        # Register idle timeout notification (per-connection)
        sock = self.sock

        async def on_idle(wid: str) -> None:
            try:
                _send_event(sock, "container_stopped", "idle timeout")
            except _WS_ERRORS:
                pass

        self._idle_cb = on_idle
        # No await between lock release and callback registration — the idle
        # loop cannot interleave here in asyncio's single-threaded model.
        # If an await is added before on_idle_stop, move registration inside the lock.
        container.registry.on_idle_stop(workspace_id, on_idle)

        # Cache workspace info for auto-restart
        self.workspace = workspace

        # Clear any stale pending_status_msg from a prior connect/restart.
        self.pending_status_msg = None

        # Resolve handle from DB and ensure per-workspace home symlink.
        handle = await model.get_user_handle(self.user["id"])
        if handle:
            workspace_home = workspaces.home_path(owner_id, workspace_id)
            self._user_home = workspaces.ensure_home_symlink(
                workspace_home, handle, self.user["id"]
            )
        else:
            self._user_home = None

        logger.info("Container ready for workspace %s", workspace_id)

    async def handle_workspace_connect(self, msg: dict) -> None:
        t_connect_start = time.monotonic()
        workspace_id = msg.get("workspaceId")
        if not workspace_id:
            send_error(self.sock, "Missing workspaceId")
            return

        from . import acl as _acl

        principals = await _acl.get_principals(self.user["id"])
        if not await _acl.check_permission(
            f"/workspaces/{workspace_id}", principals, "terminal"
        ):
            send_error(self.sock, "Permission denied")
            return
        workspace = await workspaces.get_workspace(workspace_id)
        if workspace is None:
            send_error(self.sock, "Workspace not found")
            return

        logger.info(
            "workspace-open: check permissions and fetch workspace from DB: %.3fs",
            time.monotonic() - t_connect_start,
        )

        # Disconnect from any current workspace
        await self.handle_workspace_disconnect()

        t_container = time.monotonic()
        await self.start_workspace_container(workspace_id, workspace)
        logger.info(
            "workspace-open: start or reuse container (see breakdown above): %.3fs",
            time.monotonic() - t_container,
        )

        t_post = time.monotonic()
        ports = await container.registry.get_workspace_ports(workspace_id)
        status = getattr(self, "container_status", "created")
        container_name, ports_str = _format_container_info(workspace_id, ports)
        status_msg = {
            "connected": f"Connected to running container {container_name}{ports_str}",
            "restarted": f"Restarted stopped container {container_name}{ports_str}",
            "created": f"Created new container {container_name}{ports_str}",
        }.get(status, "Container ready")

        status_msg += _format_idle_timeout(container.IDLE_TIMEOUT_SECONDS)

        self.sock.send_json(
            {
                "type": "workspace_ready",
                "workspaceId": workspace_id,
                "userId": self.user["id"],
                "ports": ports,
                "defaultCommand": workspace.get("default_command"),
            }
        )
        # Send chat history to the connecting user
        chat_history = await model.get_chat_messages(workspace_id)
        if chat_history:
            self.sock.send_json(
                {"type": "chat_history", "messages": chat_history}
            )

        # Send workspace members for @mention autocomplete
        members = await model.get_workspace_members(workspace_id)
        owner = await model.get_user_by_id(workspace.get("user_id", ""))
        if owner and not any(m["id"] == owner["id"] for m in members):
            members.append({"id": owner["id"], "email": owner["email"]})
        # Include the agent so @misterboops autocompletes
        members.append({"id": model.AGENT_USER_ID, "email": model.AGENT_EMAIL})
        self.sock.send_json({"type": "workspace_members", "members": members})

        # Start the agent eagerly so it shows as present immediately.
        if self.container_id:
            asyncio.create_task(self._start_agent_if_needed(self.container_id))

        # Send presence list to joining user and broadcast join to others
        presence = _get_presence_list(workspace_id)
        self.sock.send_json({"type": "presence_list", "users": presence})
        session = state.get_session(workspace_id)
        if session:
            join_msg = {
                "type": "presence_join",
                "user_id": self.user["id"],
                "user_email": self.user["email"],
            }
            for sock in list(session.subscribers):
                if sock is not self.sock:
                    sock.send_json(join_msg)

            # Broadcast a system chat message for the join
            sys_msg = await model.add_chat_message(
                workspace_id,
                self.user["id"],
                self.user["email"],
                f"{self.user['email']} joined",
                message_type=model.MSG_SYSTEM,
            )
            session.broadcast({"type": "chat_message", **sys_msg})

        logger.info(
            "workspace-open: send chat history, members, and presence to client: %.3fs",
            time.monotonic() - t_post,
        )

        # Store status for when frontend sends ui_ready
        self.pending_status_msg = status_msg
        logger.info(
            "workspace-open: TOTAL workspace connect (user sees workspace_ready after this): %.3fs",
            time.monotonic() - t_connect_start,
        )
        logger.info(
            "User %s connected to workspace %s (ports %s)",
            self.user["email"],
            workspace_id,
            ports,
        )

    async def handle_workspace_disconnect(self) -> None:
        await self.cleanup()
        self.workspace_id = None
        self.container_id = None

    async def handle_restart_container(self) -> None:
        """Restart a stopped container (e.g., after idle timeout)."""
        if not self.workspace_id:
            send_error(self.sock, "Not connected to a workspace")
            return

        # Save before cleanup — cleanup clears state fields.
        workspace_id = self.workspace_id
        user = self.user
        workspace = self.workspace

        _send_event(self.sock, "container_restart", "Restarting container...")

        try:
            await self.cleanup()
        except _WS_ERRORS as e:
            logger.warning("Cleanup error during restart: %s", e)

        if workspace is None:
            workspace = await workspaces.get_workspace(
                workspace_id, user["id"]
            )
        if workspace is None:
            send_error(self.sock, "Workspace not found")
            return

        await self.start_workspace_container(workspace_id, workspace)
        container.registry.record_activity(self.container_id)

        # Update container_id on ALL connections to this workspace
        # so they don't try to exec into the old (removed) container.
        new_cid = self.container_id
        for sock, conn in state.connections.items():
            if conn.workspace_id == workspace_id and conn is not self:
                conn.container_id = new_cid

        ports = await container.registry.get_workspace_ports(workspace_id)
        container_name, ports_str = _format_container_info(workspace_id, ports)
        status_msg = f"Container restarted {container_name}{ports_str}"

        timeout_mins = container.IDLE_TIMEOUT_SECONDS / 60
        if timeout_mins == int(timeout_mins):
            status_msg += f" — idle timeout: {int(timeout_mins)}m"
        else:
            status_msg += f" — idle timeout: {timeout_mins:.1f}m"

        _send_event(self.sock, "container_ready", status_msg)

        logger.info(
            "Container restarted via restart_container command for workspace %s",
            workspace_id,
        )

    async def handle_shutdown_container(self) -> None:
        """Explicitly shut down the workspace container."""
        if not self.workspace_id:
            send_error(self.sock, "Not connected to a workspace")
            return
        if not self.container_id:
            send_error(self.sock, "No container running")
            return

        workspace_id = self.workspace_id
        container_id = self.container_id

        # Notify all subscribers before stopping.
        session = state.get_session(workspace_id)
        if session:
            session.broadcast(
                {
                    "type": "event",
                    "event": {
                        "type": "CUSTOM",
                        "name": "container_stopped",
                        "value": {"reason": "shut down by user"},
                    },
                }
            )

        # Clear container_id on ALL connections to prevent stale exec attempts.
        for sock, conn in state.connections.items():
            if conn.workspace_id == workspace_id:
                conn.container_id = None

        try:
            await container.registry.stop_and_remove_container(container_id)
        except Exception as e:
            logger.warning("Error stopping container: %s", e)

        await container.registry._notify_workspace_killed(workspace_id)

        logger.info(
            "Container shut down by user for workspace %s", workspace_id
        )

    async def handle_terminal_start(self, msg: dict) -> None:
        logger.info(
            "handle_terminal_start: user=%s workspace=%s container=%s user_home=%s",
            self.user.get("email"),
            self.workspace_id,
            self.container_id,
            self._user_home,
        )
        if not self.container_id:
            logger.info("handle_terminal_start: no container_id, skipping")
            return
        # Debounce: if the last terminal start was very recent, skip.
        # This prevents rapid retry loops when the PTY exits immediately.
        import time as _time

        now = _time.monotonic()
        if hasattr(self, "_last_terminal_start"):
            if now - self._last_terminal_start < 2.0:
                logger.warning(
                    "Ignoring rapid terminal_start (%.1fs since last)",
                    now - self._last_terminal_start,
                )
                return
        self._last_terminal_start = now
        if self._user_home is None:
            send_error(self.sock, "Handle not set")
            return
        if not await self._has_perm("code-in-isolation"):
            # Spectators can't start isolated terminals but still need
            # the terminal pane. Send terminal_started (no session) so
            # the frontend renders shared tabs.
            logger.info(
                "Skipping isolated terminal for user=%s (no code-in-isolation)",
                self.user.get("email"),
            )
            self.sock.send_json({"type": "terminal_started"})
            return
        # Stop existing terminal if any
        await self.stop_terminal()
        cols = msg.get("cols", self._terminal_cols)
        rows = msg.get("rows", self._terminal_rows)
        self._terminal_cols = cols
        self._terminal_rows = rows
        command_override = msg.get("commandOverride")
        session = TerminalSession(
            self.container_id,
            session_name=self.user["id"],
            user_home=self._user_home,
            user_id=self.user["id"],
            user_handle=self.user.get("handle"),
        )

        # Revoke any existing bridge token from a previous terminal
        # session before creating a new one to avoid leaking entries.
        if self._bridge_token:
            container.registry.revoke_connection_token(self.sock)
        bridge_token = container.registry.create_bridge_token(
            self.workspace_id, sock=self.sock
        )
        self._bridge_token = bridge_token

        # Store session immediately so stop_terminal can clean it up
        # if another terminal_start arrives before this one finishes.
        self.terminal_session = session
        conn = self

        async def _start_terminal() -> None:
            try:
                logger.info(
                    "_start_terminal: starting for user=%s container=%s",
                    conn.user.get("email"),
                    conn.container_id,
                )
                await asyncio.wait_for(
                    session.start(
                        cols,
                        rows,
                        command_override=command_override,
                        bridge_token=bridge_token,
                    ),
                    timeout=30,
                )
                if not await conn._activate_session(session, cols, rows):
                    return
                conn.sock.send_json({"type": "terminal_started"})
                # Rename the initial window to "1" and send the list.
                try:
                    from .terminal import (
                        list_windows,
                        load_workspace_state,
                        restore_windows,
                    )

                    sname = conn._tmux_session_name()
                    user_id = conn.user["id"]
                    ws_session = state.get_session(conn.workspace_id)

                    # On first terminal_start after restart, restore
                    # saved window state from the container.
                    if (
                        ws_session
                        and user_id not in ws_session.terminal_windows
                    ):
                        saved = await load_workspace_state(conn.container_id)
                        if user_id in saved:
                            saved_windows = saved[user_id]
                            await restore_windows(
                                conn.container_id, sname, saved_windows
                            )
                            ws_session.terminal_windows[user_id] = (
                                saved_windows
                            )
                            # Restore shared state for ALL users from snapshot
                            for uid, wins in saved.items():
                                if uid != user_id:
                                    ws_session.terminal_windows.setdefault(
                                        uid, wins
                                    )

                    windows = await list_windows(conn.container_id, sname)
                    conn._sync_terminal_windows(windows)
                    conn.sock.send_json(
                        {
                            "type": "terminal_windows",
                            "windows": windows,
                        }
                    )
                except Exception:
                    pass  # Non-critical; tabs update on next window op
                # Also send the shared terminal list from in-memory state.
                ws_session = state.get_session(conn.workspace_id)
                if ws_session:
                    terminals = _get_shared_terminals(ws_session)
                    conn.sock.send_json(
                        {"type": "shared_terminals", "terminals": terminals}
                    )
            except asyncio.CancelledError:
                await session.stop()
                container.registry.revoke_connection_token(conn.sock)
                conn._bridge_token = None
                raise
            except Exception as e:
                await session.stop()
                container.registry.revoke_connection_token(conn.sock)
                conn._bridge_token = None
                logger.exception("Terminal start failed: %s", e)
                send_error(conn.sock, f"Terminal start failed: {e}")

        self.terminal_task = asyncio.create_task(_start_terminal())

    async def handle_terminal_input(self, msg: dict) -> None:
        import time as _time

        t0 = _time.monotonic()
        session = self.terminal_session
        if session is None or not session.is_alive:
            logger.warning("terminal_input: no session or not alive")
            return
        data = msg.get("data", "")
        if session.read_only:
            # Allow terminal protocol responses (DA queries, color
            # reports) through so tmux can complete initialization.
            # Block user-typed input.
            if not data.startswith("\x1b"):
                return
        if len(data) > _MAX_INPUT_SIZE:
            logger.warning(
                "terminal_input too large (%d bytes), dropping", len(data)
            )
            return
        container.registry.record_activity(self.container_id)
        await session.write(data)
        elapsed = _time.monotonic() - t0
        if elapsed > 0.1:  # pragma: no cover
            logger.warning("terminal_input SLOW: %.3fs", elapsed)

    async def handle_terminal_resize(self, msg: dict) -> None:
        self._terminal_cols = msg.get("cols", 80)
        self._terminal_rows = msg.get("rows", 24)
        session = self.terminal_session
        if session is None:
            return
        await session.resize(self._terminal_cols, self._terminal_rows)

    async def handle_terminal_stop(self) -> None:
        await self.stop_terminal()

    def _tmux_session_name(self) -> str:
        """Get the tmux session name (user_id).

        Callers must check ``_user_home`` before calling this method.
        """
        return self.user["id"]

    def _sync_terminal_windows(self, windows: list[dict]) -> None:
        """Update in-memory terminal_windows from tmux list_windows result."""
        ws_session = state.get_session(self.workspace_id)
        if not ws_session:
            return
        user_id = self.user["id"]
        old = ws_session.terminal_windows.get(user_id, [])
        # Build a map of shared state by window name from old data
        shared_by_name = {w["name"]: w.get("shared", False) for w in old}
        ws_session.terminal_windows[user_id] = [
            {"name": w["name"], "shared": shared_by_name.get(w["name"], False)}
            for w in windows
        ]
        self._save_state_snapshot(ws_session)

    async def handle_terminal_new_window(self, msg: dict) -> None:
        import time as _time

        t0 = _time.monotonic()
        if not self.container_id or not self._user_home:
            return
        from .terminal import new_window

        session_name = self._tmux_session_name()
        name = msg.get("name")
        try:
            windows = await new_window(
                self.container_id, session_name, name=name
            )
            logger.info(
                "handle_terminal_new_window: %.3fs",
                _time.monotonic() - t0,
            )
            self._sync_terminal_windows(windows)
            self.sock.send_json(
                {"type": "terminal_windows", "windows": windows}
            )
        except Exception as e:
            send_error(self.sock, f"Failed to create window: {e}")

    async def handle_terminal_select_window(self, msg: dict) -> None:
        import time as _time

        t0 = _time.monotonic()
        if not self.container_id or not self._user_home:
            return
        from .terminal import select_window

        session_name = self._tmux_session_name()
        index = msg.get("index", 0)
        try:
            await select_window(self.container_id, session_name, index)
            logger.info(
                "handle_terminal_select_window: index=%s %.3fs",
                index,
                _time.monotonic() - t0,
            )
        except Exception as e:
            send_error(self.sock, f"Failed to select window: {e}")

    async def handle_terminal_close_window(self, msg: dict) -> None:
        if not self.container_id or not self._user_home:
            return
        from .terminal import close_window

        session_name = self._tmux_session_name()
        index = msg.get("index", 0)
        try:
            windows = await close_window(
                self.container_id, session_name, index
            )
            self._sync_terminal_windows(windows)
            self.sock.send_json(
                {"type": "terminal_windows", "windows": windows}
            )
        except Exception as e:
            send_error(self.sock, f"Failed to close window: {e}")

    async def handle_terminal_rename_window(self, msg: dict) -> None:
        if not self.container_id or not self._user_home:
            return
        from .terminal import list_windows, rename_window

        session_name = self._tmux_session_name()
        index = msg.get("index", 0)
        name = msg.get("name", "")
        if not name:
            send_error(self.sock, "Name required")
            return
        try:
            await rename_window(self.container_id, session_name, index, name)
            windows = await list_windows(self.container_id, session_name)
            self._sync_terminal_windows(windows)
            self.sock.send_json(
                {"type": "terminal_windows", "windows": windows}
            )
        except Exception as e:
            send_error(self.sock, f"Failed to rename window: {e}")

    async def handle_terminal_list_windows(self) -> None:
        if not self.container_id or not self._user_home:
            return
        from .terminal import list_windows

        session_name = self._tmux_session_name()
        try:
            windows = await list_windows(self.container_id, session_name)
            self.sock.send_json(
                {"type": "terminal_windows", "windows": windows}
            )
        except Exception as e:
            send_error(self.sock, f"Failed to list windows: {e}")

    async def _has_perm(self, perm: str) -> bool:
        """Check if the connected user has a workspace permission."""
        from . import acl as _acl

        if not self.workspace_id:
            return False
        principals = await _acl.get_principals(self.user["id"])
        return await _acl.check_permission(
            f"/workspaces/{self.workspace_id}", principals, perm
        )

    async def handle_share_window(self, msg: dict) -> None:
        """Mark one of the user's own windows as shared."""
        if not self.container_id or not self._user_home:
            return
        if not await self._has_perm("share-terminals"):
            send_error(self.sock, "Permission denied")
            return
        index = msg.get("index")
        if index is None:
            send_error(self.sock, "Window index required")
            return
        user_id = self.user["id"]
        ws_session = state.get_session(self.workspace_id)
        if not ws_session:
            return
        windows = ws_session.terminal_windows.get(user_id, [])
        if index < 0 or index >= len(windows):
            send_error(self.sock, "Invalid window index")
            return
        windows[index]["shared"] = True
        self._broadcast_shared_terminals(ws_session)
        self._save_state_snapshot(ws_session)

    async def handle_unshare_window(self, msg: dict) -> None:
        """Remove sharing from a window and kick joiners."""
        if not self.container_id or not self._user_home:
            return
        if not await self._has_perm("share-terminals"):
            send_error(self.sock, "Permission denied")
            return
        from .terminal import kill_joiner_sessions

        index = msg.get("index")
        if index is None:
            send_error(self.sock, "Window index required")
            return
        user_id = self.user["id"]
        session_name = self._tmux_session_name()
        ws_session = state.get_session(self.workspace_id)
        if not ws_session:
            return
        windows = ws_session.terminal_windows.get(user_id, [])
        if index < 0 or index >= len(windows):
            send_error(self.sock, "Invalid window index")
            return
        windows[index]["shared"] = False
        # Kick spectators/collaborators
        try:
            await kill_joiner_sessions(self.container_id, session_name)
        except Exception:
            logger.debug("Failed to kill joiner sessions", exc_info=True)
        ws_session.broadcast(
            {
                "type": "shared_terminal_deleted",
                "user_id": user_id,
                "window_name": windows[index]["name"],
            }
        )
        self._broadcast_shared_terminals(ws_session)
        self._save_state_snapshot(ws_session)

    async def handle_join_shared_terminal(self, msg: dict) -> None:
        """Join another user's shared window via session group."""
        if not self.container_id or not self._user_home:
            return
        if not await self._has_perm("spectate-on-shared-terminals"):
            send_error(self.sock, "Permission denied")
            return

        owner_user_id = msg.get("user_id", "").strip()
        window_name = msg.get("window_name", "").strip()
        if not owner_user_id or not window_name:
            send_error(self.sock, "user_id and window_name required")
            return

        # Find the window index from the workspace session state
        ws_session = state.get_session(self.workspace_id)
        if not ws_session:
            return
        owner_windows = ws_session.terminal_windows.get(owner_user_id, [])
        window_index = None
        for i, w in enumerate(owner_windows):
            if w.get("name") == window_name and w.get("shared"):
                window_index = i
                break
        if window_index is None:
            send_error(self.sock, "Shared terminal not found")
            return

        read_only = not (
            await self._has_perm("code-in-shared-terminals")
            or await self._has_perm("share-terminals")
        )

        # Stop the current terminal session and join the owner's
        # session group on the default tmux server (no -S socket).
        # tmux session is named by user_id.
        await self.stop_terminal()
        session = TerminalSession(
            self.container_id,
            session_name=self.user["id"],
            user_home=self._user_home,
            join_session=owner_user_id,
            read_only=read_only,
            user_id=self.user["id"],
            user_handle=self.user.get("handle"),
        )
        self.terminal_session = session
        conn = self

        cols = self._terminal_cols
        rows = self._terminal_rows

        async def _start_shared() -> None:
            try:
                await session.start(cols, rows)
                if not await conn._activate_session(session, cols, rows):
                    return
                # Select the specific shared window
                from .terminal import select_window

                await select_window(
                    conn.container_id, owner_user_id, window_index
                )
                conn.sock.send_json(
                    {
                        "type": "terminal_started",
                        "shared_user_id": owner_user_id,
                        "shared_window": window_name,
                        "readOnly": read_only,
                    }
                )
            except asyncio.CancelledError:  # pragma: no cover
                await session.stop()
                raise
            except Exception as e:
                await session.stop()
                logger.exception("Shared terminal join failed: %s", e)
                send_error(conn.sock, f"Failed to join shared terminal: {e}")

        self.terminal_task = asyncio.create_task(_start_shared())

    async def handle_list_shared_terminals(self) -> None:
        if not self.workspace_id:
            return
        if not await self._has_perm("spectate-on-shared-terminals"):
            send_error(self.sock, "Permission denied")
            return
        ws_session = state.get_session(self.workspace_id)
        if not ws_session:
            self.sock.send_json({"type": "shared_terminals", "terminals": []})
            return
        terminals = _get_shared_terminals(ws_session)
        self.sock.send_json(
            {"type": "shared_terminals", "terminals": terminals}
        )

    def _broadcast_shared_terminals(self, ws_session) -> None:
        """Broadcast the current shared terminal list to all subscribers."""
        terminals = _get_shared_terminals(ws_session)
        ws_session.broadcast(
            {"type": "shared_terminals", "terminals": terminals}
        )

    def _save_state_snapshot(self, ws_session) -> None:
        """Schedule a serialized save of workspace state to the container.

        Callers must ensure ``container_id`` is set.
        Uses the session's _save_lock so concurrent saves don't overlap.
        """
        from .terminal import save_workspace_state

        container_id = self.container_id
        # Snapshot the state now — the dict may mutate before the task runs.
        snapshot = {
            uid: [dict(w) for w in wins]
            for uid, wins in ws_session.terminal_windows.items()
        }

        async def _do_save() -> None:
            async with ws_session._save_lock:
                await save_workspace_state(container_id, snapshot)

        asyncio.create_task(_do_save())

    # Keep old handler name for backwards compat with existing E2E tests
    async def handle_create_shared_terminal(self, msg: dict) -> None:
        """Create a new shared terminal (legacy API — creates a new window
        and marks it shared)."""
        if not self.container_id or not self._user_home:
            return
        if not await self._has_perm("share-terminals"):
            send_error(self.sock, "Permission denied")
            return
        name = msg.get("name", "").strip()
        if not name:
            send_error(self.sock, "Name required")
            return
        session_name = self._tmux_session_name()
        # Create a new window with this name and mark it shared
        from .terminal import new_window

        try:
            await new_window(self.container_id, session_name, name=name)
        except Exception as e:
            send_error(self.sock, f"Failed to create shared terminal: {e}")
            return
        ws_session = state.get_session(self.workspace_id)
        if not ws_session:
            return
        user_id = self.user["id"]
        windows = ws_session.terminal_windows.setdefault(user_id, [])
        windows.append({"name": name, "shared": True})
        self._broadcast_shared_terminals(ws_session)
        self._save_state_snapshot(ws_session)

    async def handle_delete_shared_terminal(self, msg: dict) -> None:
        """Delete a shared terminal (legacy API — unshares and closes
        the window)."""
        if not self.container_id:
            return
        if not await self._has_perm("share-terminals"):
            send_error(self.sock, "Permission denied")
            return
        from .terminal import close_window, kill_joiner_sessions

        owner_user_id = msg.get("user_id", "").strip()
        window_name = msg.get("window_name", "").strip()
        if not owner_user_id or not window_name:
            send_error(self.sock, "user_id and window_name required")
            return
        ws_session = state.get_session(self.workspace_id)
        if not ws_session:
            return
        owner_windows = ws_session.terminal_windows.get(owner_user_id, [])
        window_index = None
        for i, w in enumerate(owner_windows):
            if w.get("name") == window_name:
                window_index = i
                break
        if window_index is None:
            send_error(self.sock, "Terminal not found")
            return
        try:
            await kill_joiner_sessions(self.container_id, owner_user_id)
            await close_window(self.container_id, owner_user_id, window_index)
        except Exception as e:
            send_error(self.sock, f"Failed to delete shared terminal: {e}")
            return
        owner_windows.pop(window_index)
        ws_session.broadcast(
            {
                "type": "shared_terminal_deleted",
                "user_id": owner_user_id,
                "window_name": window_name,
            }
        )
        self._broadcast_shared_terminals(ws_session)
        self._save_state_snapshot(ws_session)

    # Legacy error handler kept for coverage
    async def _handle_list_error(
        self, e: Exception
    ) -> None:  # pragma: no cover
        send_error(self.sock, f"Failed to list shared terminals: {e}")

    async def handle_exec_start(self, msg: dict) -> None:
        if not self.container_id:
            return
        await self.stop_exec()
        command = msg.get("command", [])
        if not command:
            send_error(self.sock, "exec_start requires a command list")
            return
        session = ExecSession(self.container_id, user_home=self._user_home)
        await session.start(command)
        self.exec_session = session
        self.exec_task = asyncio.create_task(self.forward_exec_output(session))
        container.registry.record_activity(self.container_id)

    async def handle_exec_input(self, msg: dict) -> None:
        session = self.exec_session
        if session is None or not session.is_alive:
            return
        import base64

        raw = base64.b64decode(msg.get("data", ""))
        if len(raw) > _MAX_INPUT_SIZE:
            logger.warning(
                "exec_input too large (%d bytes), dropping", len(raw)
            )
            return
        container.registry.record_activity(self.container_id)
        await session.write(raw)

    async def handle_exec_close_stdin(self) -> None:
        session = self.exec_session
        if session is None:
            return
        await session.close_stdin()

    async def handle_exec_stop(self) -> None:
        await self.stop_exec()

    async def handle_heartbeat(self) -> None:
        if self.container_id is not None:
            container.registry.record_activity(self.container_id)

    async def handle_chat_send(self, msg: dict) -> None:
        workspace_id = self.workspace_id
        if not workspace_id:
            send_error(self.sock, "Not connected to a workspace")
            return
        text = msg.get("message", "").strip()
        if not text:
            return
        chat_msg = await model.add_chat_message(
            workspace_id, self.user["id"], self.user["email"], text
        )
        session = state.get_session(workspace_id)
        if session:
            session.broadcast({"type": "chat_message", **chat_msg})

        # Route to agent on @mention or natural follow-up.
        #
        # After an @mention, the same user's messages route to the
        # agent indefinitely until someone else speaks (interjection).
        # After interjection, a 30s window applies — follow-ups from
        # the original user still route within that window.  Messages
        # starting with @someone-else always break the conversation.
        should_route = False
        user_id = self.user["id"]
        conv = _agent_conversations.get(workspace_id)

        if _mentions_agent(text):
            should_route = True
            _agent_conversations[workspace_id] = {
                "user_id": user_id,
                "time": time.monotonic(),
                "interjected": False,
            }
        elif conv and not _addresses_other_user(text):
            if user_id == conv["user_id"]:
                if not conv["interjected"]:
                    # No interjection — route indefinitely
                    should_route = True
                    conv["time"] = time.monotonic()
                elif time.monotonic() - conv["time"] < 30:
                    # Interjected but within 30s window
                    should_route = True
                    conv["time"] = time.monotonic()
                else:
                    # Window expired
                    del _agent_conversations[workspace_id]
            else:
                # Different human speaking — mark interjection
                conv["interjected"] = True
        elif conv:
            # Addressed to someone else — break conversation
            del _agent_conversations[workspace_id]

        if should_route and self.container_id:
            _agent_tasks[workspace_id] = asyncio.create_task(
                _handle_agent_mention(workspace_id, self.container_id, text)
            )

    async def handle_chat_delete(self, msg: dict) -> None:
        workspace_id = self.workspace_id
        if not workspace_id:
            return
        message_id = msg.get("message_id", "")
        if not message_id:
            return
        deleted = await model.delete_chat_message(message_id, self.user["id"])
        if deleted:
            session = state.get_session(workspace_id)
            if session:
                session.broadcast(
                    {
                        "type": "chat_updated",
                        "message_id": message_id,
                        "message": "<message deleted by author>",
                    }
                )

    async def handle_chat_load_more(self, msg: dict) -> None:
        workspace_id = self.workspace_id
        if not workspace_id:
            return
        before_id = msg.get("before_id", "")
        if not before_id:
            return
        limit = min(msg.get("limit", 50), 100)
        messages = await model.get_chat_messages_before(
            workspace_id, before_id, limit
        )
        self.sock.send_json(
            {
                "type": "chat_history_page",
                "messages": messages,
                "has_more": len(messages) == limit,
            }
        )

    async def _start_agent_if_needed(  # pragma: no cover
        self, container_id: str
    ) -> None:
        """Start the Pi RPC agent so it shows in presence."""
        from . import agent

        try:
            session = await agent.get_session(container_id)
            await session._ensure_started()
            # Broadcast updated presence now that agent is alive
            if self.workspace_id:
                ws_session = state.get_session(self.workspace_id)
                if ws_session:
                    presence = _get_presence_list(self.workspace_id)
                    ws_session.broadcast(
                        {"type": "presence_list", "users": presence}
                    )
        except Exception:
            logger.debug("Failed to start agent eagerly", exc_info=True)

    async def handle_chat_agent_abort(self) -> None:  # pragma: no cover
        workspace_id = self.workspace_id
        if not workspace_id:
            return
        task = _agent_tasks.pop(workspace_id, None)
        if task and not task.done():
            task.cancel()

    async def handle_ui_ready(self) -> None:
        if self.workspace_id:
            sess = state.get_session(self.workspace_id)
            if sess:
                sess.browser_subscribers.add(self.sock)
        status_msg = self.pending_status_msg
        self.pending_status_msg = None
        if status_msg:
            _send_event(self.sock, "container_ready", status_msg)
        # Send shared terminal list from in-memory state.
        ws_session = state.get_session(self.workspace_id)
        if ws_session:
            logger.info(
                "ui_ready: terminal_windows keys=%s, subscribers=%d",
                list(ws_session.terminal_windows.keys()),
                len(ws_session.subscribers),
            )
            terminals = _get_shared_terminals(ws_session)
            logger.info("ui_ready: shared_terminals=%s", terminals)
            self.sock.send_json(
                {"type": "shared_terminals", "terminals": terminals}
            )

    async def handle_set_handle(self, msg: dict) -> None:
        handle = msg.get("handle", "").strip()
        if not self.workspace_id:
            send_error(self.sock, "Not connected to a workspace")
            return
        try:
            await model.set_user_handle(self.user["id"], handle)
            # Update the per-workspace symlink.
            workspace = self.workspace
            if workspace:
                owner_id = workspace.get("user_id", self.user["id"])
                workspace_home = workspaces.home_path(
                    owner_id, self.workspace_id
                )
                container_home = workspaces.ensure_home_symlink(
                    workspace_home, handle, self.user["id"]
                )
                self._user_home = container_home
            self.sock.send_json(
                {
                    "type": "handle_set",
                    "handle": handle,
                    "home": self._user_home,
                }
            )
        except ValueError as exc:
            self.sock.send_json(
                {
                    "type": "handle_error",
                    "error": str(exc),
                }
            )

    async def _claim_and_stop_terminal(self) -> None:
        session = self.terminal_session
        self.terminal_session = None
        if session is not None:
            await session.stop()

    async def _claim_and_stop_exec(self) -> None:
        session = self.exec_session
        self.exec_session = None
        if session is not None:
            await session.stop()

    async def stop_exec(self) -> None:
        task = self.exec_task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.exec_task = None
        await self._claim_and_stop_exec()

    async def forward_exec_output(self, session: ExecSession) -> None:
        """Forward exec stdout to the client via WebSocket as base64."""
        import base64

        try:
            async for data in session.output():
                self.sock.send_json(
                    {
                        "type": "exec_output",
                        "data": base64.b64encode(data).decode("ascii"),
                    }
                )
                if self.container_id:
                    container.registry.record_activity(self.container_id)
            # Process exited — send exit code
            self.sock.send_json(
                {
                    "type": "exec_exit",
                    "code": session.returncode
                    if session.returncode is not None
                    else 1,
                }
            )
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except _WS_ERRORS as e:
            logger.error("Exec output forwarding error: %s", e)
        finally:
            await self._claim_and_stop_exec()

    async def _activate_session(
        self, session: TerminalSession, cols: int, rows: int
    ) -> bool:
        """Wire up a started session for output forwarding.

        Checks the session is still current, creates the output task,
        resizes to force a tmux redraw, and records activity.
        Returns False if the session was superseded.
        """
        if self.terminal_session is not session:
            await session.stop()
            return False
        self.terminal_task = asyncio.create_task(
            self.forward_terminal_output(session)
        )
        # Resize to force tmux to redraw at the client's terminal size.
        # Without this, reattaching shows a blank screen because tmux
        # skips the redraw when the PTY size matches the default.
        await session.resize(cols, rows)
        container.registry.record_activity(self.container_id)
        return True

    async def stop_terminal(self) -> None:
        task = self.terminal_task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self.terminal_task = None
        await self._claim_and_stop_terminal()
        # Reset debounce so the next explicit start isn't blocked.
        self._last_terminal_start = 0

    async def forward_terminal_output(self, session: TerminalSession) -> None:
        """Forward terminal output to the frontend via WebSocket."""
        logger.info(
            "forward_terminal_output: starting for user=%s container=%s",
            self.user.get("email"),
            self.container_id,
        )
        try:
            async for data in session.output():
                self.sock.send_json({"type": "terminal_output", "data": data})
                if self.container_id:
                    container.registry.record_activity(self.container_id)
            # Stream ended — the tmux session exited (not necessarily the
            # container). Don't send container_stopped; the idle timeout
            # or shutdown button handles actual container death.
            logger.info(
                "forward_terminal_output: stream ended for user=%s",
                self.user.get("email"),
            )
        except asyncio.CancelledError:
            raise  # Normal cleanup, don't send event
        except _WS_ERRORS as e:
            logger.error("Terminal output forwarding error: %s", e)
            try:
                _send_event(self.sock, "container_stopped")
            except _WS_ERRORS:
                pass
        finally:
            await self._claim_and_stop_terminal()

    async def cleanup(self) -> None:
        # Remove idle callback
        workspace_id = self.workspace_id
        idle_cb = self._idle_cb
        if workspace_id and idle_cb:
            container.registry.remove_idle_callback(workspace_id, idle_cb)
            self._idle_cb = None

        # Revoke per-connection bridge tokens
        container.registry.revoke_connection_token(self.sock)
        self._bridge_token = None

        await self.stop_terminal()
        await self.stop_exec()

        # Remove this connection from the workspace session's subscriber sets.
        # If no subscribers remain, remove the session entirely. The container
        # is NOT killed — idle timeout handles that.
        session = state.get_session(workspace_id) if workspace_id else None
        if session:
            empty = await session.remove_subscriber(self.sock)
            if not empty:
                # Broadcast presence_leave if user has no other connections
                still_connected = any(
                    state.connections.get(s) is not None
                    and state.connections[s].user["id"] == self.user["id"]
                    for s in session.subscribers
                )
                if not still_connected:
                    # Broadcast a system chat message for the leave
                    sys_msg = await model.add_chat_message(
                        workspace_id,
                        self.user["id"],
                        self.user["email"],
                        f"{self.user['email']} left",
                        message_type=model.MSG_SYSTEM,
                    )
                    session.broadcast({"type": "chat_message", **sys_msg})
                    session.broadcast(
                        {
                            "type": "presence_leave",
                            "user_id": self.user["id"],
                            "user_email": self.user["email"],
                        }
                    )
            else:
                # Lock is released by remove_subscriber, so use the
                # lock-acquiring version.
                await state.remove_session(workspace_id)


# Per-workspace agent conversation state.
# user_id: who started the conversation
# time: monotonic timestamp of the last agent exchange
# interjected: True after a different human spoke
_agent_conversations: dict[str, dict] = {}

_AGENT_MENTION_RE = re.compile(
    r"(?:^|(?<=\s))@" + re.escape(model.AGENT_MENTION) + r"(?:@\S+)?(?:\s|$)",
    re.IGNORECASE,
)

_ANY_MENTION_RE = re.compile(r"(?:^|(?<=\s))@\S+")


def _mentions_agent(text: str) -> bool:
    """Return True if the message text mentions the agent."""
    return bool(_AGENT_MENTION_RE.search(text))


def _addresses_other_user(text: str) -> bool:
    """Return True if the message is directed at someone else.

    A message that *starts* with ``@someone`` (not the agent) is
    considered addressed to that person, breaking the follow-up
    conversation with the agent.
    """
    m = _ANY_MENTION_RE.match(text.lstrip())
    if not m:
        return False
    mention = m.group().lstrip("@").split("@")[0].lower()
    return mention != model.AGENT_MENTION.lower()


# Active agent tasks per workspace, for abort support.
_agent_tasks: dict[str, asyncio.Task] = {}


async def _handle_agent_mention(
    workspace_id: str, container_id: str, user_text: str
) -> None:
    """Handle an @misterboops mention by sending the prompt to Pi RPC."""
    from . import agent

    prompt = _AGENT_MENTION_RE.sub("", user_text).strip()
    if not prompt:
        prompt = "Hello!"

    # Include messages from OTHER users since the agent's last response
    # as context.  The current user's message is already the prompt;
    # we only need to show interjections from other participants that
    # Pi hasn't seen (since Pi's multi-turn history only has the
    # conversation between the mentioning user and itself).
    recent = await model.get_chat_messages(workspace_id, limit=50)
    chronological = list(reversed(recent))
    last_agent_idx = -1
    for i, m in enumerate(chronological):
        if m.get("message_type", 0) == model.MSG_AGENT:
            last_agent_idx = i
    # Messages from other users (not the current prompt sender)
    other_msgs = [
        m
        for m in chronological[last_agent_idx + 1 :]
        if m.get("message_type", 0) == model.MSG_USER
        and m.get("message", "").strip() != user_text.strip()
    ]
    if other_msgs:  # pragma: no cover
        context_lines = [
            f"{m.get('user_email', 'unknown')}: {m.get('message', '')}"
            for m in other_msgs
        ]
        context = "\n".join(context_lines)
        prompt = f"[Other participants said:\n{context}]\n\n{prompt}"

    # Notify clients the agent is thinking
    session = state.get_session(workspace_id)
    if session:
        session.broadcast(
            {
                "type": "agent_thinking",
                "thinking": True,
                "name": model.AGENT_EMAIL,
            }
        )

    try:
        pi = await agent.get_session(container_id)
        response_text = await pi.send_prompt(prompt)
    except asyncio.CancelledError:  # pragma: no cover
        response_text = "Stopped."
    except agent.AgentProcessDied:
        logger.warning("Agent process died for workspace %s", workspace_id)
        # Post system message about the crash
        sys_msg = await model.add_chat_message(
            workspace_id,
            model.AGENT_USER_ID,
            model.AGENT_EMAIL,
            f"{model.AGENT_EMAIL} has disconnected",
            message_type=model.MSG_SYSTEM,
        )
        session = state.get_session(workspace_id)
        if session:
            session.broadcast({"type": "agent_thinking", "thinking": False})
            session.broadcast({"type": "chat_message", **sys_msg})
        _agent_tasks.pop(workspace_id, None)
        return
    except Exception:
        logger.exception("Agent error for workspace %s", workspace_id)
        response_text = (
            "Sorry, I encountered an error processing your request."
        )

    agent_msg = await model.add_chat_message(
        workspace_id,
        model.AGENT_USER_ID,
        model.AGENT_EMAIL,
        response_text,
        message_type=model.MSG_AGENT,
    )
    session = state.get_session(workspace_id)
    if session:
        session.broadcast({"type": "agent_thinking", "thinking": False})
        session.broadcast({"type": "chat_message", **agent_msg})
    _agent_tasks.pop(workspace_id, None)


async def handle_websocket(websocket: WebSocket) -> None:
    """Main WebSocket handler."""
    # Authenticate via query param
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return

    user = await auth.get_user_from_token(token)
    if user is None:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await websocket.accept()
    safe_ws = SafeWebSocket(websocket)
    safe_ws.start_sender()
    conn = Connection(safe_ws, user)
    state.connections[safe_ws] = conn

    try:
        while True:
            raw = await safe_ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                send_error(safe_ws, "Invalid JSON")
                continue

            _log_ws_msg("RECV", msg, user)

            cmd = msg.get("cmd")
            if cmd == "workspace_connect":
                await conn.handle_workspace_connect(msg)
            elif cmd == "workspace_disconnect":
                await conn.handle_workspace_disconnect()
            elif cmd == "ui_ready":
                await conn.handle_ui_ready()
            elif cmd == "set_handle":
                await conn.handle_set_handle(msg)
            elif cmd == "terminal_start":
                await conn.handle_terminal_start(msg)
            elif cmd == "terminal_input":
                await conn.handle_terminal_input(msg)
            elif cmd == "terminal_resize":
                await conn.handle_terminal_resize(msg)
            elif cmd == "terminal_stop":
                await conn.handle_terminal_stop()
            elif cmd == "terminal_new_window":
                await conn.handle_terminal_new_window(msg)
            elif cmd == "terminal_select_window":
                await conn.handle_terminal_select_window(msg)
            elif cmd == "terminal_close_window":
                await conn.handle_terminal_close_window(msg)
            elif cmd == "terminal_rename_window":
                await conn.handle_terminal_rename_window(msg)
            elif cmd == "terminal_list_windows":
                await conn.handle_terminal_list_windows()
            elif cmd == "share_window":  # pragma: no cover
                await conn.handle_share_window(msg)
            elif cmd == "unshare_window":  # pragma: no cover
                await conn.handle_unshare_window(msg)
            elif cmd == "create_shared_terminal":  # pragma: no cover
                await conn.handle_create_shared_terminal(msg)
            elif cmd == "join_shared_terminal":  # pragma: no cover
                await conn.handle_join_shared_terminal(msg)
            elif cmd == "delete_shared_terminal":  # pragma: no cover
                await conn.handle_delete_shared_terminal(msg)
            elif cmd == "list_shared_terminals":  # pragma: no cover
                await conn.handle_list_shared_terminals()
            elif cmd == "restart_container":
                await conn.handle_restart_container()
            elif cmd == "shutdown_container":
                await conn.handle_shutdown_container()
            elif cmd == "exec_start":
                await conn.handle_exec_start(msg)
            elif cmd == "exec_input":
                await conn.handle_exec_input(msg)
            elif cmd == "exec_close_stdin":
                await conn.handle_exec_close_stdin()
            elif cmd == "exec_stop":
                await conn.handle_exec_stop()
            elif cmd == "heartbeat":
                await conn.handle_heartbeat()
            elif cmd == "chat_send":
                await conn.handle_chat_send(msg)
            elif cmd == "chat_delete":
                await conn.handle_chat_delete(msg)
            elif cmd == "chat_load_more":
                await conn.handle_chat_load_more(msg)
            elif cmd == "chat_agent_abort":  # pragma: no cover
                await conn.handle_chat_agent_abort()
            elif cmd == "browser_response":
                state.handle_browser_response(msg, safe_ws)
            elif cmd == "browser_chunk":
                state.handle_browser_chunk(msg, safe_ws)
            else:
                send_error(safe_ws, f"Unknown command: {cmd}")

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for user %s", user["email"])
    except SlowClientError:
        logger.warning("Slow client dropped for user %s", user["email"])
    except Exception as e:
        logger.exception("WebSocket error: %s", e)
    finally:
        await safe_ws.stop_sender()
        await conn.cleanup()
        # Container is intentionally left running — idle timeout will clean it up.
        # This allows instant reconnection when navigating back to the workspace.
        state.connections.pop(safe_ws, None)


def _broadcast_to_set(subscribers: set[SafeWebSocket], message: dict) -> int:
    """Send *message* to each socket in *subscribers*, removing dead ones.

    Returns the number of live subscribers the message was delivered to.
    """
    dead = []
    delivered = 0
    for sub in list(subscribers):
        try:
            sub.send_json(message)
            delivered += 1
        except _WS_ERRORS:
            dead.append(sub)
    for sub in dead:
        subscribers.discard(sub)
    return delivered


async def reset_workspace_state(workspace_id: str) -> None:
    """Thin wrapper for backward compatibility with external callers."""
    await state.reset_workspace(workspace_id)


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


def _log_ws_msg(direction: str, msg: dict, user: dict | None = None) -> None:
    """Log a WebSocket message for debugging (KLANGK_WS_DEBUG=1)."""
    if not _WS_DEBUG:
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
