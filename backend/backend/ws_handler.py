"""WebSocket handler: auth, workspace routing, AG-UI event streaming."""

import asyncio
import json
import logging
import os

from fastapi import WebSocket, WebSocketDisconnect

from . import auth, container_manager, user_store, workspace_manager
from .agui_translator import translate_event
from .pi_rpc_client import PiRpcClient
from .terminal_manager import TerminalSession

logger = logging.getLogger(__name__)

# Active connections: ws -> {user, workspace_id, pi_client, container_id}
_connections: dict[WebSocket, dict] = {}


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
    }
    _connections[ws] = conn_state

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await _send_error(ws, "Invalid JSON")
                continue

            cmd = msg.get("cmd")
            if cmd == "workspace_connect":
                await _handle_workspace_connect(ws, conn_state, msg)
            elif cmd == "workspace_disconnect":
                await _handle_workspace_disconnect(ws, conn_state)
            elif cmd == "prompt":
                await _handle_prompt(ws, conn_state, msg)
            elif cmd == "steer":
                await _handle_steer(conn_state, msg)
            elif cmd == "follow_up":
                await _handle_follow_up(conn_state, msg)
            elif cmd == "abort":
                await _handle_abort(conn_state)
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
                await _handle_extension_ui_response(conn_state, msg)
            elif cmd == "terminal_start":
                await _handle_terminal_start(ws, conn_state, msg)
            elif cmd == "terminal_input":
                await _handle_terminal_input(conn_state, msg)
            elif cmd == "terminal_resize":
                await _handle_terminal_resize(conn_state, msg)
            elif cmd == "terminal_stop":
                await _handle_terminal_stop(conn_state)
            elif cmd == "restart_container":
                await _handle_restart_container(ws, conn_state)
            else:
                await _send_error(ws, f"Unknown command: {cmd}")

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected for user %s", user["username"])
    except Exception as e:
        logger.error("WebSocket error: %s", e)
    finally:
        container_id = conn_state.get("container_id")
        await _cleanup_connection(ws, conn_state)
        if container_id:
            try:
                await container_manager.stop_container(container_id)
            except (OSError, RuntimeError, ConnectionError) as e:
                logger.error("Error stopping container on disconnect: %s", e)
        _connections.pop(ws, None)


def _derive_hosting_info(ws: WebSocket) -> tuple[str, str, str]:
    """Derive hosting hostname, proto, and base path from env vars or WebSocket headers.

    Returns (hostname, proto, base_path). Env vars take precedence over headers.
    """
    hostname = os.environ.get("BARK_HOSTING_HOSTNAME")
    proto = os.environ.get("BARK_HOSTING_PROTO")
    base_path = os.environ.get("BARK_HOSTING_BASE_PATH")
    if not hostname:
        hostname = (
            ws.headers.get("x-forwarded-host") or ws.headers.get("host") or "localhost"
        )
    if not proto:
        proto = ws.headers.get("x-forwarded-proto") or "http"
    if base_path is None:
        base_path = ws.headers.get("x-forwarded-prefix") or ""
    return hostname, proto, base_path


async def _start_workspace_container(
    ws: WebSocket, state: dict, workspace_id: str, workspace: dict
) -> None:
    """Start/restart container, connect Pi RPC, start event forwarding, resume session."""
    user = state["user"]
    host_path = str(workspace_manager.get_workspace_host_path(user["id"], workspace_id))
    sessions_path = str(
        workspace_manager.get_sessions_host_path(user["id"], workspace_id)
    )

    # Find the most recent session file to resume (if any)
    import glob  # noqa: E402

    session_files = sorted(glob.glob(f"{sessions_path}/**/*.jsonl", recursive=True))
    resume_session = None
    if session_files:
        most_recent = session_files[-1]
        resume_session = most_recent.replace(sessions_path, "/home/bark/.pi/sessions")

    hosting_hostname, hosting_proto, hosting_base_path = _derive_hosting_info(ws)
    container_id, container_status = await container_manager.start_container(
        workspace_id,
        host_path,
        sessions_path,
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

    pi_client = PiRpcClient(container_id)
    await pi_client.connect()

    state["workspace_id"] = workspace_id
    state["container_id"] = container_id
    state["pi_client"] = pi_client

    # Register idle timeout notification
    async def _on_idle(wid: str) -> None:
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

    state["_idle_cb"] = _on_idle
    container_manager.on_idle_stop(workspace_id, _on_idle)

    state["event_task"] = asyncio.create_task(
        _forward_events(ws, pi_client, workspace_id, state)
    )

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
        logger.info("Container ready (new session) for workspace %s", workspace_id)


async def _handle_workspace_connect(ws: WebSocket, state: dict, msg: dict) -> None:
    workspace_id = msg.get("workspaceId")
    if not workspace_id:
        await _send_error(ws, "Missing workspaceId")
        return

    user = state["user"]
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        await _send_error(ws, "Workspace not found")
        return

    # Disconnect from any current workspace
    await _handle_workspace_disconnect(ws, state)

    await _start_workspace_container(ws, state, workspace_id, workspace)

    ports = await container_manager.get_workspace_ports(workspace_id)
    status = state.get("container_status", "created")
    container_name = f"bark-{container_manager.INSTANCE_ID}-{workspace_id[:12]}"
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
        state["user"]["username"],
        workspace_id,
        ports,
    )


async def _handle_workspace_disconnect(ws: WebSocket, state: dict) -> None:
    container_id = state.get("container_id")
    await _cleanup_connection(ws, state)
    if container_id:
        await container_manager.stop_container(container_id)
    state["workspace_id"] = None
    state["container_id"] = None
    state["pi_client"] = None
    state["event_task"] = None


async def _handle_prompt(ws: WebSocket, state: dict, msg: dict) -> None:
    text = msg.get("text", "")
    if not text:
        await _send_error(ws, "Empty prompt")
        return

    workspace_id = state.get("workspace_id")
    if not workspace_id:
        await _send_error(ws, "Not connected to a workspace")
        return

    # Try to send prompt, auto-restart container if it's dead
    pi_client: PiRpcClient | None = state.get("pi_client")
    try:
        if pi_client is None or not pi_client.is_alive:
            raise RuntimeError("Pi client is dead or missing")
        container_manager.record_activity(state["container_id"])
        is_queued = state.get("agent_running", False)
        if workspace_id:
            await user_store.save_message(
                workspace_id, "user", text, is_queued=is_queued
            )
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
            await _cleanup_connection(ws, state)
        except (RuntimeError, OSError, ConnectionError) as cleanup_err:
            logger.warning("Cleanup error during restart: %s", cleanup_err)

        # Restart container
        workspace = state.get("workspace")
        if workspace is None:
            workspace = await workspace_manager.get_workspace(
                workspace_id, state["user"]["id"]
            )
        if workspace is None:
            await _send_error(ws, "Workspace not found")
            return

        await _start_workspace_container(ws, state, workspace_id, workspace)

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
            if workspace_id:
                await user_store.save_message(workspace_id, "user", text)
            logger.info("Sending prompt after restart: %s", text[:50])
            await pi_client.prompt(text)
            logger.info("Prompt sent successfully after restart")
        else:
            logger.error("Pi client not alive after restart")
            await _send_error(ws, "Failed to restart container")


async def _handle_steer(state: dict, msg: dict) -> None:
    pi_client: PiRpcClient | None = state.get("pi_client")
    if pi_client is None:
        return
    container_manager.record_activity(state["container_id"])
    await pi_client.steer(msg.get("text", ""))


async def _handle_follow_up(state: dict, msg: dict) -> None:
    pi_client: PiRpcClient | None = state.get("pi_client")
    if pi_client is None:
        return
    container_manager.record_activity(state["container_id"])
    await pi_client.follow_up(msg.get("text", ""))


async def _handle_extension_ui_response(state: dict, msg: dict) -> None:
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


async def _handle_abort(state: dict) -> None:
    pi_client: PiRpcClient | None = state.get("pi_client")
    if pi_client is None:
        return
    await pi_client.abort()


async def _handle_restart_container(ws: WebSocket, state: dict) -> None:
    """Restart a stopped container (e.g., after idle timeout)."""
    workspace_id = state.get("workspace_id")
    if not workspace_id:
        await _send_error(ws, "Not connected to a workspace")
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
        await _cleanup_connection(ws, state)
    except (RuntimeError, OSError, ConnectionError) as e:
        logger.warning("Cleanup error during restart: %s", e)

    workspace = state.get("workspace")
    if workspace is None:
        workspace = await workspace_manager.get_workspace(
            workspace_id, state["user"]["id"]
        )
    if workspace is None:
        await _send_error(ws, "Workspace not found")
        return

    await _start_workspace_container(ws, state, workspace_id, workspace)
    container_manager.record_activity(state["container_id"])

    ports = await container_manager.get_workspace_ports(workspace_id)
    ports_str = f" (ports {','.join(str(p) for p in ports)})" if ports else ""
    container_name = f"bark-{container_manager.INSTANCE_ID}-{workspace_id[:12]}"
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


async def _handle_terminal_start(ws: WebSocket, state: dict, msg: dict) -> None:
    container_id = state.get("container_id")
    if not container_id:
        return
    # Stop existing terminal if any
    await _stop_terminal(state)
    cols = msg.get("cols", 80)
    rows = msg.get("rows", 24)
    session = TerminalSession(container_id)
    await session.start(cols, rows)
    state["terminal_session"] = session
    state["terminal_task"] = asyncio.create_task(
        _forward_terminal_output(ws, session, state)
    )
    container_manager.record_activity(container_id)


async def _handle_terminal_input(state: dict, msg: dict) -> None:
    session: TerminalSession | None = state.get("terminal_session")
    if session is None or not session.is_alive:
        return
    container_manager.record_activity(state["container_id"])
    await session.write(msg.get("data", ""))


async def _handle_terminal_resize(state: dict, msg: dict) -> None:
    session: TerminalSession | None = state.get("terminal_session")
    if session is None:
        return
    await session.resize(msg.get("cols", 80), msg.get("rows", 24))


async def _handle_terminal_stop(state: dict) -> None:
    await _stop_terminal(state)


async def _stop_terminal(state: dict) -> None:
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


async def _forward_terminal_output(
    ws: WebSocket, session: TerminalSession, state: dict
) -> None:
    """Forward terminal output to the frontend via WebSocket."""
    try:
        async for data in session.output():
            await ws.send_json({"type": "terminal_output", "data": data})
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


async def _forward_events(
    ws: WebSocket, pi_client: PiRpcClient, workspace_id: str, state: dict
) -> None:
    """Forward Pi RPC events as AG-UI events over WebSocket, saving to history."""
    assistant_text = ""
    current_tool_name = ""
    current_tool_args = ""
    current_tool_output = ""

    try:
        async for pi_event in pi_client.events():
            agui_events = translate_event(pi_event, workspace_id)
            for agui_event in agui_events:
                await ws.send_json({"type": "event", "event": agui_event})

                # Track agent running state
                etype = agui_event.get("type", "")
                if etype == "RUN_STARTED":
                    state["agent_running"] = True
                elif etype in ("RUN_FINISHED", "RUN_ERROR"):
                    state["agent_running"] = False

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
    except (OSError, WebSocketDisconnect, RuntimeError, ConnectionError) as e:
        logger.error("Event forwarding error: %s", e)


async def _cleanup_connection(ws: WebSocket, state: dict) -> None:
    # Remove idle callback
    workspace_id = state.get("workspace_id")
    idle_cb = state.get("_idle_cb")
    if workspace_id and idle_cb:
        container_manager.remove_idle_callback(workspace_id, idle_cb)
        state["_idle_cb"] = None

    if state.get("event_task"):
        state["event_task"].cancel()
        try:
            await state["event_task"]
        except asyncio.CancelledError:
            pass

    pi_client: PiRpcClient | None = state.get("pi_client")
    if pi_client:
        await pi_client.disconnect()

    await _stop_terminal(state)


async def _send_error(ws: WebSocket, message: str) -> None:
    await ws.send_json({"type": "error", "message": message})
