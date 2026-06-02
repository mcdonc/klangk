"""WebSocket handler: auth, workspace routing, terminal/exec/bridge."""

import asyncio
import json
import logging
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from . import auth, container, workspaces
from .util import derive_hosting_info, resolve_env_secret
from .dockerexec import ExecSession
from .terminal import TerminalSession

logger = logging.getLogger(__name__)

_WS_DEBUG = bool(resolve_env_secret("KLANGK_WS_DEBUG"))

# Max size for terminal/exec input data (base64-decoded bytes).
_MAX_INPUT_SIZE = 65536

# Max outbound messages before we declare the client too slow and close.
_SEND_QUEUE_SIZE = 256


class SlowClientError(Exception):
    """Raised when the outbound queue is full (client can't keep up)."""


class SafeWebSocket:
    """Bounded-queue WebSocket writer.

    All outbound messages are placed on a bounded asyncio.Queue.
    A dedicated sender task drains the queue and writes to the
    underlying WebSocket, serializing concurrent sends.  If the
    queue is full the client is too slow — we drop it immediately
    rather than blocking the read loop or forwarder tasks.
    """

    def __init__(self, ws: WebSocket, *, maxsize: int = _SEND_QUEUE_SIZE):
        self._ws = ws
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
                await self._ws.send_json(msg)
        except asyncio.CancelledError:
            raise
        except (WebSocketDisconnect, RuntimeError, ConnectionError, OSError):
            # Socket gone — nothing to do, cleanup_connection handles the rest.
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
        await self._ws.accept()

    async def receive_text(self) -> str:
        return await self._ws.receive_text()

    async def close(self, code: int = 1000) -> None:
        await self._ws.close(code=code)

    @property
    def headers(self):
        """Proxy header access to the underlying WebSocket."""
        return self._ws.headers

    @property
    def raw(self) -> WebSocket:
        """Access the underlying WebSocket (e.g. for identity checks)."""
        return self._ws


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

    async def reset(self) -> None:
        self.subscribers.clear()
        self.browser_subscribers.clear()

    async def add_subscriber(
        self, ws: SafeWebSocket, container_id: str
    ) -> None:
        """Register a connection as a subscriber (acquires lock)."""
        async with self.lock:
            self.container_id = container_id
            self.subscribers.add(ws)

    async def remove_subscriber(self, ws: SafeWebSocket) -> bool:
        """Unregister a connection (acquires lock).

        Returns True if no subscribers remain (session should be removed).
        """
        async with self.lock:
            self.subscribers.discard(ws)
            self.browser_subscribers.discard(ws)
            return not self.subscribers

    def broadcast(self, message: dict) -> int:
        """Send message to all subscribers, removing dead ones."""
        return _broadcast_to_set(self.subscribers, message)

    def broadcast_to_browsers(self, message: dict) -> int:
        """Send message to browser subscribers only, removing dead ones."""
        return _broadcast_to_set(self.browser_subscribers, message)


class State:
    """Module-level singleton holding mutable WebSocket handler state."""

    def __init__(self) -> None:
        # Active connections: ws -> {user, workspace_id, container_id, ...}
        self.connections: dict[SafeWebSocket, dict] = {}
        # Active sessions keyed by workspace_id.
        self.sessions: dict[str, WorkspaceSession] = {}
        # Pending browser-delegate requests: request_id -> asyncio.Future
        self.pending_browser_requests: dict[str, asyncio.Future] = {}

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


state = State()


async def handle_websocket(ws: WebSocket) -> None:
    """Main WebSocket handler."""
    # Authenticate via query param
    token = ws.query_params.get("token")
    if not token:
        await ws.close(code=4001, reason="Missing token")
        return

    user = await auth.get_user_from_token(token)
    if user is None:
        await ws.close(code=4001, reason="Invalid token")
        return

    await ws.accept()
    safe_ws = SafeWebSocket(ws)
    safe_ws.start_sender()
    conn_state: dict = {
        "user": user,
        "container_id": None,
        "terminal_session": None,
        "terminal_task": None,
        "dockerexec": None,
        "exec_task": None,
    }
    state.connections[safe_ws] = conn_state

    try:
        while True:
            raw = await safe_ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                send_error(safe_ws, "Invalid JSON")
                continue

            if _WS_DEBUG:
                _log_ws_msg("RECV", msg, user)

            cmd = msg.get("cmd")
            if cmd == "workspace_connect":
                await handle_workspace_connect(safe_ws, conn_state, msg)
            elif cmd == "workspace_disconnect":
                await handle_workspace_disconnect(safe_ws, conn_state)
            elif cmd == "ui_ready":
                # Mark this connection as a browser (Flutter) client.
                # CLI connections never send ui_ready.
                wid = conn_state.get("workspace_id")
                if wid:
                    sess = state.get_session(wid)
                    if sess:
                        sess.browser_subscribers.add(safe_ws)
                status_msg = conn_state.pop("pending_status_msg", None)
                if status_msg:
                    _send_event(safe_ws, "container_ready", status_msg)
            elif cmd == "terminal_start":
                await handle_terminal_start(safe_ws, conn_state, msg)
            elif cmd == "terminal_input":
                await handle_terminal_input(conn_state, msg)
            elif cmd == "terminal_resize":
                await handle_terminal_resize(conn_state, msg)
            elif cmd == "terminal_stop":
                await handle_terminal_stop(conn_state)
            elif cmd == "restart_container":
                await handle_restart_container(safe_ws, conn_state)
            elif cmd == "exec_start":
                await handle_exec_start(safe_ws, conn_state, msg)
            elif cmd == "exec_input":
                await handle_exec_input(conn_state, msg)
            elif cmd == "exec_close_stdin":
                await handle_exec_close_stdin(conn_state)
            elif cmd == "exec_stop":
                await handle_exec_stop(conn_state)
            elif cmd == "heartbeat":
                await handle_heartbeat(conn_state)
            elif cmd == "browser_response":
                handle_browser_response(msg)
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
        await cleanup_connection(safe_ws, conn_state)
        # Container is intentionally left running — idle timeout will clean it up.
        # This allows instant reconnection when navigating back to the workspace.
        state.connections.pop(safe_ws, None)


async def start_workspace_container(
    ws: SafeWebSocket, conn_state: dict, workspace_id: str, workspace: dict
) -> None:
    """Start/restart container for a workspace."""
    user = conn_state["user"]
    host_path = str(
        workspaces.get_workspace_host_path(user["id"], workspace_id)
    )
    home_path = str(workspaces.get_home_host_path(user["id"], workspace_id))
    cfg_path = str(workspaces.get_config_host_path(user["id"], workspace_id))

    hosting_hostname, hosting_proto, hosting_base_path = derive_hosting_info(
        ws.headers
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
    )
    conn_state["container_status"] = container_status
    conn_state["workspace_id"] = workspace_id
    conn_state["container_id"] = container_id

    session = state.get_or_create_session(workspace_id)
    await session.add_subscriber(ws, container_id)

    # Register idle timeout notification (per-connection)
    async def on_idle(wid: str) -> None:
        try:
            _send_event(ws, "container_stopped", "idle timeout")
        except (
            SlowClientError,
            WebSocketDisconnect,
            RuntimeError,
            ConnectionError,
        ):
            pass

    conn_state["_idle_cb"] = on_idle
    # No await between lock release and callback registration — the idle
    # loop cannot interleave here in asyncio's single-threaded model.
    # If an await is added before on_idle_stop, move registration inside the lock.
    container.registry.on_idle_stop(workspace_id, on_idle)

    # Cache workspace info for auto-restart
    conn_state["workspace"] = workspace

    # Clear any stale pending_status_msg from a prior connect/restart.
    conn_state.pop("pending_status_msg", None)

    logger.info("Container ready for workspace %s", workspace_id)


async def handle_workspace_connect(
    ws: SafeWebSocket, state: dict, msg: dict
) -> None:
    workspace_id = msg.get("workspaceId")
    if not workspace_id:
        send_error(ws, "Missing workspaceId")
        return

    user = state["user"]
    workspace = await workspaces.get_workspace(workspace_id, user["id"])
    if workspace is None:
        send_error(ws, "Workspace not found")
        return

    # Disconnect from any current workspace
    await handle_workspace_disconnect(ws, state)

    await start_workspace_container(ws, state, workspace_id, workspace)

    ports = await container.registry.get_workspace_ports(workspace_id)
    status = state.get("container_status", "created")
    container_name, ports_str = _format_container_info(workspace_id, ports)
    status_msg = {
        "connected": f"Connected to running container {container_name}{ports_str}",
        "restarted": f"Restarted stopped container {container_name}{ports_str}",
        "created": f"Created new container {container_name}{ports_str}",
    }.get(status, "Container ready")

    status_msg += _format_idle_timeout(container.IDLE_TIMEOUT_SECONDS)

    ws.send_json(
        {
            "type": "workspace_ready",
            "workspaceId": workspace_id,
            "ports": ports,
            "defaultCommand": workspace.get("default_command"),
        }
    )
    # Store status for when frontend sends ui_ready
    state["pending_status_msg"] = status_msg
    logger.info(
        "User %s connected to workspace %s (ports %s)",
        state["user"]["email"],
        workspace_id,
        ports,
    )


async def handle_workspace_disconnect(ws: SafeWebSocket, state: dict) -> None:
    await cleanup_connection(ws, state)
    state["workspace_id"] = None
    state["container_id"] = None


async def handle_restart_container(ws: SafeWebSocket, state: dict) -> None:
    """Restart a stopped container (e.g., after idle timeout)."""
    workspace_id = state.get("workspace_id")
    if not workspace_id:
        send_error(ws, "Not connected to a workspace")
        return

    # Save before cleanup — cleanup_connection clears state fields.
    user = state["user"]
    workspace = state.get("workspace")

    _send_event(ws, "container_restart", "Restarting container...")

    try:
        await cleanup_connection(ws, state)
    except (WebSocketDisconnect, RuntimeError, OSError, ConnectionError) as e:
        logger.warning("Cleanup error during restart: %s", e)

    if workspace is None:
        workspace = await workspaces.get_workspace(workspace_id, user["id"])
    if workspace is None:
        send_error(ws, "Workspace not found")
        return

    await start_workspace_container(ws, state, workspace_id, workspace)
    container.registry.record_activity(state["container_id"])

    ports = await container.registry.get_workspace_ports(workspace_id)
    container_name, ports_str = _format_container_info(workspace_id, ports)
    status_msg = f"Container restarted {container_name}{ports_str}"

    timeout_mins = container.IDLE_TIMEOUT_SECONDS / 60
    if timeout_mins == int(timeout_mins):
        status_msg += f" — idle timeout: {int(timeout_mins)}m"
    else:
        status_msg += f" — idle timeout: {timeout_mins:.1f}m"

    _send_event(ws, "container_ready", status_msg)

    logger.info(
        "Container restarted via restart_container command for workspace %s",
        workspace_id,
    )


async def handle_terminal_start(
    ws: SafeWebSocket, state: dict, msg: dict
) -> None:
    container_id = state.get("container_id")
    if not container_id:
        return
    # Stop existing terminal if any
    await stop_terminal(state)
    cols = msg.get("cols", 80)
    rows = msg.get("rows", 24)
    command_override = msg.get("commandOverride")
    session = TerminalSession(container_id)

    # Store session immediately so stop_terminal can clean it up
    # if another terminal_start arrives before this one finishes.
    state["terminal_session"] = session

    async def _start_terminal() -> None:
        try:
            await session.start(cols, rows, command_override=command_override)
            # Check we're still the active session — stop_terminal may have
            # replaced us while session.start() was awaited.
            if state.get("terminal_session") is not session:
                await session.stop()
                return
            state["terminal_task"] = asyncio.create_task(
                forward_terminal_output(ws, session, state)
            )
            container.registry.record_activity(container_id)
            ws.send_json({"type": "terminal_started"})
        except asyncio.CancelledError:
            await session.stop()
            raise
        except Exception as e:
            await session.stop()
            logger.exception("Terminal start failed: %s", e)
            send_error(ws, f"Terminal start failed: {e}")

    state["terminal_task"] = asyncio.create_task(_start_terminal())


async def handle_terminal_input(state: dict, msg: dict) -> None:
    session: TerminalSession | None = state.get("terminal_session")
    if session is None or not session.is_alive:
        return
    data = msg.get("data", "")
    if len(data) > _MAX_INPUT_SIZE:
        logger.warning(
            "terminal_input too large (%d bytes), dropping", len(data)
        )
        return
    container.registry.record_activity(state["container_id"])
    await session.write(data)


async def handle_terminal_resize(state: dict, msg: dict) -> None:
    session: TerminalSession | None = state.get("terminal_session")
    if session is None:
        return
    await session.resize(msg.get("cols", 80), msg.get("rows", 24))


async def handle_terminal_stop(state: dict) -> None:
    await stop_terminal(state)


async def handle_exec_start(ws: SafeWebSocket, state: dict, msg: dict) -> None:
    container_id = state.get("container_id")
    if not container_id:
        return
    await stop_exec(state)
    command = msg.get("command", [])
    if not command:
        send_error(ws, "exec_start requires a command list")
        return
    session = ExecSession(container_id)
    await session.start(command)
    state["dockerexec"] = session
    state["exec_task"] = asyncio.create_task(
        forward_exec_output(ws, session, state)
    )
    container.registry.record_activity(container_id)


async def handle_exec_input(state: dict, msg: dict) -> None:
    session: ExecSession | None = state.get("dockerexec")
    if session is None or not session.is_alive:
        return
    import base64

    raw = base64.b64decode(msg.get("data", ""))
    if len(raw) > _MAX_INPUT_SIZE:
        logger.warning("exec_input too large (%d bytes), dropping", len(raw))
        return
    container.registry.record_activity(state["container_id"])
    await session.write(raw)


async def handle_exec_close_stdin(state: dict) -> None:
    session: ExecSession | None = state.get("dockerexec")
    if session is None:
        return
    await session.close_stdin()


async def handle_exec_stop(state: dict) -> None:
    await stop_exec(state)


async def handle_heartbeat(state: dict) -> None:
    container_id = state.get("container_id")
    if container_id is not None:
        container.registry.record_activity(container_id)


def handle_browser_response(msg: dict) -> None:
    """Resolve a pending browser-delegate request."""
    request_id = msg.get("id")
    if not request_id:
        return
    future = state.pending_browser_requests.pop(request_id, None)
    if future and not future.done():
        future.set_result(msg)
    elif request_id:
        logger.debug(
            "Browser response for unknown/completed request %s", request_id
        )


async def dispatch_browser_request(
    workspace_id: str, request: dict, timeout: float = 30.0
) -> dict:
    """Send a browser_request to browser (Flutter) subscribers and wait for the response.

    Called by the /api/browser-delegate HTTP endpoint. Holds the connection
    open until a browser_response arrives or the timeout expires.
    Only sends to browser_subscribers (connections that sent ui_ready),
    not CLI connections which can't handle browser requests.
    """
    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    state.pending_browser_requests[request_id] = future

    session = state.get_session(workspace_id)
    if not session or not session.browser_subscribers:
        state.pending_browser_requests.pop(request_id, None)
        return {"error": "No browser client connected to this workspace"}

    message = {
        **request,
        "type": "browser_request",
        "id": request_id,
    }
    delivered = await _broadcast_to_browsers(workspace_id, message)
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


async def _claim_and_stop(state: dict, key: str) -> None:
    """Atomically remove a session from state and stop it.

    dict.pop() under the GIL ensures only one caller claims the
    session, preventing concurrent double-stop between forwarders
    and stop functions.
    """
    session = state.pop(key, None)
    if session is not None:
        await session.stop()


async def stop_exec(state: dict) -> None:
    task = state.get("exec_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        state["exec_task"] = None
    await _claim_and_stop(state, "dockerexec")


async def forward_exec_output(
    ws: SafeWebSocket, session: ExecSession, state: dict
) -> None:
    """Forward exec stdout to the client via WebSocket as base64."""
    import base64

    try:
        async for data in session.output():
            ws.send_json(
                {
                    "type": "exec_output",
                    "data": base64.b64encode(data).decode("ascii"),
                }
            )
            container_id = state.get("container_id")
            if container_id:
                container.registry.record_activity(container_id)
        # Process exited — send exit code
        ws.send_json(
            {
                "type": "exec_exit",
                "code": session.returncode
                if session.returncode is not None
                else 1,
            }
        )
    except asyncio.CancelledError:  # pragma: no cover
        raise
    except (
        SlowClientError,
        OSError,
        WebSocketDisconnect,
        RuntimeError,
        ConnectionError,
    ) as e:
        logger.error("Exec output forwarding error: %s", e)
    finally:
        await _claim_and_stop(state, "dockerexec")


async def stop_terminal(state: dict) -> None:
    task = state.get("terminal_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        state["terminal_task"] = None
    await _claim_and_stop(state, "terminal_session")


async def forward_terminal_output(
    ws: SafeWebSocket, session: TerminalSession, state: dict
) -> None:
    """Forward terminal output to the frontend via WebSocket."""
    try:
        async for data in session.output():
            ws.send_json({"type": "terminal_output", "data": data})
            container_id = state.get("container_id")
            if container_id:
                container.registry.record_activity(container_id)
        # Stream ended without cancellation — container likely died
        _send_event(ws, "container_stopped")
    except asyncio.CancelledError:
        raise  # Normal cleanup, don't send event
    except (
        SlowClientError,
        OSError,
        WebSocketDisconnect,
        RuntimeError,
        ConnectionError,
    ) as e:
        logger.error("Terminal output forwarding error: %s", e)
        try:
            _send_event(ws, "container_stopped")
        except (
            SlowClientError,
            WebSocketDisconnect,
            RuntimeError,
            ConnectionError,
        ):
            pass
    finally:
        await _claim_and_stop(state, "terminal_session")


def _broadcast_to_set(subscribers: set[SafeWebSocket], message: dict) -> int:
    """Send *message* to each socket in *subscribers*, removing dead ones.

    Returns the number of live subscribers the message was delivered to.
    """
    dead = []
    delivered = 0
    for sub_ws in list(subscribers):
        try:
            sub_ws.send_json(message)
            delivered += 1
        except (
            SlowClientError,
            WebSocketDisconnect,
            RuntimeError,
            ConnectionError,
        ):
            dead.append(sub_ws)
    for sub_ws in dead:
        subscribers.discard(sub_ws)
    return delivered


async def _broadcast(workspace_id: str, message: dict) -> int:
    """Send a message to all subscribers for a workspace."""
    if _WS_DEBUG:
        _log_ws_msg("BCAST", message)
    session = state.get_session(workspace_id)
    if not session:  # pragma: no cover
        return 0
    return session.broadcast(message)


async def _broadcast_to_browsers(workspace_id: str, message: dict) -> int:
    """Send a message to browser (Flutter) subscribers only."""
    if _WS_DEBUG:
        _log_ws_msg("BCAST", message)
    session = state.get_session(workspace_id)
    if not session:  # pragma: no cover
        return 0
    return session.broadcast_to_browsers(message)


async def cleanup_connection(ws: SafeWebSocket, conn_state: dict) -> None:
    # Remove idle callback
    workspace_id = conn_state.get("workspace_id")
    idle_cb = conn_state.get("_idle_cb")
    if workspace_id and idle_cb:
        container.registry.remove_idle_callback(workspace_id, idle_cb)
        conn_state["_idle_cb"] = None

    await stop_terminal(conn_state)
    await stop_exec(conn_state)

    # Remove this connection from the workspace session's subscriber sets.
    # If no subscribers remain, remove the session entirely. The container
    # is NOT killed — idle timeout handles that.
    session = state.get_session(workspace_id) if workspace_id else None
    if session:
        empty = await session.remove_subscriber(ws)
        if empty:
            # Lock is released by remove_subscriber, so use the
            # lock-acquiring version.
            await state.remove_session(workspace_id)


async def reset_workspace_state(workspace_id: str) -> None:
    """Clean up shared state for a workspace.

    Called when a container is killed externally (idle timeout,
    manual stop) so the next workspace_connect starts fresh.
    """
    await state.remove_session(workspace_id)
    container.registry.remove_state(workspace_id)
    logger.info("Reset workspace state for %s", workspace_id)


def _send_event(
    ws: SafeWebSocket, name: str, reason: str | None = None
) -> None:
    """Send a CUSTOM event (container_ready, container_stopped, etc.)."""
    value = {"reason": reason} if reason else {}
    ws.send_json(
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


def send_error(ws: SafeWebSocket, message: str) -> None:
    msg = {"type": "error", "message": message}
    if _WS_DEBUG:
        _log_ws_msg("SEND", msg)
    ws.send_json(msg)


def _log_ws_msg(direction: str, msg: dict, user: dict | None = None) -> None:
    """Log a WebSocket message for debugging (KLANGK_WS_DEBUG=1)."""
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
