"""WebSocket handler: auth, workspace routing, terminal/exec/bridge."""

import asyncio
import glob
import json
import logging
import uuid

from fastapi import WebSocket, WebSocketDisconnect

from . import auth, container_manager, workspace_manager
from .util import resolve_env_secret
from .exec_session import ExecSession
from .terminal_manager import TerminalSession

logger = logging.getLogger(__name__)

_WS_DEBUG = bool(resolve_env_secret("BARK_WS_DEBUG"))

# Active connections: ws -> {user, workspace_id, container_id, ...}
_connections: dict[WebSocket, dict] = {}


class WorkspaceSession:
    """Shared state for a single workspace.

    Created by the first WebSocket connection, cleaned up by the last.
    """

    def __init__(self, workspace_id: str):
        self.workspace_id = workspace_id
        self.container_id: str | None = None
        self.subscribers: set[WebSocket] = set()
        self.browser_subscribers: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def reset(self) -> None:
        self.subscribers.clear()
        self.browser_subscribers.clear()


# Active sessions keyed by workspace_id.
_sessions: dict[str, WorkspaceSession] = {}

# Pending browser-delegate requests: request_id -> asyncio.Future
_pending_browser_requests: dict[str, asyncio.Future] = {}


def get_session(workspace_id: str) -> WorkspaceSession | None:
    return _sessions.get(workspace_id)


def get_or_create_session(workspace_id: str) -> WorkspaceSession:
    if workspace_id not in _sessions:
        _sessions[workspace_id] = WorkspaceSession(workspace_id)
    return _sessions[workspace_id]


async def remove_session(workspace_id: str) -> None:
    session = _sessions.pop(workspace_id, None)
    if session:
        await session.reset()


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
    conn_state: dict = {
        "user": user,
        "container_id": None,
        "terminal_session": None,
        "terminal_task": None,
        "exec_session": None,
        "exec_task": None,
    }
    _connections[ws] = conn_state

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send_error(ws, "Invalid JSON")
                continue

            if _WS_DEBUG:
                _log_ws_msg("RECV", msg, user)

            cmd = msg.get("cmd")
            if cmd == "workspace_connect":
                await handle_workspace_connect(ws, conn_state, msg)
            elif cmd == "workspace_disconnect":
                await handle_workspace_disconnect(ws, conn_state)
            elif cmd == "ui_ready":
                status_msg = conn_state.pop("pending_status_msg", None)
                if status_msg:
                    await ws.send_json(
                        {
                            "type": "event",
                            "event": {
                                "type": "CUSTOM",
                                "name": "container_ready",
                                "value": {"reason": status_msg},
                            },
                        }
                    )
            elif cmd == "terminal_start":
                await handle_terminal_start(ws, conn_state, msg)
            elif cmd == "terminal_input":
                await handle_terminal_input(conn_state, msg)
            elif cmd == "terminal_resize":
                await handle_terminal_resize(conn_state, msg)
            elif cmd == "terminal_stop":
                await handle_terminal_stop(conn_state)
            elif cmd == "restart_container":
                await handle_restart_container(ws, conn_state)
            elif cmd == "exec_start":
                await handle_exec_start(ws, conn_state, msg)
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
                await send_error(ws, f"Unknown command: {cmd}")

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for user %s", user["email"])
    except Exception as e:
        logger.error("WebSocket error: %s", e)
    finally:
        await cleanup_connection(ws, conn_state)
        # Container is intentionally left running — idle timeout will clean it up.
        # This allows instant reconnection when navigating back to the workspace.
        _connections.pop(ws, None)


def derive_hosting_info(headers) -> tuple[str, str, str]:
    """Derive hosting hostname, proto, and base path from env vars or request headers.

    Returns (hostname, proto, base_path). Env vars take precedence over headers.
    Works with both Request.headers and WebSocket.headers.
    """
    hostname = resolve_env_secret("BARK_HOSTING_HOSTNAME")
    proto = resolve_env_secret("BARK_HOSTING_PROTO")
    base_path = resolve_env_secret("BARK_HOSTING_BASE_PATH")
    if not hostname:
        forwarded_host = headers.get("x-forwarded-host")
        if forwarded_host:
            # Behind an external reverse proxy — trust its hostname as-is
            hostname = forwarded_host
        else:
            # Direct access (local dev) — use nginx port for hosted app URLs
            nginx_port = resolve_env_secret("BARK_NGINX_PORT")
            host = headers.get("host") or "localhost"
            if nginx_port:
                host_no_port = host.split(":")[0]
                hostname = f"{host_no_port}:{nginx_port}"
            else:
                hostname = host
    if not proto:
        proto = headers.get("x-forwarded-proto") or "http"
    if base_path is None:
        base_path = headers.get("x-forwarded-prefix") or ""
    return hostname, proto, base_path


async def start_workspace_container(
    ws: WebSocket, state: dict, workspace_id: str, workspace: dict
) -> None:
    """Start/restart container for a workspace."""
    user = state["user"]
    host_path = str(
        workspace_manager.get_workspace_host_path(user["id"], workspace_id)
    )
    home_path = str(
        workspace_manager.get_home_host_path(user["id"], workspace_id)
    )

    # Find the most recent Pi session file to resume (if any).
    session_files = sorted(
        glob.glob(f"{home_path}/.pi/sessions/**/*.jsonl", recursive=True)
    )
    resume_session = None
    if session_files:
        most_recent = session_files[-1]
        resume_session = most_recent.replace(home_path, "/home/bark")

    hosting_hostname, hosting_proto, hosting_base_path = derive_hosting_info(
        ws.headers
    )
    (
        container_id,
        container_status,
    ) = await container_manager.registry.start_container(
        workspace_id,
        host_path,
        home_path,
        workspace.get("container_id"),
        resume_session=resume_session,
        num_ports=workspace.get(
            "num_ports", container_manager.DEFAULT_PORTS_PER_WORKSPACE
        ),
        hosting_hostname=hosting_hostname,
        hosting_proto=hosting_proto,
        hosting_base_path=hosting_base_path,
        image=workspace.get("image"),
    )
    state["container_status"] = container_status
    state["workspace_id"] = workspace_id
    state["container_id"] = container_id

    session = get_or_create_session(workspace_id)
    async with session.lock:
        container_manager.registry.add_connection(workspace_id)
        session.container_id = container_id
        session.subscribers.add(ws)

    # Register idle timeout notification (per-connection)
    async def on_idle(wid: str) -> None:
        try:
            await ws.send_json(
                {
                    "type": "event",
                    "event": {
                        "type": "CUSTOM",
                        "name": "container_stopped",
                        "value": {"reason": "idle timeout"},
                    },
                }
            )
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass

    state["_idle_cb"] = on_idle
    container_manager.registry.on_idle_stop(workspace_id, on_idle)

    # Cache workspace info for auto-restart
    state["workspace"] = workspace

    logger.info("Container ready for workspace %s", workspace_id)


async def handle_workspace_connect(
    ws: WebSocket, state: dict, msg: dict
) -> None:
    workspace_id = msg.get("workspaceId")
    if not workspace_id:
        await send_error(ws, "Missing workspaceId")
        return

    user = state["user"]
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        await send_error(ws, "Workspace not found")
        return

    # Disconnect from any current workspace
    await handle_workspace_disconnect(ws, state)

    await start_workspace_container(ws, state, workspace_id, workspace)

    ports = await container_manager.registry.get_workspace_ports(workspace_id)
    status = state.get("container_status", "created")
    container_name = (
        f"bark-{container_manager.INSTANCE_ID}-{workspace_id[:12]}"
    )
    ports_str = f" (ports {','.join(str(p) for p in ports)})" if ports else ""
    status_msg = {
        "connected": f"Connected to running container {container_name}{ports_str}",
        "restarted": f"Restarted stopped container {container_name}{ports_str}",
        "created": f"Created new container {container_name}{ports_str}",
    }.get(status, "Container ready")

    timeout_mins = container_manager.IDLE_TIMEOUT_SECONDS / 60
    if timeout_mins == int(timeout_mins):
        status_msg += f" — idle timeout: {int(timeout_mins)}m"
    else:
        status_msg += f" — idle timeout: {timeout_mins:.1f}m"

    await ws.send_json(
        {
            "type": "workspace_ready",
            "workspaceId": workspace_id,
            "ports": ports,
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


async def handle_workspace_disconnect(ws: WebSocket, state: dict) -> None:
    await cleanup_connection(ws, state)
    state["workspace_id"] = None
    state["container_id"] = None


async def handle_restart_container(ws: WebSocket, state: dict) -> None:
    """Restart a stopped container (e.g., after idle timeout)."""
    workspace_id = state.get("workspace_id")
    if not workspace_id:
        await send_error(ws, "Not connected to a workspace")
        return

    # Save before cleanup — cleanup_connection clears state fields.
    user = state["user"]
    workspace = state.get("workspace")

    await ws.send_json(
        {
            "type": "event",
            "event": {
                "type": "CUSTOM",
                "name": "container_restart",
                "value": {"reason": "Restarting container..."},
            },
        }
    )

    try:
        await cleanup_connection(ws, state)
    except (RuntimeError, OSError, ConnectionError) as e:
        logger.warning("Cleanup error during restart: %s", e)

    if workspace is None:
        workspace = await workspace_manager.get_workspace(
            workspace_id, user["id"]
        )
    if workspace is None:
        await send_error(ws, "Workspace not found")
        return

    await start_workspace_container(ws, state, workspace_id, workspace)
    container_manager.registry.record_activity(state["container_id"])

    ports = await container_manager.registry.get_workspace_ports(workspace_id)
    ports_str = f" (ports {','.join(str(p) for p in ports)})" if ports else ""
    container_name = (
        f"bark-{container_manager.INSTANCE_ID}-{workspace_id[:12]}"
    )
    status_msg = f"Container restarted {container_name}{ports_str}"

    timeout_mins = container_manager.IDLE_TIMEOUT_SECONDS / 60
    if timeout_mins == int(timeout_mins):
        status_msg += f" — idle timeout: {int(timeout_mins)}m"
    else:
        status_msg += f" — idle timeout: {timeout_mins:.1f}m"

    await ws.send_json(
        {
            "type": "event",
            "event": {
                "type": "CUSTOM",
                "name": "container_ready",
                "value": {"reason": status_msg},
            },
        }
    )

    logger.info(
        "Container restarted via restart_container command for workspace %s",
        workspace_id,
    )


async def handle_terminal_start(ws: WebSocket, state: dict, msg: dict) -> None:
    container_id = state.get("container_id")
    if not container_id:
        return
    # Stop existing terminal if any
    await stop_terminal(state)
    cols = msg.get("cols", 80)
    rows = msg.get("rows", 24)
    session = TerminalSession(container_id)
    await session.start(cols, rows)
    state["terminal_session"] = session
    state["terminal_task"] = asyncio.create_task(
        forward_terminal_output(ws, session, state)
    )
    # Clear the screen to hide the double-prompt on startup.
    await ws.send_json({"type": "terminal_output", "data": "\x1b[2J\x1b[H"})
    container_manager.registry.record_activity(container_id)


async def handle_terminal_input(state: dict, msg: dict) -> None:
    session: TerminalSession | None = state.get("terminal_session")
    if session is None or not session.is_alive:
        return
    container_manager.registry.record_activity(state["container_id"])
    await session.write(msg.get("data", ""))


async def handle_terminal_resize(state: dict, msg: dict) -> None:
    session: TerminalSession | None = state.get("terminal_session")
    if session is None:
        return
    await session.resize(msg.get("cols", 80), msg.get("rows", 24))


async def handle_terminal_stop(state: dict) -> None:
    await stop_terminal(state)


async def handle_exec_start(ws: WebSocket, state: dict, msg: dict) -> None:
    container_id = state.get("container_id")
    if not container_id:
        return
    await stop_exec(state)
    command = msg.get("command", [])
    if not command:
        await send_error(ws, "exec_start requires a command list")
        return
    session = ExecSession(container_id)
    await session.start(command)
    state["exec_session"] = session
    state["exec_task"] = asyncio.create_task(
        forward_exec_output(ws, session, state)
    )
    container_manager.registry.record_activity(container_id)


async def handle_exec_input(state: dict, msg: dict) -> None:
    session: ExecSession | None = state.get("exec_session")
    if session is None or not session.is_alive:
        return
    container_manager.registry.record_activity(state["container_id"])
    import base64

    raw = base64.b64decode(msg.get("data", ""))
    await session.write(raw)


async def handle_exec_close_stdin(state: dict) -> None:
    session: ExecSession | None = state.get("exec_session")
    if session is None:
        return
    await session.close_stdin()


async def handle_exec_stop(state: dict) -> None:
    await stop_exec(state)


async def handle_heartbeat(state: dict) -> None:
    container_id = state.get("container_id")
    if container_id is not None:
        container_manager.registry.record_activity(container_id)


def handle_browser_response(msg: dict) -> None:
    """Resolve a pending browser-delegate request."""
    request_id = msg.get("id")
    if not request_id:
        return
    future = _pending_browser_requests.pop(request_id, None)
    if future and not future.done():
        future.set_result(msg)
    elif request_id:
        logger.debug(
            "Browser response for unknown/completed request %s", request_id
        )


async def dispatch_browser_request(
    workspace_id: str, request: dict, timeout: float = 30.0
) -> dict:
    """Send a browser_request to all workspace subscribers and wait for the response.

    Called by the /api/browser-delegate HTTP endpoint. Holds the connection
    open until a browser_response arrives or the timeout expires.
    """
    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    _pending_browser_requests[request_id] = future

    session = get_session(workspace_id)
    if not session or not session.subscribers:
        _pending_browser_requests.pop(request_id, None)
        return {"error": "No browser client connected to this workspace"}

    message = {
        **request,
        "type": "browser_request",
        "id": request_id,
    }
    delivered = await _broadcast(workspace_id, message)
    if delivered == 0:
        _pending_browser_requests.pop(request_id, None)
        return {"error": "No browser client connected to this workspace"}

    try:
        result = await asyncio.wait_for(future, timeout=timeout)
        return result
    except asyncio.TimeoutError:
        _pending_browser_requests.pop(request_id, None)
        return {"error": "Browser client did not respond within timeout"}
    except asyncio.CancelledError:
        _pending_browser_requests.pop(request_id, None)
        raise


async def stop_exec(state: dict) -> None:
    task = state.get("exec_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        state["exec_task"] = None
    session: ExecSession | None = state.get("exec_session")
    if session:
        await session.stop()
        state["exec_session"] = None


async def forward_exec_output(
    ws: WebSocket, session: ExecSession, state: dict
) -> None:
    """Forward exec stdout to the client via WebSocket as base64."""
    import base64

    try:
        async for data in session.output():
            await ws.send_json(
                {
                    "type": "exec_output",
                    "data": base64.b64encode(data).decode("ascii"),
                }
            )
            container_id = state.get("container_id")
            if container_id:
                container_manager.registry.record_activity(container_id)
        # Process exited — send exit code
        await ws.send_json(
            {
                "type": "exec_exit",
                "code": session.returncode
                if session.returncode is not None
                else 1,
            }
        )
    except asyncio.CancelledError:  # pragma: no cover
        raise
    except (OSError, WebSocketDisconnect, RuntimeError, ConnectionError) as e:
        logger.error("Exec output forwarding error: %s", e)


async def stop_terminal(state: dict) -> None:
    task = state.get("terminal_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        state["terminal_task"] = None
    session: TerminalSession | None = state.get("terminal_session")
    if session:
        await session.stop()
        state["terminal_session"] = None


async def forward_terminal_output(
    ws: WebSocket, session: TerminalSession, state: dict
) -> None:
    """Forward terminal output to the frontend via WebSocket."""
    try:
        async for data in session.output():
            await ws.send_json({"type": "terminal_output", "data": data})
            container_id = state.get("container_id")
            if container_id:
                container_manager.registry.record_activity(container_id)
        # Stream ended without cancellation — container likely died
        await ws.send_json(
            {
                "type": "event",
                "event": {
                    "type": "CUSTOM",
                    "name": "container_stopped",
                    "value": {},
                },
            }
        )
    except asyncio.CancelledError:
        raise  # Normal cleanup, don't send event
    except (OSError, WebSocketDisconnect, RuntimeError, ConnectionError) as e:
        logger.error("Terminal output forwarding error: %s", e)
        try:
            await ws.send_json(
                {
                    "type": "event",
                    "event": {
                        "type": "CUSTOM",
                        "name": "container_stopped",
                        "value": {},
                    },
                }
            )
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass


async def _broadcast(workspace_id: str, message: dict) -> int:
    """Send a message to all subscribers for a workspace, removing dead ones.

    Returns the number of live subscribers the message was delivered to.
    """
    if _WS_DEBUG:
        _log_ws_msg("BCAST", message)
    session = get_session(workspace_id)
    if not session:  # pragma: no cover
        return 0
    dead = []
    delivered = 0
    for sub_ws in list(session.subscribers):
        try:
            await sub_ws.send_json(message)
            delivered += 1
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            dead.append(sub_ws)
    for sub_ws in dead:
        session.subscribers.discard(sub_ws)
    return delivered


async def cleanup_connection(ws: WebSocket, state: dict) -> None:
    # Remove idle callback
    workspace_id = state.get("workspace_id")
    idle_cb = state.get("_idle_cb")
    if workspace_id and idle_cb:
        container_manager.registry.remove_idle_callback(workspace_id, idle_cb)
        state["_idle_cb"] = None

    await stop_terminal(state)
    await stop_exec(state)

    # Remove this WebSocket from subscribers
    session = get_session(workspace_id) if workspace_id else None
    if session:
        session.subscribers.discard(ws)

    # Decrement connection refcount. Only the last connection to disconnect
    # destroys the container.
    remaining = 0
    if workspace_id:
        remaining = container_manager.registry.remove_connection(workspace_id)

    if remaining == 0 and workspace_id:
        await remove_session(workspace_id)

        container_id = state.get("container_id")
        if container_id:
            await container_manager.registry.stop_and_remove_container(
                container_id
            )


async def reset_workspace_state(workspace_id: str) -> None:
    """Clean up shared state for a workspace.

    Called when a container is killed externally (idle timeout,
    manual stop) so the next workspace_connect starts fresh.
    """
    await remove_session(workspace_id)
    container_manager.registry.remove_state(workspace_id)
    logger.info("Reset workspace state for %s", workspace_id)


async def send_error(ws: WebSocket, message: str) -> None:
    msg = {"type": "error", "message": message}
    if _WS_DEBUG:
        _log_ws_msg("SEND", msg)
    await ws.send_json(msg)


def _log_ws_msg(direction: str, msg: dict, user: dict | None = None) -> None:
    """Log a WebSocket message for debugging (BARK_WS_DEBUG=1)."""
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
