"""WebSocket handler: auth, workspace routing, AG-UI event streaming."""

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from . import auth, container_manager, user_store, workspace_manager
from .env_util import resolve_env_secret
from .agui_translator import translate_event
from .pi_rpc_client import PiRpcClient
from .exec_session import ExecSession
from .terminal_manager import TerminalSession

logger = logging.getLogger(__name__)

# Active connections: ws -> {user, workspace_id, container_id, ...}
_connections: dict[WebSocket, dict] = {}
# Shared per-workspace state: workspace_id -> {pi_client, event_task, subscribers}
# Owned by the first connection, cleaned up by the last.
# subscribers is a set of WebSockets that receive Pi AG-UI events.
_workspace_state: dict[str, dict] = {}
# Serializes workspace_connect for the same workspace to prevent races
# where two connections both see conn_num==1 and both try to start Pi.
_workspace_connect_lock: asyncio.Lock = asyncio.Lock()


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
        "pi_client": None,
        "container_id": None,
        "event_task": None,
        "agent_running": False,
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

            cmd = msg.get("cmd")
            if cmd == "workspace_connect":
                await handle_workspace_connect(ws, conn_state, msg)
            elif cmd == "workspace_disconnect":
                await handle_workspace_disconnect(ws, conn_state)
            elif cmd == "prompt":
                await handle_prompt(ws, conn_state, msg)
            elif cmd == "steer":
                await handle_steer(conn_state, msg)
            elif cmd == "follow_up":
                await handle_follow_up(conn_state, msg)
            elif cmd == "abort":
                await handle_abort(conn_state)
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
            elif cmd == "extension_ui_response":
                await handle_extension_ui_response(conn_state, msg)
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
    """Start/restart container, connect Pi RPC, start event forwarding, resume session."""
    user = state["user"]
    host_path = str(
        workspace_manager.get_workspace_host_path(user["id"], workspace_id)
    )
    home_path = str(
        workspace_manager.get_home_host_path(user["id"], workspace_id)
    )

    # Find the most recent session file to resume (if any).
    # Sessions live inside the persistent home mount at .pi/sessions/.
    import glob  # noqa: E402

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
    container_id, container_status = await container_manager.start_container(
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
    )
    state["container_status"] = container_status
    state["workspace_id"] = workspace_id
    state["container_id"] = container_id

    # Lock prevents two simultaneous workspace_connect calls from both
    # seeing conn_num==1 and both trying to start Pi.
    async with _workspace_connect_lock:
        conn_num = container_manager.add_connection(workspace_id)

        if conn_num == 1:
            pi_client = PiRpcClient(container_id)
            await pi_client.connect()

            ws_state = {
                "pi_client": pi_client,
                "container_id": container_id,
                "agent_running": False,
                "subscribers": {ws},
                "event_task": asyncio.create_task(
                    forward_events(pi_client, workspace_id)
                ),
            }
            _workspace_state[workspace_id] = ws_state
            state["pi_client"] = pi_client
        else:
            # Share the Pi client and subscribe to events
            ws_state = _workspace_state.get(workspace_id, {})
            state["pi_client"] = ws_state.get("pi_client")
            subscribers = ws_state.get("subscribers")
            if subscribers is not None:
                subscribers.add(ws)

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
    container_manager.on_idle_stop(workspace_id, on_idle)

    # Cache workspace info for auto-restart
    state["workspace"] = workspace

    state["resume_session"] = resume_session
    if resume_session:
        logger.info(
            "Container started with session resume: %s for workspace %s",
            resume_session,
            workspace_id,
        )
    else:
        logger.info(
            "Container ready (new session) for workspace %s", workspace_id
        )


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

    ports = await container_manager.get_workspace_ports(workspace_id)
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

    if state.get("resume_session"):
        status_msg += " (session resumed)"

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
    state["pi_client"] = None


async def handle_prompt(ws: WebSocket, state: dict, msg: dict) -> None:
    text = msg.get("text", "")
    if not text:
        await send_error(ws, "Empty prompt")
        return

    workspace_id = state.get("workspace_id")
    if not workspace_id:
        await send_error(ws, "Not connected to a workspace")
        return

    logger.info(
        "Prompt received for workspace %s: %s", workspace_id, text[:80]
    )
    # Try to send prompt, auto-restart container if it's dead
    pi_client: PiRpcClient | None = state.get("pi_client")
    message_saved = False
    try:
        if pi_client is None or not pi_client.is_alive:
            raise RuntimeError("Pi client is dead or missing")
        container_manager.record_activity(state["container_id"])
        ws_state = _workspace_state.get(workspace_id, {})
        is_queued = ws_state.get("agent_running", False)
        await user_store.save_message(
            workspace_id, "user", text, is_queued=is_queued
        )
        message_saved = True
        # Show prompt in debug pane
        preview = text[:80] + ("..." if len(text) > 80 else "")
        await ws.send_json(
            {
                "type": "event",
                "event": {
                    "type": "CUSTOM",
                    "name": "query_prompt",
                    "value": {"text": preview},
                },
            }
        )
        if is_queued:
            await pi_client.follow_up(text)
            await ws.send_json(
                {
                    "type": "event",
                    "event": {
                        "type": "CUSTOM",
                        "name": "prompt_queued",
                        "value": {"text": text},
                    },
                }
            )
            logger.info("Agent busy, queued as follow_up: %s", text[:50])
        else:
            await pi_client.prompt(text)
            logger.info("Prompt sent to Pi for workspace %s", workspace_id)
    except (RuntimeError, OSError, ConnectionError) as e:
        logger.info(
            "Prompt failed (%s), auto-restarting container for workspace %s",
            e,
            workspace_id,
        )
        try:
            await ws.send_json(
                {
                    "type": "event",
                    "event": {
                        "type": "CUSTOM",
                        "name": "container_restart",
                        "value": {
                            "reason": "Container was idle and stopped. Restarting..."
                        },
                    },
                }
            )
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            pass
        # Clean up old connection
        try:
            await cleanup_connection(ws, state)
        except (RuntimeError, OSError, ConnectionError) as cleanup_err:
            logger.warning("Cleanup error during restart: %s", cleanup_err)

        # Restart container
        workspace = state.get("workspace")
        if workspace is None:
            workspace = await workspace_manager.get_workspace(
                workspace_id, state["user"]["id"]
            )
        if workspace is None:
            await send_error(ws, "Workspace not found")
            return

        await start_workspace_container(ws, state, workspace_id, workspace)

        # Record activity immediately to prevent idle timeout from killing it again
        container_manager.record_activity(state["container_id"])
        logger.info(
            "Container restarted, container_id=%s, waiting for Pi...",
            state["container_id"],
        )

        # Wait for Pi to be ready and session to resume, then retry the prompt
        await asyncio.sleep(4)
        pi_client = state.get("pi_client")
        logger.info(
            "After wait: pi_client=%s, is_alive=%s",
            pi_client,
            pi_client.is_alive if pi_client else None,
        )
        if pi_client and pi_client.is_alive:
            container_manager.record_activity(state["container_id"])
            if not message_saved:
                await user_store.save_message(workspace_id, "user", text)
            logger.info("Sending prompt after restart: %s", text[:50])
            await pi_client.prompt(text)
            logger.info("Prompt sent successfully after restart")
        else:
            logger.error("Pi client not alive after restart")
            await send_error(ws, "Failed to restart container")


async def handle_steer(state: dict, msg: dict) -> None:
    pi_client: PiRpcClient | None = state.get("pi_client")
    if pi_client is None:
        return
    container_manager.record_activity(state["container_id"])
    await pi_client.steer(msg.get("text", ""))


async def handle_follow_up(state: dict, msg: dict) -> None:
    pi_client: PiRpcClient | None = state.get("pi_client")
    if pi_client is None:
        return
    container_manager.record_activity(state["container_id"])
    await pi_client.follow_up(msg.get("text", ""))


async def handle_extension_ui_response(state: dict, msg: dict) -> None:
    """Forward extension UI response from frontend to Pi."""
    pi_client: PiRpcClient | None = state.get("pi_client")
    if pi_client is None:
        return
    # Forward the response as-is to Pi (it expects extension_ui_response)
    response = {"type": "extension_ui_response", "id": msg.get("id")}
    if "value" in msg:
        response["value"] = msg["value"]
    if msg.get("cancelled"):
        response["cancelled"] = True
    if "confirmed" in msg:
        response["confirmed"] = msg["confirmed"]
    await pi_client.send_command(response)


async def handle_abort(state: dict) -> None:
    pi_client: PiRpcClient | None = state.get("pi_client")
    if pi_client is None:
        return
    await pi_client.abort()


async def handle_restart_container(ws: WebSocket, state: dict) -> None:
    """Restart a stopped container (e.g., after idle timeout)."""
    workspace_id = state.get("workspace_id")
    if not workspace_id:
        await send_error(ws, "Not connected to a workspace")
        return

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

    workspace = state.get("workspace")
    if workspace is None:
        workspace = await workspace_manager.get_workspace(
            workspace_id, state["user"]["id"]
        )
    if workspace is None:
        await send_error(ws, "Workspace not found")
        return

    await start_workspace_container(ws, state, workspace_id, workspace)
    container_manager.record_activity(state["container_id"])

    ports = await container_manager.get_workspace_ports(workspace_id)
    ports_str = f" (ports {','.join(str(p) for p in ports)})" if ports else ""
    container_name = (
        f"bark-{container_manager.INSTANCE_ID}-{workspace_id[:12]}"
    )
    status_msg = f"Container restarted {container_name}{ports_str}"
    if state.get("resume_session"):
        status_msg += " (session resumed)"

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
    # Sent directly to the frontend (not stdin) so it works even while
    # bash.bashrc is waiting for the entrypoint to finish.
    await ws.send_json({"type": "terminal_output", "data": "\x1b[2J\x1b[H"})
    container_manager.record_activity(container_id)


async def handle_terminal_input(state: dict, msg: dict) -> None:
    session: TerminalSession | None = state.get("terminal_session")
    if session is None or not session.is_alive:
        return
    container_manager.record_activity(state["container_id"])
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
    container_manager.record_activity(container_id)


async def handle_exec_input(state: dict, msg: dict) -> None:
    session: ExecSession | None = state.get("exec_session")
    if session is None or not session.is_alive:
        return
    container_manager.record_activity(state["container_id"])
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
                container_manager.record_activity(container_id)
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
                container_manager.record_activity(container_id)
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


async def _broadcast(workspace_id: str, message: dict) -> None:
    """Send a message to all subscribers for a workspace, removing dead ones."""
    ws_state = _workspace_state.get(workspace_id)
    if not ws_state:  # pragma: no cover
        return
    subscribers = ws_state.get("subscribers")
    if not subscribers:  # pragma: no cover
        return
    dead = []
    for sub_ws in list(subscribers):
        try:
            await sub_ws.send_json(message)
        except (WebSocketDisconnect, RuntimeError, ConnectionError):
            dead.append(sub_ws)
    for sub_ws in dead:
        subscribers.discard(sub_ws)


async def forward_events(pi_client: PiRpcClient, workspace_id: str) -> None:
    """Forward Pi RPC events as AG-UI events to all workspace subscribers."""
    assistant_text = ""
    current_tool_name = ""
    current_tool_args = ""
    current_tool_output = ""

    event_count = 0
    try:
        async for pi_event in pi_client.events():
            event_count += 1
            if event_count <= 3:
                logger.info(
                    "Pi event #%d for %s: %s",
                    event_count,
                    workspace_id,
                    pi_event.get("type", "unknown"),
                )
            agui_events = translate_event(pi_event, workspace_id)
            for agui_event in agui_events:
                await _broadcast(
                    workspace_id,
                    {"type": "event", "event": agui_event},
                )

                etype = agui_event.get("type", "")

                # Track agent running state and keep container alive.
                # Use the real workspace_state dict, not a fallback —
                # if it's been popped (cleanup raced us), skip updates.
                ws_state = _workspace_state.get(workspace_id)
                if ws_state is not None:
                    if etype == "RUN_STARTED":
                        ws_state["agent_running"] = True
                    elif etype in ("RUN_FINISHED", "RUN_ERROR"):
                        ws_state["agent_running"] = False
                    cid = ws_state.get("container_id")
                    if cid:
                        container_manager.record_activity(cid)

                # Accumulate and save to history
                if etype == "TEXT_MESSAGE_CONTENT":
                    assistant_text += agui_event.get("delta", "")
                elif etype == "TEXT_MESSAGE_END":
                    if assistant_text:
                        await user_store.save_message(
                            workspace_id, "assistant", assistant_text
                        )
                        assistant_text = ""
                elif etype == "TOOL_CALL_START":
                    current_tool_name = agui_event.get("toolCallName", "tool")
                    current_tool_args = agui_event.get("toolCallArgs", "")
                    current_tool_output = ""
                elif etype == "TOOL_CALL_RESULT":
                    current_tool_output = agui_event.get("content", "")
                    await user_store.save_message(
                        workspace_id,
                        "tool_call",
                        current_tool_name,
                        tool_args=current_tool_args,
                        tool_output=current_tool_output,
                        is_complete=True,
                    )
                elif etype == "RUN_ERROR":
                    await user_store.save_message(
                        workspace_id,
                        "error",
                        agui_event.get("message", "Unknown error"),
                    )
    except asyncio.CancelledError:  # pragma: no cover
        raise
    except (OSError, RuntimeError, ConnectionError) as e:  # pragma: no cover
        logger.error("Event forwarding error for %s: %s", workspace_id, e)
    finally:
        logger.info(
            "Event forwarding ended for %s after %d events",
            workspace_id,
            event_count,
        )


async def cleanup_connection(ws: WebSocket, state: dict) -> None:
    # Remove idle callback
    workspace_id = state.get("workspace_id")
    idle_cb = state.get("_idle_cb")
    if workspace_id and idle_cb:
        container_manager.remove_idle_callback(workspace_id, idle_cb)
        state["_idle_cb"] = None

    await stop_terminal(state)
    await stop_exec(state)

    # Remove this WebSocket from event subscribers
    if workspace_id and workspace_id in _workspace_state:
        subscribers = _workspace_state[workspace_id].get("subscribers")
        if subscribers is not None:  # pragma: no cover
            subscribers.discard(ws)

    # Decrement connection refcount. Only the last connection to disconnect
    # kills Pi and (optionally) destroys the container.
    remaining = 0
    if workspace_id:
        remaining = container_manager.remove_connection(workspace_id)

    if remaining == 0 and workspace_id:
        # Last connection — clean up shared Pi state
        ws_state = _workspace_state.pop(workspace_id, {})

        event_task = ws_state.get("event_task")
        if event_task:
            event_task.cancel()
            try:
                await event_task
            except asyncio.CancelledError:
                pass

        pi_client: PiRpcClient | None = ws_state.get("pi_client")
        if pi_client:
            await pi_client.disconnect()

        container_id = state.get("container_id")
        if container_id:
            await container_manager.stop_and_remove_container(container_id)


async def send_error(ws: WebSocket, message: str) -> None:
    await ws.send_json({"type": "error", "message": message})
