"""WebSocket handler: auth, workspace routing, AG-UI event streaming."""

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from . import auth, container_manager, user_store, workspace_manager
from .agui_translator import translate_event
from .pi_rpc_client import PiRpcClient

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
    conn_state: dict = {"user": user, "pi_client": None, "container_id": None, "event_task": None}
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
                    await ws.send_json({"type": "event", "event": {
                        "type": "CUSTOM",
                        "name": "container_ready",
                        "value": {"reason": status_msg},
                    }})
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
            except Exception:
                pass
        _connections.pop(ws, None)


async def _start_workspace_container(ws: WebSocket, state: dict, workspace_id: str, workspace: dict) -> None:
    """Start/restart container, connect Pi RPC, start event forwarding, resume session."""
    user = state["user"]
    host_path = str(workspace_manager.get_workspace_host_path(user["id"], workspace["name"]))
    container_id, container_status = await container_manager.start_container(
        workspace_id, host_path, workspace.get("container_id")
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
            await ws.send_json({"type": "event", "event": {
                "type": "CUSTOM",
                "name": "container_idle_stop",
                "value": {"reason": "Container stopped due to idle timeout. Will restart on next prompt."},
            }})
        except Exception:
            pass
    state["_idle_cb"] = _on_idle
    container_manager.on_idle_stop(workspace_id, _on_idle)

    state["event_task"] = asyncio.create_task(
        _forward_events(ws, pi_client, workspace_id, state)
    )

    # Cache workspace info for auto-restart
    state["workspace"] = workspace

    asyncio.create_task(_resume_pi_session(pi_client, workspace_id, user["id"], workspace["name"]))
    logger.info("Container ready for workspace %s", workspace_id)


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

    ports = container_manager.get_workspace_ports(workspace_id)
    port_info = {"startPort": ports[0], "endPort": ports[1]} if ports else None
    status = state.get("container_status", "created")
    status_msg = {
        "connected": f"Connected to running container (ports {port_info['startPort']}-{port_info['endPort']})" if port_info else "Connected to running container",
        "restarted": f"Restarted stopped container (ports {port_info['startPort']}-{port_info['endPort']})" if port_info else "Restarted stopped container",
        "created": f"Created new container (ports {port_info['startPort']}-{port_info['endPort']})" if port_info else "Created new container",
    }.get(status, "Container ready")

    await ws.send_json({
        "type": "workspace_ready",
        "workspaceId": workspace_id,
        "ports": port_info,
    })
    # Store status for when frontend sends ui_ready
    state["pending_status_msg"] = status_msg
    logger.info("User %s connected to workspace %s (ports %s)", state["user"]["username"], workspace_id, port_info)


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
        if workspace_id:
            await user_store.save_message(workspace_id, "user", text)
        # Show prompt in debug pane
        preview = text[:80] + ("..." if len(text) > 80 else "")
        await ws.send_json({"type": "event", "event": {
            "type": "CUSTOM",
            "name": "query_prompt",
            "value": {"text": preview},
        }})
        await pi_client.prompt(text)
    except Exception as e:
        logger.info("Prompt failed (%s), auto-restarting container for workspace %s", e, workspace_id)
        try:
            await ws.send_json({"type": "event", "event": {
                "type": "CUSTOM",
                "name": "container_restart",
                "value": {"reason": "Container was idle and stopped. Restarting..."},
            }})
        except Exception:
            pass
        # Clean up old connection
        try:
            await _cleanup_connection(ws, state)
        except Exception as cleanup_err:
            logger.warning("Cleanup error during restart: %s", cleanup_err)

        # Restart container
        workspace = state.get("workspace")
        if workspace is None:
            workspace = await workspace_manager.get_workspace(workspace_id, state["user"]["id"])
        if workspace is None:
            await _send_error(ws, "Workspace not found")
            return

        await _start_workspace_container(ws, state, workspace_id, workspace)

        # Record activity immediately to prevent idle timeout from killing it again
        container_manager.record_activity(state["container_id"])
        logger.info("Container restarted, container_id=%s, waiting for Pi...", state["container_id"])

        # Wait for Pi to be ready and session to resume, then retry the prompt
        await asyncio.sleep(4)
        pi_client = state.get("pi_client")
        logger.info("After wait: pi_client=%s, is_alive=%s", pi_client, pi_client.is_alive if pi_client else None)
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


async def _handle_abort(state: dict) -> None:
    pi_client: PiRpcClient | None = state.get("pi_client")
    if pi_client is None:
        return
    await pi_client.abort()


async def _resume_pi_session(pi_client: PiRpcClient, workspace_id: str, user_id: str, workspace_name: str) -> None:
    """Resume Pi's most recent native session if one exists in the workspace."""
    import glob
    try:
        await asyncio.sleep(1)  # Give Pi a moment to finish startup

        # Find the most recent session file in the workspace's Pi sessions dir
        host_path = str(workspace_manager.get_workspace_host_path(user_id, workspace_name))
        sessions_dir = f"{host_path}/.pi/sessions"
        session_files = sorted(glob.glob(f"{sessions_dir}/**/*.jsonl", recursive=True))

        if not session_files:
            logger.info("No Pi sessions to resume for workspace %s", workspace_id)
            return

        # Resume the most recent session
        most_recent = session_files[-1]
        # Convert host path to container path
        container_path = most_recent.replace(host_path, "/workspace")

        await pi_client.send_command({
            "type": "switch_session",
            "sessionPath": container_path,
        })
        logger.info("Resumed Pi session %s for workspace %s", container_path, workspace_id)
    except Exception as e:
        logger.warning("Failed to resume Pi session: %s", e)


async def _forward_events(ws: WebSocket, pi_client: PiRpcClient, workspace_id: str, state: dict) -> None:
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

                # Accumulate and save to history
                etype = agui_event.get("type", "")
                if etype == "TEXT_MESSAGE_CONTENT":
                    assistant_text += agui_event.get("delta", "")
                elif etype == "TEXT_MESSAGE_END":
                    if assistant_text:
                        await user_store.save_message(workspace_id, "assistant", assistant_text)
                        assistant_text = ""
                elif etype == "TOOL_CALL_START":
                    current_tool_name = agui_event.get("toolCallName", "tool")
                    current_tool_args = agui_event.get("toolCallArgs", "")
                    current_tool_output = ""
                elif etype == "TOOL_CALL_RESULT":
                    current_tool_output = agui_event.get("content", "")
                    await user_store.save_message(
                        workspace_id, "tool_call", current_tool_name,
                        tool_args=current_tool_args, tool_output=current_tool_output,
                        is_complete=True,
                    )
                elif etype == "RUN_ERROR":
                    await user_store.save_message(
                        workspace_id, "error", agui_event.get("message", "Unknown error"),
                    )
    except Exception as e:
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


async def _send_error(ws: WebSocket, message: str) -> None:
    await ws.send_json({"type": "error", "message": message})
